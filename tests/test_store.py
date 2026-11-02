from datetime import UTC, datetime

import pytest
from fakes import FakeClock, FakeConnector

from multi_agent_ai_systems.cache import RecordCache
from multi_agent_ai_systems.connectors.base import Record
from multi_agent_ai_systems.gateway import FetchReason, Gateway
from multi_agent_ai_systems.store import SQLiteRecordStore
from multi_agent_ai_systems.validity import TTLPolicy

HOUR = 3600.0


class WallClock:
    """A settable stand-in for time.time()."""

    def __init__(self, start: float = 1_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now


def record(key: str, data, size: int = 10, version: str | None = "v1") -> Record:
    return Record(
        key=key, data=data, version=version, size_bytes=size, fetched_at=datetime.now(UTC)
    )


def make_gateway(conn, store, *, ttl=HOUR, cache_bytes=1_000_000):
    return Gateway(
        conn,
        cache=RecordCache(cache_bytes),
        policy=TTLPolicy(ttl),
        store=store,
        clock=FakeClock(),
    )


# --- the store on its own ---


def test_round_trips_json_and_bytes(tmp_path):
    store = SQLiteRecordStore(tmp_path / "g.db", 1_000_000)
    store.put(record("j", {"a": [1, 2], "b": "x"}))
    store.put(record("b", b"\x00\xffraw"))

    assert store.get("j").record.data == {"a": [1, 2], "b": "x"}
    assert store.get("b").record.data == b"\x00\xffraw"
    assert store.get("j").record.version == "v1"
    assert store.get("missing") is None


def test_age_comes_from_the_wall_clock(tmp_path):
    wall = WallClock()
    store = SQLiteRecordStore(tmp_path / "g.db", 1_000_000, wall_clock=wall)
    store.put(record("a", "x"))
    wall.now += 90
    assert store.get("a").age_seconds == 90


def test_unstorable_data_is_skipped(tmp_path):
    store = SQLiteRecordStore(tmp_path / "g.db", 1_000_000)
    assert store.put(record("a", object())) is False
    assert store.stats.unstorable == 1
    assert len(store) == 0


def test_evicts_least_recently_used_within_budget(tmp_path):
    wall = WallClock()
    store = SQLiteRecordStore(tmp_path / "g.db", 100, wall_clock=wall)  # high 90, low 80
    for key in "abcd":
        wall.now += 1
        store.put(record(key, "x", size=20))  # 80 bytes used
    wall.now += 1
    store.get("a")  # a is now the most recently used
    wall.now += 1
    store.put(record("e", "x", size=20))  # 100 > 90: evict down to 60, then insert

    assert store.get("b") is None  # least recently used; a was read after it
    assert all(store.get(key) is not None for key in "acde")
    assert store.used_bytes == 80
    assert store.stats.evictions == 1


def test_contents_and_usage_survive_reopening(tmp_path):
    path = tmp_path / "g.db"
    store = SQLiteRecordStore(path, 1_000_000)
    store.put(record("a", "x", size=10))
    store.put(record("b", "y", size=15))
    store.close()

    reopened = SQLiteRecordStore(path, 1_000_000)
    assert len(reopened) == 2
    assert reopened.used_bytes == 25
    assert reopened.get("b").record.data == "y"


# --- through the gateway ---


async def test_record_is_reused_after_a_restart(tmp_path):
    path, wall = tmp_path / "g.db", WallClock()
    first = make_gateway(
        FakeConnector({"a": "x"}), SQLiteRecordStore(path, 1_000_000, wall_clock=wall)
    )
    await first.get("a")
    first.store.close()

    wall.now += 600  # ten minutes later, a new process with an empty memory cache
    conn = FakeConnector({"a": "x"})
    second = make_gateway(conn, SQLiteRecordStore(path, 1_000_000, wall_clock=wall))
    response = await second.get("a")

    assert conn.calls == 0
    assert response.label.source == "store"
    assert response.label.age_seconds == 600
    assert second.stats.store_hits == 1


async def test_store_hit_is_promoted_to_memory_with_its_age(tmp_path):
    path, wall = tmp_path / "g.db", WallClock()
    SQLiteRecordStore(path, 1_000_000, wall_clock=wall).put(record("a", "x"))
    wall.now += 60
    gateway = make_gateway(FakeConnector(), SQLiteRecordStore(path, 1_000_000, wall_clock=wall))

    await gateway.get("a")
    again = await gateway.get("a")

    assert again.label.source == "cache"
    assert again.label.age_seconds == 60


async def test_ttl_still_applies_after_a_restart(tmp_path):
    path, wall = tmp_path / "g.db", WallClock()
    SQLiteRecordStore(path, 1_000_000, wall_clock=wall).put(record("a", "x"))
    wall.now += HOUR + 1  # a new day: stored, but expired
    conn = FakeConnector({"a": "x"})
    gateway = make_gateway(conn, SQLiteRecordStore(path, 1_000_000, wall_clock=wall))

    response = await gateway.get("a")

    assert conn.calls == 1
    assert response.label.source == "upstream"
    # the store remembered the old version, so the refetch is classified, not "first"
    assert gateway.stats.fetches == {FetchReason.REFETCH_UNCHANGED: 1}


async def test_store_serves_what_memory_evicted(tmp_path):
    conn = FakeConnector({"a": "x" * 50, "b": "y" * 50})
    store = SQLiteRecordStore(tmp_path / "g.db", 1_000_000)
    gateway = make_gateway(conn, store, cache_bytes=100)  # memory holds one of them

    await gateway.get("a")
    await gateway.get("b")  # evicts a from memory, not from the store
    response = await gateway.get("a")

    assert response.label.source == "store"
    assert conn.calls == 2


@pytest.mark.parametrize("capacity", [0, -1])
def test_bad_capacity_rejected(tmp_path, capacity):
    with pytest.raises(ValueError):
        SQLiteRecordStore(tmp_path / "g.db", capacity)
