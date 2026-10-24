"""Time source for the gateway, so tests and replay can run without real waiting."""

import asyncio
import time
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float:
        """seconds on a monotonic clock, high-resolution clock."""
        ...

    async def sleep(self, seconds: float) -> None: ...


class RealClock:
    def now(self) -> float:
        return time.perf_counter()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
