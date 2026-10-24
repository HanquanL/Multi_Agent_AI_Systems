import asyncio

import pytest
from fakes import FakeClock, FakeConnector

from multi_agent_ai_systems.connectors.base import RateLimit, SourceError, ThrottledError
from multi_agent_ai_systems.merging import RequestMerger
from multi_agent_ai_systems.ratelimit import RateLimiter
from multi_agent_ai_systems.upstream import Upstream

# --- request merging under failure ---


async def test_failure_reaches_every_waiter_then_next_request_retries():
    conn = FakeConnector({"a": "x"})
    conn.gate = asyncio.Event()
    conn.failures = [SourceError("boom")]
    merger = RequestMerger(conn.fetch)

    tasks = [asyncio.create_task(merger.get("a")) for _ in range(5)]
    await asyncio.sleep(0)
    conn.gate.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)

    assert all(isinstance(r, SourceError) for r in results)
    assert conn.calls == 1
    assert merger._inflight == {}  # the failed fetch did not stick

    record = await merger.get("a")  # a fresh fetch, not the old failure
    assert record.data == "x"
    assert conn.calls == 2


async def test_cancelling_first_caller_does_not_cancel_the_fetch():
    conn = FakeConnector({"a": "x"})
    conn.gate = asyncio.Event()
    merger = RequestMerger(conn.fetch)

    first = asyncio.create_task(merger.get("a"))
    others = [asyncio.create_task(merger.get("a")) for _ in range(3)]
    await asyncio.sleep(0)
    first.cancel()
    conn.gate.set()

    records = await asyncio.gather(*others)
    assert all(r.data == "x" for r in records)
    assert conn.calls == 1
    with pytest.raises(asyncio.CancelledError):
        await first


async def test_timed_out_caller_leaves_fetch_running_for_others():
    conn = FakeConnector({"a": "x"})
    conn.gate = asyncio.Event()
    merger = RequestMerger(conn.fetch)

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(merger.get("a"), timeout=0.01)

    late = asyncio.create_task(merger.get("a"))  # joins the same in-flight fetch
    await asyncio.sleep(0)
    conn.gate.set()
    assert (await late).data == "x"
    assert conn.calls == 1


async def test_close_cancels_inflight_fetches():
    conn = FakeConnector({"a": "x"})
    conn.gate = asyncio.Event()
    merger = RequestMerger(conn.fetch)

    waiter = asyncio.create_task(merger.get("a"))
    await asyncio.sleep(0)
    await merger.close()
    with pytest.raises(asyncio.CancelledError):
        await waiter


# --- retries in Upstream ---


def make_upstream(conn: FakeConnector, limit: RateLimit, **kwargs) -> tuple[Upstream, FakeClock]:
    clock = FakeClock()
    limiter = RateLimiter(limit, clock=clock)
    return Upstream(conn, limiter, clock=clock, **kwargs), clock


async def test_every_retry_passes_the_limiter():
    conn = FakeConnector({"a": "x"})
    conn.failures = [SourceError("1"), SourceError("2")]
    upstream, clock = make_upstream(
        conn, RateLimit(max_requests=1, period_seconds=1.0), max_attempts=3, backoff_seconds=0
    )

    record = await upstream.fetch("a")
    assert record.data == "x"
    assert conn.calls == 3
    assert clock.now() == 2.0  # one slot per second: three attempts need two seconds


async def test_missing_record_is_not_retried():
    conn = FakeConnector({})
    upstream, _ = make_upstream(
        conn, RateLimit(max_requests=10, period_seconds=1.0), max_attempts=3
    )
    with pytest.raises(SourceError):
        await upstream.fetch("missing")
    assert conn.calls == 1


async def test_throttled_retry_waits_retry_after():
    conn = FakeConnector({"a": "x"})
    conn.failures = [ThrottledError("slow down", retry_after=5.0)]
    upstream, clock = make_upstream(
        conn, RateLimit(max_requests=10, period_seconds=1.0), max_attempts=2
    )

    await upstream.fetch("a")
    assert clock.now() == 5.0


async def test_gives_up_after_max_attempts():
    conn = FakeConnector({"a": "x"})
    conn.failures = [SourceError("1"), SourceError("2"), SourceError("3")]
    upstream, _ = make_upstream(
        conn, RateLimit(max_requests=10, period_seconds=1.0), max_attempts=3
    )
    with pytest.raises(SourceError):
        await upstream.fetch("a")
    assert conn.calls == 3
