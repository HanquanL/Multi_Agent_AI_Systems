import asyncio
from datetime import UTC, datetime

from fakes import FakeClock, FakeConnector
from mcp import Client

from multi_agent_ai_systems.cache import RecordCache
from multi_agent_ai_systems.connectors.base import SourceError
from multi_agent_ai_systems.gateway import FreshnessLabel, Gateway, Response
from multi_agent_ai_systems.server import build_server, to_payload
from multi_agent_ai_systems.validity import TTLPolicy


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
