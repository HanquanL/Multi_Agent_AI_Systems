"""The gateway for one data source: cache, freshness labels, request merging, rate limiting.

get(key) serves from the cache when the validity policy (and the caller's max_age) allows
it. Otherwise it makes one merged, rate-limited upstream fetch and caches the result.
Every response carries a freshness label.
"""

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from multi_agent_ai_systems.cache import RecordCache
from multi_agent_ai_systems.clock import Clock, RealClock
from multi_agent_ai_systems.connectors.base import Connector, Record
from multi_agent_ai_systems.merging import RequestMerger
from multi_agent_ai_systems.ratelimit import RateLimiter
from multi_agent_ai_systems.upstream import Upstream
from multi_agent_ai_systems.validity import Validity, ValidityPolicy


@dataclass(frozen=True)
class FreshnessLabel:
    key: str
    version: str | None  # source version, e.g. the filing the data came from
    fetched_at: datetime  # when the source returned this data (UTC)
    age_seconds: float  # how long the gateway has held it; 0 if just fetched
    source: str  # "cache" or "upstream"


@dataclass(frozen=True)
class Response:
    data: Any
    label: FreshnessLabel


class FetchReason(Enum):
    """Why an upstream fetch happened. Feeds the request profiling."""

    FIRST = "first"  # never fetched before
    EVICTED = "evicted"  # was cached, then evicted for space
    UNCACHEABLE = "uncacheable"  # too large to cache last time
    REFETCH_UNCHANGED = "refetch_unchanged"  # expired, and the version did not change
    REFETCH_CHANGED = "refetch_changed"  # expired, and the version changed
    REFETCH_UNKNOWN = "refetch_unknown"  # expired; no version to compare


@dataclass
class GatewayStats:
    hits: int = 0
    misses: int = 0
    fetches: Counter[FetchReason] = field(default_factory=Counter)


class Gateway:
    def __init__(
        self,
        connector: Connector,
        *,
        cache: RecordCache,
        policy: ValidityPolicy,
        safety_factor: float = 1.0,
        max_attempts: int = 1,
        clock: Clock | None = None,
    ):
        self._clock = clock or RealClock()
        self.connector = connector
        self.cache = cache
        self.policy = policy
        self.limiter = RateLimiter(
            connector.capabilities.rate_limit, safety_factor=safety_factor, clock=self._clock
        )
        self.upstream = Upstream(
            connector, self.limiter, max_attempts=max_attempts, clock=self._clock
        )
        self._merger = RequestMerger(self._fetch_and_store)
        self._stored_at: dict[str, float] = {}  # gateway-clock time each cached key was stored
        self._ever_cached: set[str] = set()
        self._uncacheable: set[str] = set()
        self.stats = GatewayStats()

    async def get(self, key: str, *, max_age: float | None = None) -> Response:
        record = self.cache.get(key)
        if record is not None:
            age = self._clock.now() - self._stored_at[key]
            verdict = self.policy.check(record, age, self.connector.mutability(key))
            if _servable(verdict, age, max_age):
                self.stats.hits += 1
                return Response(record.data, _label(record, age, "cache"))

        self.stats.misses += 1
        record = await self._merger.get(key)
        return Response(record.data, _label(record, 0.0, "upstream"))

    async def close(self) -> None:
        await self._merger.close()
        self.limiter.close()

    async def _fetch_and_store(self, key: str) -> Record:
        """Runs once per merged fetch, so each upstream fetch is classified exactly once."""
        old = self.cache.get(key)
        record = await self.upstream.fetch(key)
        self.stats.fetches[self._reason(key, old, record)] += 1
        if self.cache.put(record):
            self._stored_at[key] = self._clock.now()
            self._ever_cached.add(key)
            self._uncacheable.discard(key)
        else:
            self._uncacheable.add(key)
        return record

    def _reason(self, key: str, old: Record | None, new: Record) -> FetchReason:
        if old is not None:
            if old.version is None or new.version is None:
                return FetchReason.REFETCH_UNKNOWN
            if old.version == new.version:
                return FetchReason.REFETCH_UNCHANGED
            return FetchReason.REFETCH_CHANGED
        if key in self._uncacheable:
            return FetchReason.UNCACHEABLE
        if key in self._ever_cached:
            return FetchReason.EVICTED
        return FetchReason.FIRST


def _servable(verdict: Validity, age: float, max_age: float | None) -> bool:
    if max_age is not None and age > max_age:
        return False
    if verdict is Validity.VALID:
        return True
    if verdict is Validity.UNKNOWN:
        return max_age is not None  # the caller said how stale is acceptable
    return False


def _label(record: Record, age: float, source: str) -> FreshnessLabel:
    return FreshnessLabel(
        key=record.key,
        version=record.version,
        fetched_at=record.fetched_at,
        age_seconds=age,
        source=source,
    )
