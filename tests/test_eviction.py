import random
from datetime import UTC, datetime

import pytest

from multi_agent_ai_systems.cache import FIFOPolicy, RecordCache
from multi_agent_ai_systems.connectors.base import Record


def rec(key: str, size: int) -> Record:
    return Record(key=key, data=None, version=None, size_bytes=size, fetched_at=datetime.now(UTC))


def test_put_and_get():
    cache = RecordCache(100)
    cache.put(rec("a", 10))
    assert cache.get("a").key == "a"
    assert cache.used_bytes == 10
    assert len(cache) == 1


def test_missing_key_returns_none():
    assert RecordCache(100).get("nope") is None


def test_replacing_a_key_does_not_double_count():
    cache = RecordCache(100)
    cache.put(rec("a", 10))
    cache.put(rec("a", 30))
    assert cache.used_bytes == 30
    assert len(cache) == 1


def test_lru_evicts_least_recently_used():
    cache = RecordCache(100, high_watermark=1.0, low_watermark=1.0)
    cache.put(rec("a", 40))
    cache.put(rec("b", 40))
    cache.get("a")  # "b" is now the least recently used
    cache.put(rec("c", 40))
    assert "b" not in cache
    assert "a" in cache and "c" in cache
    assert cache.stats.evictions == 1


def test_fifo_evicts_oldest_even_if_read():
    cache = RecordCache(100, high_watermark=1.0, low_watermark=1.0, policy=FIFOPolicy())
    cache.put(rec("a", 40))
    cache.put(rec("b", 40))
    cache.get("a")
    cache.put(rec("c", 40))
    assert "a" not in cache
    assert "b" in cache and "c" in cache


def test_evicts_down_to_low_watermark():
    cache = RecordCache(100, high_watermark=0.9, low_watermark=0.5)
    for i in range(9):
        cache.put(rec(f"k{i}", 10))  # 90 bytes: at the high watermark, no eviction yet
    assert cache.stats.evictions == 0

    cache.put(rec("k9", 10))  # would reach 100 > 90: evict until 10 more fits under 50
    assert cache.stats.evictions == 5
    assert cache.used_bytes == 50


def test_usage_never_exceeds_high_watermark():
    rng = random.Random(0)
    cache = RecordCache(10_000)
    for _i in range(500):
        cache.put(rec(f"k{rng.randrange(100)}", rng.randrange(1, 2_000)))
        assert cache.used_bytes <= cache.high_limit
    assert cache.used_bytes == sum(cache.get(k).size_bytes for k in list(cache._entries))


def test_too_large_record_is_not_cached_and_evicts_nothing():
    cache = RecordCache(100, low_watermark=0.8)
    cache.put(rec("a", 10))
    assert cache.put(rec("huge", 81)) is False
    assert "huge" not in cache
    assert "a" in cache
    assert cache.stats.too_large == 1
    assert cache.stats.evictions == 0


def test_remove_is_not_an_eviction():
    cache = RecordCache(100)
    cache.put(rec("a", 10))
    cache.remove("a")
    assert "a" not in cache
    assert cache.used_bytes == 0
    assert cache.stats.evictions == 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"capacity_bytes": 0},
        {"capacity_bytes": 100, "low_watermark": 0},
        {"capacity_bytes": 100, "high_watermark": 1.5},
        {"capacity_bytes": 100, "high_watermark": 0.5, "low_watermark": 0.9},
    ],
)
def test_bad_config_rejected(kwargs):
    with pytest.raises(ValueError):
        RecordCache(**kwargs)
