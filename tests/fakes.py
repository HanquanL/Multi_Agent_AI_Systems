"""Test doubles shared by several test files."""


class FakeClock:
    """A clock that jumps forward instead of waiting.

    Only correct when one task sleeps at a time. That holds for the rate limiter,
    which sleeps under its lock. The replay engine will need a proper event-queue clock.
    """

    def __init__(self, start: float = 0.0):
        self._now = start

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds

    async def sleep(self, seconds: float) -> None:
        self._now += max(seconds, 0.0)
