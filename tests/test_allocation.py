import asyncio

import pytest
from fakes import FakeClock

from multi_agent_ai_systems.allocation import DEFAULT_WORKLOAD, QuotaFIFO
from multi_agent_ai_systems.connectors.base import RateLimit
from multi_agent_ai_systems.ratelimit import RateLimiter

ONE_PER_SECOND = RateLimit(max_requests=1, period_seconds=1.0)


async def admissions(
    limiter: RateLimiter, clock: FakeClock, workloads: list[str]
) -> list[tuple[float, str]]:
    """Queue one request per workload name, in list order; return (time, workload) admitted."""
    admitted: list[tuple[float, str]] = []

    async def worker(workload: str):
        await limiter.acquire(workload)
        admitted.append((clock.now(), workload))

    await asyncio.gather(*(worker(w) for w in workloads))
    return admitted


async def test_plain_fifo_without_quotas():
    clock = FakeClock()
    limiter = RateLimiter(ONE_PER_SECOND, clock=clock)
    got = await admissions(limiter, clock, ["A", "B", "A", "B"])
    assert got == [(0.0, "A"), (1.0, "B"), (2.0, "A"), (3.0, "B")]


async def test_quotas_split_each_window():
    clock = FakeClock()
    policy = QuotaFIFO({"A": 2, "B": 2})
    limiter = RateLimiter(RateLimit(max_requests=4, period_seconds=1.0), clock=clock, policy=policy)
    got = await admissions(limiter, clock, ["A"] * 8 + ["B"] * 8)  # all of A arrives first

    for second in range(4):
        window = [w for t, w in got if t == second]
        assert sorted(window) == ["A", "A", "B", "B"]
    assert clock.now() == 3.0


async def test_idle_capacity_is_borrowed():
    clock = FakeClock()
    policy = QuotaFIFO({"A": 1, "B": 3})
    limiter = RateLimiter(RateLimit(max_requests=4, period_seconds=1.0), clock=clock, policy=policy)
    got = await admissions(limiter, clock, ["A"] * 4)  # B is idle

    assert [t for t, _ in got] == [0.0] * 4


async def test_unregistered_workloads_share_the_default_bucket():
    clock = FakeClock()
    policy = QuotaFIFO({"A": 2}, default_quota=1)
    limiter = RateLimiter(RateLimit(max_requests=3, period_seconds=1.0), clock=clock, policy=policy)
    got = await admissions(limiter, clock, ["X", "Y", "A", "A"])

    assert policy.bucket("X") == policy.bucket("Y") == DEFAULT_WORKLOAD
    assert sorted(got) == [(0.0, "A"), (0.0, "A"), (0.0, "X"), (1.0, "Y")]


async def test_stats_per_workload():
    clock = FakeClock()
    policy = QuotaFIFO({"A": 1, "B": 1})
    limiter = RateLimiter(ONE_PER_SECOND, clock=clock, policy=policy)
    await admissions(limiter, clock, ["A", "B"])

    assert limiter.stats["A"].admitted == 1
    assert limiter.stats["A"].max_wait == 0.0
    assert limiter.stats["B"].admitted == 1
    assert limiter.stats["B"].max_wait == 1.0


@pytest.mark.parametrize("kwargs", [{"quotas": {"A": 0}}, {"quotas": {"A": 1}, "default_quota": 0}])
def test_bad_quotas_rejected(kwargs):
    with pytest.raises(ValueError):
        QuotaFIFO(**kwargs)
