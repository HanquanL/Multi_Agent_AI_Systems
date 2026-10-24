"""Global upstream rate limiter.

Sliding window: at most max_requests are admitted in any window of period_seconds.
A token bucket was rejected because it can admit up to twice the limit in one window
(docs/challenges.md, C3).
"""

import asyncio
from collections import deque

from multi_agent_ai_systems.clock import Clock, RealClock
from multi_agent_ai_systems.connectors.base import RateLimit


class LimiterClosedError(RuntimeError):
    """The limiter was shut down and admits no more requests."""


class RateLimiter:
    """The one upstream limiter shared by every agent and every attempt, retries included.

    Callers queue in arrival order. A caller uses capacity only when admitted, right before
    it sends its request; queued callers use none. A queued caller that is cancelled or times
    out (e.g. via asyncio.wait_for) leaves without using capacity. After close(), queued and
    new callers get LimiterClosedError.
    """

    def __init__(
        self,
        limit: RateLimit,
        *,
        safety_factor: float = 1.0,
        clock: Clock | None = None,
    ):
        if not 0 < safety_factor <= 1:
            raise ValueError("safety_factor must be in (0, 1]")
        self.max_requests = max(1, int(limit.max_requests * safety_factor))
        self.period = limit.period_seconds
        self._clock = clock or RealClock()
        self._admitted: deque[float] = deque()
        self._lock = asyncio.Lock()
        self._closed = False

    def close(self) -> None:
        self._closed = True

    async def acquire(self) -> None:
        """Wait until one more request fits in the window, then record it."""
        async with self._lock:
            while True:
                if self._closed:
                    raise LimiterClosedError("rate limiter is closed")
                now = self._clock.now()
                while self._admitted and self._admitted[0] <= now - self.period:
                    self._admitted.popleft()
                if len(self._admitted) < self.max_requests:
                    self._admitted.append(now)
                    return
                await self._clock.sleep(self._admitted[0] + self.period - now)
