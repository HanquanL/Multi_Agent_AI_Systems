"""Byte-bounded cache of fetched records, with a replaceable eviction policy.

Holds disposable source data only. Durable data (config, quotas, experiment results)
never goes in here, so eviction can't remove it.
"""

from collections import OrderedDict
from dataclasses import dataclass
from typing import Protocol

from multi_agent_ai_systems.connectors.base import Record


class EvictionPolicy(Protocol):
    """Decides which entry to evict. The cache reports every insert, read and removal."""

    def inserted(self, key: str) -> None: ...

    def accessed(self, key: str) -> None: ...

    def removed(self, key: str) -> None: ...

    def victim(self) -> str:
        """The key to evict next. Only called when the cache is not empty."""
        ...


class LRUPolicy:
    """Evict the least recently used entry."""

    def __init__(self) -> None:
        self._order: OrderedDict[str, None] = OrderedDict()

    def inserted(self, key: str) -> None:
        self._order[key] = None
        self._order.move_to_end(key)

    def accessed(self, key: str) -> None:
        self._order.move_to_end(key)

    def removed(self, key: str) -> None:
        self._order.pop(key, None)

    def victim(self) -> str:
        return next(iter(self._order))


class FIFOPolicy(LRUPolicy):
    """Evict the oldest entry, however often it is read."""

    def accessed(self, key: str) -> None:
        pass


@dataclass
class CacheStats:
    evictions: int = 0
    evicted_bytes: int = 0
    too_large: int = 0  # records not cached because they exceed the low watermark


class RecordCache:
    """Keeps records within a byte budget.

    When an insert would push usage above the high watermark, entries are evicted until
    usage plus the new record fits under the low watermark. Usage never exceeds the high
    watermark, so it never reaches the hard capacity. Records larger than the low
    watermark are not cached at all.
    """

    def __init__(
        self,
        capacity_bytes: int,
        *,
        high_watermark: float = 0.9,
        low_watermark: float = 0.8,
        policy: EvictionPolicy | None = None,
    ):
        if capacity_bytes <= 0:
            raise ValueError("capacity_bytes must be positive")
        if not 0 < low_watermark <= high_watermark <= 1:
            raise ValueError("need 0 < low_watermark <= high_watermark <= 1")
        self.capacity_bytes = capacity_bytes
        self.high_limit = int(capacity_bytes * high_watermark)
        self.low_limit = int(capacity_bytes * low_watermark)
        self._policy = policy or LRUPolicy()
        self._entries: dict[str, Record] = {}
        self.used_bytes = 0
        self.stats = CacheStats()

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, key: str) -> bool:
        return key in self._entries

    @property
    def occupancy(self) -> float:
        return self.used_bytes / self.capacity_bytes

    def get(self, key: str) -> Record | None:
        record = self._entries.get(key)
        if record is not None:
            self._policy.accessed(key)
        return record

    def put(self, record: Record) -> bool:
        """Store record, replacing any entry with the same key. Returns False if too large."""
        self.remove(record.key)
        if record.size_bytes > self.low_limit:
            self.stats.too_large += 1
            return False
        if self.used_bytes + record.size_bytes > self.high_limit:
            self._evict_until(self.low_limit - record.size_bytes)
        self._entries[record.key] = record
        self.used_bytes += record.size_bytes
        self._policy.inserted(record.key)
        return True

    def remove(self, key: str) -> None:
        """Drop an entry, e.g. on invalidation. Not counted as an eviction."""
        record = self._entries.pop(key, None)
        if record is not None:
            self.used_bytes -= record.size_bytes
            self._policy.removed(key)

    def _evict_until(self, target_bytes: int) -> None:
        while self.used_bytes > target_bytes:
            key = self._policy.victim()
            size = self._entries[key].size_bytes
            self.remove(key)
            self.stats.evictions += 1
            self.stats.evicted_bytes += size
