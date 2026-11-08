import asyncio
from datetime import UTC, datetime

import pytest
from fakes import FakeClock, FakeConnector, FakeProvider
from mcp import Client

from multi_agent_ai_systems.answers import AnswerCache
from multi_agent_ai_systems.cache import RecordCache
from multi_agent_ai_systems.connectors.base import SourceError
from multi_agent_ai_systems.dependencies import Dependency
from multi_agent_ai_systems.gateway import FreshnessLabel, Gateway, Response
from multi_agent_ai_systems.inference import InferenceGateway
from multi_agent_ai_systems.llm import ModelPrice
from multi_agent_ai_systems.server import build_server, to_payload
from multi_agent_ai_systems.validity import TTLPolicy

HELLO = {"messages": [{"role": "user", "content": "hello"}], "max_tokens": 50}

def make_server(conn: FakeConnector):
    gateway = Gateway(conn, cache=RecordCache(1_000_000), policy=TTLPolicy(3600), clock=FakeClock())
    return gateway, build_server(gateway)


async def call(server, tool: str, args: dict | None = None):
    async with Client(server) as client:
        return await client.call_tool(tool, args or {})


async def test_server_lists_both_tools():
    _, server = make_server(FakeConnector())
    async with Client(server) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
    assert names == {"get_record", "gateway_stats"}


async def test_get_record_returns_data_and_freshness_label():
    _, server = make_server(FakeConnector({"a": "x"}))
    first = await call(server, "get_record", {"key": "a"})
    second = await call(server, "get_record", {"key": "a"})

    assert first.is_error is False
    assert first.structured_content["data"] == "x"
    assert first.structured_content["freshness"]["source"] == "upstream"
    assert first.structured_content["freshness"]["version"] == "v1"
    assert second.structured_content["freshness"]["source"] == "cache"


async def test_three_clients_share_one_fetch():
    conn = FakeConnector({"a": "x"})
    conn.gate = asyncio.Event()
    gateway, server = make_server(conn)

    tasks = [asyncio.create_task(call(server, "get_record", {"key": "a"})) for _ in range(3)]
    for _ in range(100):  # wait until all three requests reach the gateway
        if gateway.stats.misses == 3:
            break
        await asyncio.sleep(0.01)
    conn.gate.set()
    results = await asyncio.gather(*tasks)

    assert gateway.stats.misses == 3
    assert all(r.structured_content["data"] == "x" for r in results)
    assert conn.calls == 1


async def test_missing_record_is_a_readable_tool_error():
    _, server = make_server(FakeConnector({}))
    result = await call(server, "get_record", {"key": "nope"})
    assert result.is_error is True
    assert "not found: nope" in result.content[0].text


async def test_source_failure_is_a_readable_tool_error():
    conn = FakeConnector({"a": "x"})
    conn.failures = [SourceError("upstream down")]
    _, server = make_server(conn)
    result = await call(server, "get_record", {"key": "a"})
    assert result.is_error is True
    assert "source error: upstream down" in result.content[0].text


async def test_gateway_stats_tool():
    _, server = make_server(FakeConnector({"a": "x"}))
    await call(server, "get_record", {"key": "a"})
    await call(server, "get_record", {"key": "a"})
    stats = (await call(server, "gateway_stats")).structured_content

    assert (stats["hits"], stats["misses"]) == (1, 1)
    assert stats["upstream_fetches"] == {"first": 1}
    assert stats["cache"]["entries"] == 1
    assert stats["rate_limit"]["max_requests"] == 10


def test_bytes_are_returned_as_text():
    label = FreshnessLabel(
        key="k", version=None, fetched_at=datetime.now(UTC), age_seconds=0.0, source="upstream"
    )
    assert to_payload(Response(b"<html>", label))["data"] == "<html>"

async def test_plugins_add_tools_and_answer_cache_stats():
    gateway, _ = make_server(FakeConnector({"a": "x"}))
    answers = AnswerCache(gateway, lambda probe, bodies: bodies[0], RecordCache(1_000_000))
    answers.put("q", "x", [Dependency(("a",), "same.v1", "x")])
    await answers.get("q")

    def add_echo(server):
        @server.tool()
        async def echo(text: str) -> str:
            return text

    server = build_server(gateway, tools=[add_echo], answers={"demo": answers})
    async with Client(server) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
        stats = (await client.call_tool("gateway_stats", {})).structured_content

    assert names == {"get_record", "gateway_stats", "echo"}
    demo = stats["answers"]["demo"]
    assert demo == {"entries": 1, "outcomes": {"match": 1}, "changed_probes": {}}

def make_llm_server(price=None):
    gateway, _ = make_server(FakeConnector({}))
    provider = FakeProvider()
    llm = InferenceGateway(
        provider,
        cache=RecordCache(1_000_000),
        policy=TTLPolicy(3600),
        price=price,
        clock=FakeClock(),
    )
    return build_server(gateway, inference=llm), provider


async def test_generate_tool_reuses_answers_and_reports_estimated_cost():
    server, provider = make_llm_server(ModelPrice(1.0, 2.0))
    async with Client(server) as client:
        first = (await client.call_tool("generate", HELLO)).structured_content
        second = (await client.call_tool("generate", HELLO)).structured_content
        stats = (await client.call_tool("gateway_stats", {})).structured_content

    assert (first["text"], first["source"], first["answer_kind"]) == (
        "an answer",
        "upstream",
        "generated",
    )
    assert second["source"] == "cache"
    assert provider.calls == 1
    counts = stats["inference"]["workloads"]["default"]
    assert (counts["calls"], counts["cache_hits"]) == (1, 1)
    assert counts["estimated_usd_spent"] == pytest.approx(5e-6)  # 1 input and 2 output tokens
    assert counts["estimated_usd_avoided"] == pytest.approx(5e-6)


async def test_local_model_reports_no_dollar_estimate():
    server, _ = make_llm_server(price=None)
    async with Client(server) as client:
        await client.call_tool("generate", HELLO)
        stats = (await client.call_tool("gateway_stats", {})).structured_content
    counts = stats["inference"]["workloads"]["default"]
    assert counts["estimated_usd_spent"] is None
    assert counts["prompt_tokens_spent"] == 1  # usage is still counted
    assert "generation_seconds_spent" in counts


async def test_generate_tool_passes_the_workload():
    server, provider = make_llm_server()
    async with Client(server) as client:
        await client.call_tool("generate", {**HELLO, "workload": "team-a"})
        other = await client.call_tool("generate", {**HELLO, "workload": "team-b"})
    assert other.structured_content["source"] == "upstream"
    assert provider.calls == 2


@pytest.mark.parametrize(
    "messages",
    [[{"role": "robot", "content": "hi"}], [{"role": "user"}]],
)
async def test_generate_tool_rejects_bad_requests(messages):
    server, provider = make_llm_server()
    result = await call(server, "generate", {"messages": messages, "max_tokens": 50})
    assert result.is_error is True
    assert "bad request" in result.content[0].text
    assert provider.calls == 0


async def test_generate_tool_reports_source_failures():
    server, provider = make_llm_server()
    provider.failures = [SourceError("provider down")]
    result = await call(server, "generate", HELLO)
    assert result.is_error is True
    assert "source error: provider down" in result.content[0].text