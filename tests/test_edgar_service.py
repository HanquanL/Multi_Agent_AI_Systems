"""get_fact service (edgar_service.py): resolver + probes + answer cache, through the gateway."""

import json
from dataclasses import replace

import pytest
from fakes import FakeClock, FakeConnector, FakeProvider
from mcp import Client
from test_edgar_facts import (
    APPLE_FACTS,
    APPLE_NET_INCOME,
    APPLE_SUBMISSIONS,
    companyfacts,
    fact_rows,
)

from multi_agent_ai_systems.answers import Outcome
from multi_agent_ai_systems.cache import RecordCache
from multi_agent_ai_systems.connectors.edgar import companyfacts_key, submissions_key
from multi_agent_ai_systems.connectors.edgar_facts import FactError
from multi_agent_ai_systems.connectors.edgar_service import (
    FactContext,
    FactService,
    register_tools,
)
from multi_agent_ai_systems.gateway import Gateway
from multi_agent_ai_systems.inference import ContextError, InferenceGateway
from multi_agent_ai_systems.llm import GenerationRequest, Message
from multi_agent_ai_systems.server import build_server
from multi_agent_ai_systems.validity import TTLPolicy

HOUR = 3600.0
FACTS, SUBMISSIONS = companyfacts_key("320193"), submissions_key("320193")
QUESTION = GenerationRequest((Message("user", "How did Apple do?"),), max_tokens=50)
SPEC = ("320193:NetIncome:FY2024",)

def llm_for(service, clock):
    provider = FakeProvider()
    gateway = InferenceGateway(
        provider,
        cache=RecordCache(1_000_000),
        policy=TTLPolicy(168 * HOUR),
        context_source=FactContext(service),
        clock=clock,
    )
    return gateway, provider


async def test_llm_answer_is_grounded_in_the_resolved_fact(setup):
    _, clock, service = setup
    llm, provider = llm_for(service, clock)
    await llm.generate(QUESTION, context=SPEC)
    system = provider.requests[0].messages[0].content
    assert "93736000000 USD" in system
    assert "2023-10-01 to 2024-09-28" in system
    assert "0000320193" in system and "-25-000079" not in system  # no filing is named


async def test_restatement_regenerates_the_llm_answer(setup):
    conn, clock, service = setup
    llm, provider = llm_for(service, clock)
    await llm.generate(QUESTION, context=SPEC)
    amended = "2023-10-01 2024-09-28 2024 FY 10-K/A 2025-12-01 0000320193-25-000999 93000000000"
    conn.data[FACTS] = net_income(APPLE_NET_INCOME + amended)
    clock.advance(HOUR + 1)
    assert (await llm.generate(QUESTION, context=SPEC)).source == "upstream"
    assert "93000000000 USD" in provider.requests[1].messages[0].content
    assert llm.stats.changed_probes["fact.v1"] == 1


async def test_unchanged_comparative_keeps_the_llm_answer(setup):
    conn, clock, service = setup
    day1 = "\n".join(line for line in APPLE_NET_INCOME.split("\n") if "25-000079" not in line)
    conn.data[FACTS] = net_income(day1)
    llm, provider = llm_for(service, clock)
    await llm.generate(QUESTION, context=SPEC)
    conn.data[FACTS] = APPLE_FACTS  # the 2025 10-K repeats FY2024 with the same value
    clock.advance(HOUR + 1)
    assert (await llm.generate(QUESTION, context=SPEC)).source == "cache"
    assert provider.calls == 1


@pytest.mark.parametrize(
    "spec",
    ["320193:NetIncome:2024Q5", "320193:NetIncome", "320193:NetIncome:2024Q4"],
)
async def test_unanswerable_fact_makes_no_llm_call(setup, spec):
    _, clock, service = setup
    llm, provider = llm_for(service, clock)
    with pytest.raises(ContextError):
        await llm.generate(QUESTION, context=(spec,))
    assert provider.calls == 0
    
class JsonConnector(FakeConnector):
    """Serves JSON bodies, as the EDGAR connector does for companyfacts and submissions."""

    async def fetch(self, key):
        record = await super().fetch(key)
        return replace(record, size_bytes=len(json.dumps(record.data)))


def net_income(text):
    return companyfacts({"NetIncomeLoss": ("USD", fact_rows(text))})


@pytest.fixture
def setup():
    conn = JsonConnector({FACTS: APPLE_FACTS, SUBMISSIONS: APPLE_SUBMISSIONS})
    clock = FakeClock()
    gateway = Gateway(conn, cache=RecordCache(10_000_000), policy=TTLPolicy(HOUR), clock=clock)
    return conn, clock, FactService(gateway)


async def test_answer_is_computed_once_then_reused(setup):
    conn, _, service = setup
    first = await service.get_fact("320193", "NetIncome", "FY2024")
    second = await service.get_fact("0000320193", "NetIncome", "FY2024")  # same CIK, padded
    assert first.outcome is Outcome.MISS
    assert (first.answer["value"], first.answer["accn"]) == (93_736_000_000, "0000320193-25-000079")
    assert (second.outcome, second.answer) == (Outcome.MATCH, first.answer)
    assert conn.calls == 2  # companyfacts and submissions, each fetched once


async def test_unrelated_new_filing_keeps_the_answer(setup):
    conn, clock, service = setup
    await service.get_fact("320193", "NetIncome", "FY2024")
    # A later quarter appears (made-up row). FY2024 is untouched.
    extra = "2025-12-28 2026-03-28 2026 Q2 10-Q 2026-05-01 0000320193-26-000100 1"
    conn.data[FACTS] = net_income(APPLE_NET_INCOME + extra)
    clock.advance(HOUR + 1)  # both records expire, so the check reads the new versions
    assert (await service.get_fact("320193", "NetIncome", "FY2024")).outcome is Outcome.MATCH
    assert conn.calls == 4


async def test_restatement_recomputes_the_answer(setup):
    conn, clock, service = setup
    await service.get_fact("320193", "NetIncome", "FY2024")
    # A made-up amendment restates FY2024.
    amended = "2023-10-01 2024-09-28 2024 FY 10-K/A 2025-12-01 0000320193-25-000999 93000000000"
    conn.data[FACTS] = net_income(APPLE_NET_INCOME + amended)
    clock.advance(HOUR + 1)
    result = await service.get_fact("320193", "NetIncome", "FY2024")
    assert result.outcome is Outcome.CHANGED
    assert (result.answer["value"], result.answer["form"]) == (93_000_000_000, "10-K/A")
    assert service.answers.stats.changed_probes["fact.v1"] == 1
    assert (await service.get_fact("320193", "NetIncome", "FY2024")).outcome is Outcome.MATCH


async def test_provenance_dependency_is_opt_in(setup):
    conn, clock, service = setup
    # Day 1: before the 2025 10-K, which repeats FY2024 with the same value.
    day1 = "\n".join(line for line in APPLE_NET_INCOME.split("\n") if "25-000079" not in line)
    conn.data[FACTS] = net_income(day1)
    await service.get_fact("320193", "NetIncome", "FY2024")
    await service.get_fact("320193", "NetIncome", "FY2024", provenance=True)

    conn.data[FACTS] = APPLE_FACTS  # day 2: the 2025 10-K is out
    clock.advance(HOUR + 1)
    value_only = await service.get_fact("320193", "NetIncome", "FY2024")
    with_source = await service.get_fact("320193", "NetIncome", "FY2024", provenance=True)

    assert value_only.outcome is Outcome.MATCH  # same value: still valid
    assert value_only.answer["accn"] == "0000320193-24-000123"  # provenance shows the older filing
    assert with_source.outcome is Outcome.CHANGED
    assert with_source.answer["accn"] == "0000320193-25-000079"


@pytest.mark.parametrize(("metric", "period"), [("GrossMargin", "FY2024"), ("NetIncome", "2024Q5")])
async def test_bad_question_fails_before_any_fetch(setup, metric, period):
    conn, _, service = setup
    with pytest.raises(FactError):
        await service.get_fact("320193", metric, period)
    assert conn.calls == 0


async def test_bad_cik_fails_before_any_fetch(setup):
    conn, _, service = setup
    with pytest.raises(ValueError):
        await service.get_fact("AAPL", "NetIncome", "FY2024")  # tickers come later
    assert conn.calls == 0

def make_server(service):
    return build_server(
        service.gateway,
        tools=[lambda s: register_tools(s, service)],
        answers={"get_fact": service.answers},
    )


async def test_get_fact_tool_reports_reuse(setup):
    _, _, service = setup
    args = {"cik": "320193", "metric": "NetIncome", "period": "FY2024"}
    async with Client(make_server(service)) as client:
        first = (await client.call_tool("get_fact", args)).structured_content
        second = (await client.call_tool("get_fact", args)).structured_content
        stats = (await client.call_tool("gateway_stats", {})).structured_content

    assert (first["value"], first["period"], first["answer_kind"]) == (
        93_736_000_000,
        "FY2024",
        "exact",
    )
    assert (first["cache"], second["cache"]) == ("miss", "match")
    assert stats["answers"]["get_fact"]["outcomes"] == {"miss": 1, "match": 1}


async def test_get_fact_tool_refusal_names_its_kind(setup):
    _, _, service = setup
    args = {"cik": "320193", "metric": "NetIncome", "period": "2024Q4"}
    async with Client(make_server(service)) as client:
        result = await client.call_tool("get_fact", args)
    assert result.is_error is True
    assert "not_reported:" in result.content[0].text