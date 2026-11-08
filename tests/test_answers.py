"""Answer cache (answers.py), on a made-up domain so the core is tested without EDGAR."""

import pytest
from fakes import FakeClock, FakeConnector

from multi_agent_ai_systems.answers import AnswerCache, Outcome
from multi_agent_ai_systems.cache import RecordCache
from multi_agent_ai_systems.connectors.base import SourceError
from multi_agent_ai_systems.dependencies import Dependency
from multi_agent_ai_systems.gateway import Gateway
from multi_agent_ai_systems.validity import TTLPolicy

HOUR = 3600.0


def evaluate(probe, bodies):
    """The made-up domain's probes: "field.v1:<name>" reads one field of a "k=v;k=v" record."""
    name, _, field = probe.partition(":")
    if name != "field.v1":
        raise ValueError(f"unknown probe: {probe!r}")
    (body,) = bodies
    fields = dict(pair.split("=") for pair in body.split(";"))
    return fields.get(field, {"error": "missing"})


def temp(key="station/1", expected="21"):
    return Dependency((key,), "field.v1:temp", expected)


@pytest.fixture
def setup():
    conn = FakeConnector({"station/1": "temp=21;wind=5", "station/2": "temp=18;wind=2"})
    clock = FakeClock()
    gateway = Gateway(conn, cache=RecordCache(1_000_000), policy=TTLPolicy(HOUR), clock=clock)
    answers = AnswerCache(gateway, evaluate, RecordCache(1_000_000))
    return conn, clock, gateway, answers


async def test_unknown_question_is_a_miss(setup):
    *_, answers = setup
    assert (await answers.get("q")).outcome is Outcome.MISS


async def test_answer_is_reused_while_its_probes_match(setup):
    conn, _, _, answers = setup
    answers.put("q", {"temp": 21}, [temp()])
    first = await answers.get("q")
    assert (first.outcome, first.answer) == (Outcome.MATCH, {"temp": 21})
    assert (await answers.get("q")).outcome is Outcome.MATCH
    assert conn.calls == 1  # the record was fetched once, then served from the record cache


async def test_change_to_an_unused_field_keeps_the_answer(setup):
    conn, clock, _, answers = setup
    answers.put("q", {"temp": 21}, [temp()])
    await answers.get("q")
    conn.data["station/1"] = "temp=21;wind=40"
    clock.advance(HOUR + 1)  # the record expires, so the check reads the new version
    assert (await answers.get("q")).outcome is Outcome.MATCH
    assert conn.calls == 2


async def test_change_to_a_used_field_invalidates_the_answer(setup):
    conn, _, _, answers = setup
    answers.put("q", {"temp": 21}, [temp()])
    conn.data["station/1"] = "temp=25;wind=5"
    lookup = await answers.get("q")
    assert (lookup.outcome, lookup.changed) == (Outcome.CHANGED, ("field.v1:temp",))
    assert answers.stats.changed_probes["field.v1"] == 1
    assert (await answers.get("q")).outcome is Outcome.MISS  # the stale answer was dropped


async def test_check_is_as_fresh_as_the_record_layer_and_max_age_require(setup):
    conn, clock, _, answers = setup
    answers.put("q", {"temp": 21}, [temp()])
    await answers.get("q")  # caches the record
    conn.data["station/1"] = "temp=25;wind=5"
    clock.advance(60)
    assert (await answers.get("q")).outcome is Outcome.MATCH  # the cached record is within TTL
    assert (await answers.get("q", max_age=30)).outcome is Outcome.CHANGED  # caller wants fresher


async def test_probe_the_plugin_no_longer_knows_is_unknown_and_dropped(setup):
    *_, answers = setup
    answers.put("q", "x", [Dependency(("station/1",), "field.v0:temp", "21")])
    assert (await answers.get("q")).outcome is Outcome.UNKNOWN
    assert (await answers.get("q")).outcome is Outcome.MISS


async def test_source_failure_is_unknown_and_keeps_the_answer(setup):
    conn, _, _, answers = setup
    answers.put("q", "x", [temp()])
    conn.failures.append(SourceError("down"))
    assert (await answers.get("q")).outcome is Outcome.UNKNOWN
    assert (await answers.get("q")).outcome is Outcome.MATCH  # checked once the source is back


async def test_each_record_is_read_once_per_check(setup):
    _, _, gateway, answers = setup
    wind = Dependency(("station/1",), "field.v1:wind", "5")
    answers.put("q", "x", [temp(), wind, temp("station/2", "18")])
    assert (await answers.get("q")).outcome is Outcome.MATCH
    assert (gateway.stats.misses, gateway.stats.hits) == (2, 0)  # station/1 asked once, not twice


async def test_stored_answer_is_a_copy(setup):
    *_, answers = setup
    answer = {"temp": 21}
    answers.put("q", answer, [temp()])
    answer["temp"] = 99
    assert (await answers.get("q")).answer == {"temp": 21}


def test_answer_must_be_json_compatible(setup):
    *_, answers = setup
    with pytest.raises(TypeError):
        answers.put("q", {1, 2}, [temp()])