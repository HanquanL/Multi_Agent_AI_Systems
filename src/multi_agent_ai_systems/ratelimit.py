"""Global upstream rate limiter.

Sliding window: at most max_requests are admitted in any window of period_seconds.
A token bucket was rejected because it can admit up to twice the limit in one window
(docs/challenges.md, C3). Which waiting request goes next is decided by an
AllocationPolicy (allocation.py); the default is plain FIFO.
"""

import asyncio
from collections import Counter, deque
from dataclasses import dataclass

from multi_agent_ai_systems.allocation import (
    DEFAULT_WORKLOAD,
    AllocationPolicy,
    QuotaFIFO,
    Waiter,
)
from multi_agent_ai_systems.clock import Clock, RealClock
from multi_agent_ai_systems.connectors.base import RateLimit


class LimiterClosedError(RuntimeError):
    """The limiter was shut down and admits no more requests."""


@dataclass
class WorkloadStats:
    admitted: int = 0
    total_wait: float = 0.0
    max_wait: float = 0.0


class RateLimiter:
    """The one upstream limiter shared by every agent and every attempt, retries included.

    Callers wait in a queue; one dispatcher task admits them when the window has room,
    in the order the policy picks. A caller uses capacity only when admitted, right before
    it sends its request; queued callers use none. A queued caller that is cancelled or
    times out (e.g. via asyncio.wait_for) leaves without using capacity. After close(),
    queued and new callers get LimiterClosedError.
    """

    def __init__(
        self,
        limit: RateLimit,
        *,
        safety_factor: float = 1.0,
        clock: Clock | None = None,
        policy: AllocationPolicy | None = None,
    ):
        if not 0 < safety_factor <= 1:
            raise ValueError("safety_factor must be in (0, 1]")
        self.max_requests = max(1, int(limit.max_requests * safety_factor))
        self.period = limit.period_seconds
        self.policy = policy or QuotaFIFO()
        self._clock = clock or RealClock()
        self._admitted: deque[tuple[float, str, int]] = deque()  # (time, bucket, cost)
        self._waiting: list[Waiter] = []
        self._dispatcher: asyncio.Task[None] | None = None
        self._closed = False
        self.stats: dict[str, WorkloadStats] = {}  # per bucket

    def close(self) -> None:
        self._closed = True
        for waiter in self._waiting:
            if not waiter.admitted.done():
                waiter.admitted.set_exception(LimiterClosedError("rate limiter is closed"))
        self._waiting.clear()
        if self._dispatcher is not None:
            self._dispatcher.cancel()

    async def acquire(
        self, workload: str = DEFAULT_WORKLOAD, agent: str = "anonymous", *, cost: int = 1
    ) -> None:
        """Wait until the policy admits this request into the window. cost is the capacity
        it uses, 1 for a plain request; it can be any unit, such as tokens."""
        if self._closed:
            raise LimiterClosedError("rate limiter is closed")
        if not 1 <= cost <= self.max_requests:
            raise ValueError(f"cost must be between 1 and {self.max_requests}, got {cost}")
        start = self._clock.now()
        waiter = Waiter(
            self.policy.bucket(workload),
            agent,
            asyncio.get_running_loop().create_future(),
            cost,
        )
        self._waiting.append(waiter)
        if self._dispatcher is None or self._dispatcher.done():
            self._dispatcher = asyncio.create_task(self._dispatch())
        try:
            await waiter.admitted
        except BaseException:
            if waiter in self._waiting:
                self._waiting.remove(waiter)
            raise
        wait = self._clock.now() - start
        stats = self.stats.setdefault(waiter.workload, WorkloadStats())
        stats.admitted += 1
        stats.total_wait += wait
        stats.max_wait = max(stats.max_wait, wait)

    async def _dispatch(self) -> None:
        # The only task that admits callers or sleeps on the window. Callers never hold a
        # lock, so an admitted caller is never stuck behind a waiting one.
        while True:
            self._waiting = [w for w in self._waiting if not w.admitted.done()]
            if not self._waiting:
                return
            now = self._clock.now()
            while self._admitted and self._admitted[0][0] <= now - self.period:
                self._admitted.popleft()
            used = sum(cost for _, _, cost in self._admitted)
            if used < self.max_requests:
                usage: Counter[str] = Counter()
                for _, bucket, cost in self._admitted:
                    usage[bucket] += cost
                waiter = self.policy.choose(self._waiting, usage)
                if used + waiter.cost <= self.max_requests:  # otherwise it keeps its place
                    self._waiting.remove(waiter)
                    self._admitted.append((now, waiter.workload, waiter.cost))
                    waiter.admitted.set_result(None)
                    continue
            await self._clock.sleep(self._admitted[0][0] + self.period - now)
