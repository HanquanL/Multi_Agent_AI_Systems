"""Persistent LLM responses, including the agreed 24-hour acceptance test (C14)."""

import pytest
from fakes import FakeClock, FakeProvider

from multi_agent_ai_systems.cache import RecordCache
from multi_agent_ai_systems.inference import InferenceGateway
from multi_agent_ai_systems.llm import GenerationRequest, Message
from multi_agent_ai_systems.store import SQLiteRecordStore
from multi_agent_ai_systems.validity import TTLPolicy

HOUR = 3600.0
DAY = 24 * HOUR
WEEK = 7 * DAY
TEAM = "team-a"
REQUEST = GenerationRequest((Message("user", "hello"),), max_tokens=50)


class Env:
    """A gateway that can be restarted on the same file, with a wall clock the test moves."""

    def __init__(self, path):
        self.path = path
        self.wall = 1_000_000.0
        self.provider = FakeProvider()
        self.store = None
        self.gateway = None

    def start(self, *, ttl=WEEK, with_store=True):
        self.store = (
            SQLiteRecordStore(self.path, 1_000_000, wall_clock=lambda: self.wall)
            if with_store
            else None
        )
        self.gateway = InferenceGateway(  # a new process: empty memory, a new monotonic clock
            self.provider,
            cache=RecordCache(1_000_000),
            policy=TTLPolicy(ttl),
            store=self.store,
            clock=FakeClock(),
        )

    async def stop(self):
        await self.gateway.close()
        if self.store is not None:
            self.store.close()

    async def restart(self, *, after=0.0, **kwargs):
        await self.stop()
        self.wall += after
        self.start(**kwargs)


@pytest.fixture
async def env(tmp_path):
    environment = Env(tmp_path / "responses.db")
    environment.start()
    yield environment
    await environment.stop()


async def test_answer_survives_restart_and_a_day_without_another_llm_call(env):  # acceptance
    first = await env.gateway.generate(REQUEST, workload=TEAM, agent="agent-a")
    await env.restart(after=DAY)
    second = await env.gateway.generate(REQUEST, workload=TEAM, agent="agent-b")

    assert (first.source, second.source) == ("upstream", "store")
    assert second.text == first.text
    assert second.answer_kind == "generated"
    assert second.age_seconds == pytest.approx(DAY)
    assert env.provider.calls == 1  # the requirement: no second LLM call

    counts = env.gateway.stats.workload("default")
    assert (counts.cache_hits, counts.store_hits, counts.calls) == (1, 1, 0)
    assert (counts.prompt_tokens_avoided, counts.completion_tokens_avoided) == (1, 2)


async def test_store_hit_moves_to_memory_and_keeps_its_age(env):
    await env.gateway.generate(REQUEST, workload=TEAM)
    await env.restart(after=DAY)
    await env.gateway.generate(REQUEST, workload=TEAM)
    third = await env.gateway.generate(REQUEST, workload=TEAM)
    assert (third.source, third.age_seconds) == ("cache", pytest.approx(DAY))


async def test_without_the_store_a_restart_loses_the_answer(env):  # the control
    await env.restart(with_store=False)
    await env.gateway.generate(REQUEST, workload=TEAM)
    await env.restart(after=DAY, with_store=False)
    assert (await env.gateway.generate(REQUEST, workload=TEAM)).source == "upstream"
    assert env.provider.calls == 2


async def test_other_workloads_cannot_read_it_after_a_restart(env):
    await env.gateway.generate(REQUEST, workload=TEAM)
    await env.restart(after=DAY)
    assert (await env.gateway.generate(REQUEST, workload="team-b")).source == "upstream"


async def test_a_restart_does_not_extend_the_lifetime(env):
    await env.restart(ttl=12 * HOUR)
    await env.gateway.generate(REQUEST, workload=TEAM)
    await env.restart(after=DAY, ttl=12 * HOUR)
    assert (await env.gateway.generate(REQUEST, workload=TEAM)).source == "upstream"
    assert env.provider.calls == 2


async def test_max_age_applies_to_stored_answers(env):
    await env.gateway.generate(REQUEST, workload=TEAM)
    await env.restart(after=DAY)
    result = await env.gateway.generate(REQUEST, workload=TEAM, max_age=HOUR)
    assert result.source == "upstream"


async def test_model_update_after_a_restart_does_not_reuse_old_answers(env):
    await env.gateway.generate(REQUEST, workload=TEAM)
    env.provider.version = "v2"
    await env.restart(after=DAY)
    assert (await env.gateway.generate(REQUEST, workload=TEAM)).source == "upstream"


async def test_truncated_response_is_not_written_to_disk(env):
    env.provider.finish_reason = "length"
    await env.gateway.generate(REQUEST, workload=TEAM)
    env.provider.finish_reason = "stop"
    await env.restart(after=DAY)
    assert (await env.gateway.generate(REQUEST, workload=TEAM)).source == "upstream"