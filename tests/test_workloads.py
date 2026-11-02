import argparse
import asyncio

import pytest
from fakes import FakeClock, FakeConnector
from mcp import Client

from multi_agent_ai_systems.__main__ import workload_quota
from multi_agent_ai_systems.allocation import DEFAULT_WORKLOAD, QuotaFIFO
from multi_agent_ai_systems.cache import RecordCache
from multi_agent_ai_systems.gateway import Gateway
from multi_agent_ai_systems.server import build_server
from multi_agent_ai_systems.validity import TTLPolicy


def make_gateway(conn: FakeConnector, quotas: dict[str, int], **kwargs) -> Gateway:
    return Gateway(
        conn,
        cache=RecordCache(1_000_000),
        policy=TTLPolicy(3600),
        allocation=QuotaFIFO(quotas),
        clock=FakeClock(),
        **kwargs,
    )


# --- who is charged ---


async def test_merged_fetch_is_charged_to_the_workload_that_started_it():
    conn = FakeConnector({"a": "x"})
    conn.gate = asyncio.Event()
    gateway = make_gateway(conn, {"A": 5, "B": 5, "C": 5})

    tasks = [asyncio.create_task(gateway.get("a", workload=w)) for w in ("A", "B", "C")]
    await asyncio.sleep(0)
    conn.gate.set()
    await asyncio.gather(*tasks)

    counts = gateway.stats.by_workload
    assert conn.calls == 1
    assert [counts[w].misses for w in "ABC"] == [1, 1, 1]
    assert [counts[w].fetches_started for w in "ABC"] == [1, 0, 0]
    assert gateway.limiter.stats["A"].admitted == 1
    assert "B" not in gateway.limiter.stats  # joining used none of B's share


async def test_cache_hits_are_counted_per_workload_and_use_no_share():
    gateway = make_gateway(FakeConnector({"a": "x"}), {"A": 5, "B": 5})
    await gateway.get("a", workload="A")
    await gateway.get("a", workload="B")

    assert gateway.stats.by_workload["B"].hits == 1
    assert gateway.stats.by_workload["B"].fetches_started == 0
    assert "B" not in gateway.limiter.stats


async def test_unregistered_workloads_are_counted_as_default():
    gateway = make_gateway(FakeConnector({"a": "x"}), {"A": 5})
    await gateway.get("a", workload="someone-else")

    assert set(gateway.stats.by_workload) == {DEFAULT_WORKLOAD}
    assert gateway.stats.by_workload[DEFAULT_WORKLOAD].fetches_started == 1


# --- quotas through the whole stack ---


async def test_quota_protects_a_workload_that_arrives_late():
    keys = {f"k{i}": "x" for i in range(12)}
    conn = FakeConnector(keys)  # 10 per second, 0.4 safety factor: 4 per second
    gateway = make_gateway(conn, {"A": 2, "B": 2}, safety_factor=0.4)

    a = [gateway.get(f"k{i}", workload="A") for i in range(8)]
    b = [gateway.get(f"k{i}", workload="B") for i in range(8, 12)]
    await asyncio.gather(*a, *b)  # all of A is queued before B

    # Plain FIFO would make B wait 2 s behind A; with quotas B waits at most 1 s.
    assert gateway.limiter.stats["B"].max_wait == 1.0
    # Once B is done (after t = 1), A borrows the whole limit: its last four go at t = 2.
    assert gateway.limiter.stats["A"].max_wait == 2.0
    assert conn.calls == 12


# --- MCP tools and command line ---


async def test_get_record_passes_workload_and_stats_report_it():
    gateway = make_gateway(FakeConnector({"a": "x"}), {"A": 5})
    server = build_server(gateway)
    async with Client(server) as client:
        await client.call_tool("get_record", {"key": "a", "workload": "A", "agent_id": "a-1"})
        await client.call_tool("get_record", {"key": "a"})
        stats = (await client.call_tool("gateway_stats", {})).structured_content

    assert stats["workloads"]["A"]["fetches_started"] == 1
    assert stats["workloads"]["A"]["limiter_admitted"] == 1
    assert stats["workloads"][DEFAULT_WORKLOAD]["hits"] == 1


def test_workload_quota_parsing():
    assert workload_quota("research=3") == ("research", 3)
    for bad in ["research", "=3", "research=0", "research=x"]:
        with pytest.raises(argparse.ArgumentTypeError):
            workload_quota(bad)
