"""Test doubles shared by several test files."""

import asyncio
from datetime import UTC, datetime

from multi_agent_ai_systems.connectors.base import (
    Capabilities,
    ChangeDiscovery,
    Connector,
    Mutability,
    NotFoundError,
    RateLimit,
    Record,
    Versioning,
)


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


class FakeConnector(Connector):
    """In-memory connector. Tests can hold fetches open and inject failures."""

    name = "fake"
    capabilities = Capabilities(
        change_discovery=ChangeDiscovery.POLL,
        versioning=Versioning.VERSION_ID,
        rate_limit=RateLimit(max_requests=10, period_seconds=1.0),
    )

    def __init__(self, data: dict[str, str] | None = None):
        self.data = dict(data or {})
        self.calls = 0
        self.gate: asyncio.Event | None = None  # if set, fetch waits until the test opens it
        self.failures: list[Exception] = []  # raised one per call before succeeding
        self.versions: dict[str, str] = {}  # per-key version; default "v1"
        self.immutable: set[str] = set()  # keys reported as immutable

    def mutability(self, key: str) -> Mutability:
        return Mutability.IMMUTABLE if key in self.immutable else Mutability.OVERWRITTEN

    async def fetch(self, key: str) -> Record:
        self.calls += 1
        if self.gate is not None:
            await self.gate.wait()
        if self.failures:
            raise self.failures.pop(0)
        if key not in self.data:
            raise NotFoundError(key)
        value = self.data[key]
        return Record(
            key=key,
            data=value,
            version=self.versions.get(key, "v1"),
            size_bytes=len(value),
            fetched_at=datetime.now(UTC),
        )
