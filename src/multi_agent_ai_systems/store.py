"""Persistent record store in SQLite: the second cache tier, below the in-memory cache.

Records fetched today are still there after a restart, so they can be reused tomorrow
without a new upstream request, if the validity policy still allows it. Like the memory
cache, it holds disposable source data only and stays within a byte budget.

Times are wall-clock seconds (time.time), because a monotonic clock restarts with the
process. Calls are synchronous: SQLite is fast for single-key reads and writes, and the
gateway only writes once per upstream fetch.
"""

import json
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from multi_agent_ai_systems.connectors.base import Record

_SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    key TEXT PRIMARY KEY,
    encoding TEXT NOT NULL,     -- 'bytes' or 'json'
    data BLOB NOT NULL,
    version TEXT,
    size_bytes INTEGER NOT NULL,
    fetched_at TEXT NOT NULL,   -- ISO 8601, from the source fetch
    stored_at REAL NOT NULL,    -- wall-clock seconds when the gateway stored it
    last_used REAL NOT NULL     -- wall-clock seconds, for least-recently-used eviction
)
"""


@dataclass
class StoreStats:
    evictions: int = 0
    evicted_bytes: int = 0
    too_large: int = 0  # records not stored because they exceed the low watermark
    unstorable: int = 0  # records whose data is neither bytes nor JSON-serializable


@dataclass(frozen=True)
class StoredRecord:
    record: Record
    age_seconds: float  # wall-clock time since the gateway stored it


class SQLiteRecordStore:
    """Keeps records on disk within a byte budget, evicting the least recently used.

    Uses the same watermarks as RecordCache: when an insert would push usage above the
    high watermark, entries are evicted until usage plus the new record fits under the
    low watermark. Budgets count each record's size_bytes, like the memory cache.
    """

    def __init__(
        self,
        path: str | Path,
        capacity_bytes: int,
        *,
        high_watermark: float = 0.9,
        low_watermark: float = 0.8,
        wall_clock: Callable[[], float] = time.time,
    ):
        if capacity_bytes <= 0:
            raise ValueError("capacity_bytes must be positive")
        if not 0 < low_watermark <= high_watermark <= 1:
            raise ValueError("need 0 < low_watermark <= high_watermark <= 1")
        self.capacity_bytes = capacity_bytes
        self.high_limit = int(capacity_bytes * high_watermark)
        self.low_limit = int(capacity_bytes * low_watermark)
        self._wall = wall_clock
        self._db = sqlite3.connect(path)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(_SCHEMA)
        self._db.commit()
        self.used_bytes = self._db.execute(
            "SELECT COALESCE(SUM(size_bytes), 0) FROM records"
        ).fetchone()[0]
        self.stats = StoreStats()

    def __len__(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM records").fetchone()[0]

    @property
    def occupancy(self) -> float:
        return self.used_bytes / self.capacity_bytes

    def get(self, key: str) -> StoredRecord | None:
        row = self._db.execute(
            "SELECT encoding, data, version, size_bytes, fetched_at, stored_at"
            " FROM records WHERE key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        encoding, blob, version, size_bytes, fetched_at, stored_at = row
        now = self._wall()
        self._db.execute("UPDATE records SET last_used = ? WHERE key = ?", (now, key))
        self._db.commit()
        data = blob if encoding == "bytes" else json.loads(blob)
        record = Record(
            key=key,
            data=data,
            version=version,
            size_bytes=size_bytes,
            fetched_at=datetime.fromisoformat(fetched_at),
        )
        return StoredRecord(record, max(now - stored_at, 0.0))

    def put(self, record: Record) -> bool:
        """Store record, replacing any entry with the same key. Returns False if it was
        too large or its data cannot be stored."""
        encoded = _encode(record.data)
        if encoded is None:
            self.stats.unstorable += 1
            return False
        self.remove(record.key)
        if record.size_bytes > self.low_limit:
            self.stats.too_large += 1
            return False
        if self.used_bytes + record.size_bytes > self.high_limit:
            self._evict_until(self.low_limit - record.size_bytes)
        now = self._wall()
        encoding, blob = encoded
        self._db.execute(
            "INSERT INTO records VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.key,
                encoding,
                blob,
                record.version,
                record.size_bytes,
                record.fetched_at.isoformat(),
                now,
                now,
            ),
        )
        self._db.commit()
        self.used_bytes += record.size_bytes
        return True

    def remove(self, key: str) -> None:
        """Drop an entry, e.g. on invalidation. Not counted as an eviction."""
        row = self._db.execute("SELECT size_bytes FROM records WHERE key = ?", (key,)).fetchone()
        if row is not None:
            self._db.execute("DELETE FROM records WHERE key = ?", (key,))
            self._db.commit()
            self.used_bytes -= row[0]

    def close(self) -> None:
        self._db.close()

    def _evict_until(self, target_bytes: int) -> None:
        while self.used_bytes > target_bytes:
            key, size = self._db.execute(
                "SELECT key, size_bytes FROM records ORDER BY last_used, rowid LIMIT 1"
            ).fetchone()
            self.remove(key)
            self.stats.evictions += 1
            self.stats.evicted_bytes += size


def _encode(data: object) -> tuple[str, bytes] | None:
    if isinstance(data, bytes):
        return "bytes", data
    try:
        return "json", json.dumps(data).encode()
    except (TypeError, ValueError):
        return None
