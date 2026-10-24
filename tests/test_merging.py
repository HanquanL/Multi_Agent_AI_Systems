import asyncio

from fakes import FakeClock, FakeConnector

from multi_agent_ai_systems.connectors.base import RateLimit
from multi_agent_ai_systems.merging import RequestMerger
from multi_agent_ai_systems.ratelimit import RateLimiter
from multi_agent_ai_systems.upstream import Upstream


async def test_concurrent_requests_share_one_fetch():
    conn = FakeConnector({"a": "x"})
    conn.gate = asyncio.Event()
    merger = RequestMerger(conn.fetch)

    tasks = [asyncio.create_task(merger.get("a")) for _ in range(10)]
    await asyncio.sleep(0)  # let every caller start waiting
    conn.gate.set()
    records = await asyncio.gather(*tasks)

    assert conn.calls == 1
    assert all(r is records[0] for r in records)


async def test_different_keys_fetch_separately():
    conn = FakeConnector({"a": "x", "b": "y"})
    merger = RequestMerger(conn.fetch)
    a, b = await asyncio.gather(merger.get("a"), merger.get("b"))
    assert (a.data, b.data) == ("x", "y")
    assert conn.calls == 2


async def test_merger_does_not_cache():
    conn = FakeConnector({"a": "x"})
    merger = RequestMerger(conn.fetch)
    await merger.get("a")
    await merger.get("a")
    assert conn.calls == 2


async def test_inflight_table_is_empty_after_fetch():
    merger = RequestMerger(FakeConnector({"a": "x"}).fetch)
    await merger.get("a")
    assert merger._inflight == {}


async def test_merged_requests_use_one_limiter_slot():
    clock = FakeClock()
    conn = FakeConnector({"a": "x"})
    conn.gate = asyncio.Event()
    limiter = RateLimiter(RateLimit(max_requests=10, period_seconds=1.0), clock=clock)
    upstream = Upstream(conn, limiter, clock=clock)
    merger = RequestMerger(upstream.fetch)

    tasks = [asyncio.create_task(merger.get("a")) for _ in range(10)]
    await asyncio.sleep(0)
    conn.gate.set()
    await asyncio.gather(*tasks)

    assert upstream.attempts == 1
    assert len(limiter._admitted) == 1
