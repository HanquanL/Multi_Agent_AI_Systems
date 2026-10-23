"""Capability-based interface that every data source connector implements.
The gateway core talks to sources only through this interface, so nothing in the core
depends on how one particular API works.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any


class ChangeDiscovery(Enum):
    """How gateway can learn that source data changed."""

    POLL = "poll"  # we have to ask
    FEED = "feed"  # the source pushes change events


class Versioning(Enum):
    """what the source tells us about record versions."""

    VERSION_ID = "version_id"  # records will carry an ID that changes when they change
    TIMESTAMP_ONLY = "timestamp_only"  # last-modified time


class Mutability(Enum):
    """whether a record can change after it is published."""

    IMMUTABLE = "immutable"
    OVERWRITTEN = "overwritten"


@dataclass(frozen=True)
class RateLimit:
    """At most max_requests per period_seconds, covers per-second and daily limits."""

    max_requests: int
    period_seconds: float


@dataclass(frozen=True)
class Capabilities:
    change_discovery: ChangeDiscovery
    versioning: Versioning
    rate_limit: RateLimit


@dataclass(frozen=True)
class Record:
    """One item fetched from a source"""

    key: str
    data: Any
    version: str | None  # source version ID
    size_bytes: int  # size of the response body, the cache budgets storage by this.
    fetched_at: datetime


class SourceError(Exception):
    """The source failed to return a record"""


class NotFoundError(SourceError):
    """The source has no record for this key"""


class ThrottledError(SourceError):
    """the source rejected the request for going over its rate limit"""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class Connector(ABC):
    """A data source the gateway can fetch from.
    connectors don't rate-limit, cache, or merge requests. The gateway
    does that, using the capabilities each connector declares."""

    name: str
    capabilities: Capabilities

    @abstractmethod
    def mutability(self, key: str) -> Mutability:
        """say whether the record behind key can change after it is published."""

    @abstractmethod
    async def fetch(self, key: str) -> Record:
        """Fetch key with at most one upstream request. Never retry; the gateway decides that."""
