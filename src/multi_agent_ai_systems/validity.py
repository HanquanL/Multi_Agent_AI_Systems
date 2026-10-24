"""Validity policies: may a cached record still be served?

The default policy knows nothing about any domain. Domain policies (for example one that
tracks which facts an answer used) can plug in later and may answer UNKNOWN.
"""

from enum import Enum
from typing import Protocol

from multi_agent_ai_systems.connectors.base import Mutability, Record


class Validity(Enum):
    VALID = "valid"
    INVALID = "invalid"
    UNKNOWN = "unknown"  # no evidence either way; the caller's max_age decides


class ValidityPolicy(Protocol):
    def check(self, record: Record, age_seconds: float, mutability: Mutability) -> Validity: ...


class TTLPolicy:
    """Valid for ttl_seconds after it was cached. Immutable records never expire."""

    def __init__(self, ttl_seconds: float):
        if ttl_seconds < 0:
            raise ValueError("ttl_seconds must not be negative")
        self.ttl_seconds = ttl_seconds

    def check(self, record: Record, age_seconds: float, mutability: Mutability) -> Validity:
        if mutability is Mutability.IMMUTABLE:
            return Validity.VALID
        return Validity.VALID if age_seconds <= self.ttl_seconds else Validity.INVALID
