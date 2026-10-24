import asyncio

import pytest
from fakes import FakeClock

from multi_agent_ai_systems.connectors.base import RateLimit
from multi_agent_ai_systems.ratelimit import LimiterClosedError, RateLimiter

TEN_PER_SECOND = RateLimit(max_requests=10, period_seconds=1.0)
ONE_PER_SECOND = RateLimit(max_requests=1, period_seconds=1.0)


def max_in_any_window(times: list[float], period: float) -> int:
    """Largest number of timestamps inside any window [t, t + period)."""
    return max(sum(1 for u in times if t <= u < t + period) for t in times)


async def run_workers(limiter: RateLimiter, clock: FakeClock, n: int) -> list[float]:
    times: list[float] = []

    async def worker():
        await limiter.acquire()
        times.append(clock.now())

    await asyncio.gather(*(worker() for _ in range(n)))
    return times


# --- the gate ---


async def test_first_requests_pass_immediately():
    clock = FakeClock()
    limiter = RateLimiter(TEN_PER_SECOND, clock=clock)
    for _ in range(10):
        await limiter.acquire()
    assert clock.now() == 0.0


async def test_next_request_waits_for_the_window():
    clock = FakeClock()
    limiter = RateLimiter(TEN_PER_SECOND, clock=clock)
    for _ in range(11):
        await limiter.acquire()
    assert clock.now() == 1.0


async def test_never_more_than_limit_in_any_window():
    clock = FakeClock()
    times = await run_workers(RateLimiter(TEN_PER_SECOND, clock=clock), clock, 55)
    assert len(times) == 55
    assert max_in_any_window(times, 1.0) == 10


async def test_simulated_schedule_is_deterministic():
    runs = []
    for _ in range(2):
        clock = FakeClock()
        runs.append(await run_workers(RateLimiter(TEN_PER_SECOND, clock=clock), clock, 30))
    assert runs[0] == runs[1]


# --- rolling-window boundaries ---


async def test_admitted_exactly_when_window_ends():
    clock = FakeClock()
    limiter = RateLimiter(ONE_PER_SECOND, clock=clock)
    await limiter.acquire()  # t = 0
    clock.advance(1.0)
    await limiter.acquire()  # the t = 0 admission just left the window: no wait
    assert clock.now() == 1.0


async def test_waits_when_just_inside_window():
    clock = FakeClock()
    limiter = RateLimiter(ONE_PER_SECOND, clock=clock)
    await limiter.acquire()  # t = 0
    clock.advance(0.999)
    await limiter.acquire()  # must wait until t = 1.0
    assert clock.now() == pytest.approx(1.0)


# --- ordering and configuration ---


async def test_admits_in_arrival_order():
    limiter = RateLimiter(ONE_PER_SECOND, clock=FakeClock())
    order: list[int] = []

    async def worker(i: int):
        await limiter.acquire()
        order.append(i)

    await asyncio.gather(*(worker(i) for i in range(5)))
    assert order == [0, 1, 2, 3, 4]


def test_safety_factor_lowers_the_limit():
    assert RateLimiter(TEN_PER_SECOND, safety_factor=0.8).max_requests == 8


@pytest.mark.parametrize("factor", [0, -0.5, 1.5])
def test_bad_safety_factor_rejected(factor):
    with pytest.raises(ValueError):
        RateLimiter(TEN_PER_SECOND, safety_factor=factor)


# --- cancellation, timeout, shutdown (real clock, short times: callers must really block) ---


async def test_cancelled_waiter_uses_no_capacity():
    limiter = RateLimiter(RateLimit(max_requests=1, period_seconds=0.1))
    await limiter.acquire()
    waiter = asyncio.create_task(limiter.acquire())
    await asyncio.sleep(0.01)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    assert len(limiter._admitted) == 1  # the cancelled waiter took no slot
    await asyncio.wait_for(limiter.acquire(), timeout=1.0)  # and the queue still moves


async def test_timed_out_waiter_uses_no_capacity():
    limiter = RateLimiter(RateLimit(max_requests=1, period_seconds=0.1))
    await limiter.acquire()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(limiter.acquire(), timeout=0.01)

    assert len(limiter._admitted) == 1  # the timed-out waiter took no slot
    await asyncio.wait_for(limiter.acquire(), timeout=1.0)


async def test_close_rejects_new_callers():
    limiter = RateLimiter(ONE_PER_SECOND, clock=FakeClock())
    limiter.close()
    with pytest.raises(LimiterClosedError):
        await limiter.acquire()


async def test_close_rejects_queued_callers():
    limiter = RateLimiter(RateLimit(max_requests=1, period_seconds=0.1))
    await limiter.acquire()
    queued = [asyncio.create_task(limiter.acquire()) for _ in range(3)]
    await asyncio.sleep(0.01)
    limiter.close()
    results = await asyncio.gather(*queued, return_exceptions=True)
    assert all(isinstance(r, LimiterClosedError) for r in results)
