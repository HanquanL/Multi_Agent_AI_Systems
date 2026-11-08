"""LLM answers grounded in checked data, on a made-up domain so the core needs no EDGAR."""

import pytest
from fakes import FakeClock, FakeConnector, FakeProvider

from multi_agent_ai_systems.answers import DependencyChecker
from multi_agent_ai_systems.cache import RecordCache
from multi_agent_ai_systems.connectors.base import SourceError
from multi_agent_ai_systems.dependencies import Dependency
from multi_agent_ai_systems.gateway import Gateway
from multi_agent_ai_systems.inference import ContextError, Grounding, InferenceGateway
from multi_agent_ai_systems.llm import GenerationRequest, Message
from multi_agent_ai_systems.store import SQLiteRecordStore
from multi_agent_ai_systems.validity import TTLPolicy

HOUR = 3600.0
WEEK = 168 * HOUR
QUESTION = GenerationRequest((Message("user", "Is it warm?"),), max_tokens=50)
ONE = ("station/1",)


def evaluate(probe, bodies):
    """"field.v1:<name>" reads one field of a "k=v;k=v" record."""
    name, _, wanted = probe.partition(":")
    if name != "field.v1":
        raise ValueError(probe)
    return dict(pair.split("=") for pair in bodies[0].split(";")).get(wanted)


class WeatherContext:
    """A ContextSource for the made-up domain: a specification is a station's record key."""

    def __init__(self, gateway):
        self.gateway = gateway
        self.checker = DependencyChecker(gateway, evaluate)

    def normalize(self, spec):
        if not spec.startswith("station/"):
            raise ContextError(f"not a station: {spec}")
        return spec

    async def ground(self, specs, *, max_age=None, workload="default", agent="anonymous"):
        lines, deps = [], []
        for spec in specs:
            response = await self.gateway.get(spec, max_age=max_age, workload=workload, agent=agent)
            temp = evaluate("field.v1:temp", [response.data])
            lines.append(f"{spec} temp {temp}")
            deps.append(Dependency((spec,), "field.v1:temp", temp))
        return Grounding("\n".join(lines), tuple(deps))


class Env:
    def __init__(self, store=None):
        self.conn = FakeConnector({"station/1": "temp=21;wind=5", "station/2": "temp=18;wind=2"})
        self.provider = FakeProvider()
        self.clock = FakeClock()
        self.store = store
        self.data = Gateway(
            self.conn, cache=RecordCache(1_000_000), policy=TTLPolicy(HOUR), clock=self.clock
        )
        self.llm = self.new_llm()

    def new_llm(self):
        return InferenceGateway(
            self.provider,
            cache=RecordCache(1_000_000),
            policy=TTLPolicy(WEEK),
            store=self.store,
            context_source=WeatherContext(self.data),
            clock=self.clock,
        )

    def ask(self, request=QUESTION, **kwargs):
        return self.llm.generate(request, **{"context": ONE, **kwargs})

    @property
    def counts(self):
        return self.llm.stats.workload("default")


@pytest.fixture
def env():
    return Env()


async def test_context_is_added_first_in_the_prompt(env):
    await env.ask()
    messages = env.provider.requests[0].messages
    assert messages[0] == Message("system", "station/1 temp 21")
    assert messages[1] == QUESTION.messages[0]


async def test_repeat_is_reused_while_dependencies_match(env):
    await env.ask()
    assert (await env.ask()).source == "cache"
    assert env.provider.calls == 1


async def test_change_to_data_the_answer_did_not_use_keeps_it(env):
    await env.ask()
    env.conn.data["station/1"] = "temp=21;wind=40"
    env.clock.advance(HOUR + 1)  # the record expires, so the check reads the new one
    assert (await env.ask()).source == "cache"
    assert (env.provider.calls, env.conn.calls) == (1, 2)


async def test_change_to_data_the_answer_used_regenerates_it(env):
    await env.ask()
    env.conn.data["station/1"] = "temp=25;wind=5"
    env.clock.advance(HOUR + 1)
    assert (await env.ask()).source == "upstream"
    assert env.provider.requests[1].messages[0].content == "station/1 temp 25"
    assert env.counts.invalidated == 1
    assert env.llm.stats.changed_probes["field.v1"] == 1
    assert (await env.ask()).source == "cache"  # the same specification finds the new answer
    assert env.provider.calls == 2


async def test_different_specifications_are_different_questions(env):
    await env.ask()
    assert (await env.ask(context=("station/2",))).source == "upstream"
    assert env.provider.calls == 2


async def test_invalid_specification_makes_no_calls(env):
    with pytest.raises(ContextError):
        await env.ask(context=("city/1",))
    assert (env.provider.calls, env.conn.calls) == (0, 0)


async def test_context_needs_a_context_source():
    llm = InferenceGateway(FakeProvider(), cache=RecordCache(1_000), policy=TTLPolicy(WEEK))
    with pytest.raises(ValueError):
        await llm.generate(QUESTION, context=ONE)


async def test_data_max_age_requires_fresher_records(env):
    await env.ask()
    env.conn.data["station/1"] = "temp=25;wind=5"
    env.clock.advance(60)
    assert (await env.ask()).source == "cache"  # the data record is still within its TTL
    assert (await env.ask(data_max_age=30)).source == "upstream"


async def test_unverifiable_answer_is_not_served(env):
    await env.ask()
    env.clock.advance(HOUR + 1)
    env.conn.failures = [SourceError("down"), SourceError("down")]  # the check, then grounding
    with pytest.raises(SourceError):
        await env.ask()
    assert env.provider.calls == 1
    assert (await env.ask()).source == "cache"  # kept: it is checked once the source is back


async def test_no_store_is_never_cached(env):
    request = GenerationRequest(QUESTION.messages, max_tokens=50, no_store=True)
    await env.ask(request)
    assert (await env.ask(request)).source == "upstream"
    assert len(env.llm.cache) == 0


async def test_random_request_is_grounded_but_not_reused(env):
    request = GenerationRequest(QUESTION.messages, max_tokens=50, temperature=0.7)
    await env.ask(request)
    await env.ask(request)
    assert env.provider.calls == 2
    assert env.provider.requests[1].messages[0].role == "system"


async def test_dependencies_survive_on_disk(tmp_path):
    store = SQLiteRecordStore(tmp_path / "responses.db", 1_000_000)
    env = Env(store)
    await env.ask()
    env.llm = env.new_llm()  # empty memory, same file
    assert (await env.ask()).source == "store"

    env.conn.data["station/1"] = "temp=25;wind=5"
    env.clock.advance(HOUR + 1)
    env.llm = env.new_llm()
    assert (await env.ask()).source == "upstream"  # the dependency came back from disk too
    assert env.provider.calls == 2
    store.close()