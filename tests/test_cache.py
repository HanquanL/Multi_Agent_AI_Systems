import asyncio
from datetime import UTC, datetime

import pytest
from fakes import FakeClock, FakeConnector

from multi_agent_ai_systems.cache import RecordCache
from multi_agent_ai_systems.connectors.base import Mutability, Record, SourceError
from multi_agent_ai_systems.gateway import FetchReason, Gateway
from multi_agent_ai_systems.validity import TTLPolicy, Validity

HOUR = 3600.0


def make_gateway(conn, *, ttl=HOUR, capacity=1_000_000, policy=None):
    clock = FakeClock()
    gateway = Gateway(
        conn, cache=RecordCache(capacity), policy=policy or TTLPolicy(ttl), clock=clock
    )
    return gateway, clock


# --- validity policy ---


def test_ttl_policy_expires_only_overwritten_records():
    policy = TTLPolicy(HOUR)
    record = Record(key="a", data=None, version=None, size_bytes=1, fetched_at=datetime.now(UTC))
    assert policy.check(record, HOUR, Mutability.OVERWRITTEN) is Validity.VALID
    assert policy.check(record, HOUR + 1, Mutability.OVERWRITTEN) is Validity.INVALID
    assert policy.check(record, 1e9, Mutability.IMMUTABLE) is Validity.VALID


# --- hits, expiry, and why we fetched ---


async def test_second_request_within_ttl_is_a_cache_hit():
    conn = FakeConnector({"a": "x"})
    gateway, clock = make_gateway(conn)
    first = await gateway.get("a")
    clock.advance(10)
    second = await gateway.get("a")

    assert first.label.source == "upstream"
    assert second.label.source == "cache"
    assert second.data == "x"
    assert conn.calls == 1
    assert (gateway.stats.hits, gateway.stats.misses) == (1, 1)
    assert gateway.stats.fetches[FetchReason.FIRST] == 1


async def test_expired_entry_is_refetched_and_classified_unchanged():
    conn = FakeConnector({"a": "x"})
    gateway, clock = make_gateway(conn)
    await gateway.get("a")
    clock.advance(HOUR + 1)
    response = await gateway.get("a")

    assert response.label.source == "upstream"
    assert conn.calls == 2
    assert gateway.stats.fetches[FetchReason.REFETCH_UNCHANGED] == 1


async def test_changed_version_is_classified_changed():
    conn = FakeConnector({"a": "old"})
    gateway, clock = make_gateway(conn)
    await gateway.get("a")
    conn.data["a"], conn.versions["a"] = "new", "v2"
    clock.advance(HOUR + 1)
    response = await gateway.get("a")

    assert response.data == "new"
    assert response.label.version == "v2"
    assert gateway.stats.fetches[FetchReason.REFETCH_CHANGED] == 1


async def test_stale_hit_inside_ttl_is_visible_in_the_label():
    # The cache-safety problem in one test: the source changed, but the TTL still says valid.
    conn = FakeConnector({"a": "old"})
    gateway, clock = make_gateway(conn)
    await gateway.get("a")
    conn.data["a"], conn.versions["a"] = "new", "v2"
    clock.advance(60)
    response = await gateway.get("a")

    assert response.data == "old"
    assert response.label.version == "v1"
    assert response.label.age_seconds == 60


async def test_immutable_records_never_expire():
    conn = FakeConnector({"a": "x"})
    conn.immutable.add("a")
    gateway, clock = make_gateway(conn)
    await gateway.get("a")
    clock.advance(10 * 365 * 24 * HOUR)

    assert (await gateway.get("a")).label.source == "cache"
    assert conn.calls == 1


async def test_caller_max_age_forces_refetch():
    conn = FakeConnector({"a": "x"})
    gateway, clock = make_gateway(conn)
    await gateway.get("a")
    clock.advance(120)

    assert (await gateway.get("a", max_age=60)).label.source == "upstream"
    assert conn.calls == 2


async def test_label_reports_version_fetch_time_and_age():
    conn = FakeConnector({"a": "x"})
    gateway, clock = make_gateway(conn)
    first = await gateway.get("a")
    clock.advance(30)
    label = (await gateway.get("a")).label

    assert label.key == "a"
    assert label.version == "v1"
    assert label.fetched_at == first.label.fetched_at
    assert label.age_seconds == 30


async def test_evicted_entry_is_refetched_and_classified_evicted():
    conn = FakeConnector({"a": "x" * 40, "b": "y" * 40, "c": "z" * 40})
    cache = RecordCache(100, high_watermark=1.0, low_watermark=1.0)
    gateway = Gateway(conn, cache=cache, policy=TTLPolicy(HOUR), clock=FakeClock())
    for key in ("a", "b", "c"):  # storing "c" evicts "a", the least recently used
        await gateway.get(key)
    await gateway.get("a")

    assert gateway.stats.fetches[FetchReason.EVICTED] == 1


async def test_too_large_record_is_served_but_not_cached():
    conn = FakeConnector({"big": "x" * 90})
    gateway, _ = make_gateway(conn, capacity=100)  # low watermark: 80 bytes
    first = await gateway.get("big")
    second = await gateway.get("big")

    assert first.data == second.data == "x" * 90
    assert conn.calls == 2
    assert gateway.stats.fetches[FetchReason.UNCACHEABLE] == 1


# --- coordination and failure through the whole gateway ---


async def test_concurrent_misses_make_one_fetch_and_one_classification():
    conn = FakeConnector({"a": "x"})
    conn.gate = asyncio.Event()
    gateway, _ = make_gateway(conn)
    tasks = [asyncio.create_task(gateway.get("a")) for _ in range(10)]
    await asyncio.sleep(0)
    conn.gate.set()
    responses = await asyncio.gather(*tasks)

    assert all(r.data == "x" for r in responses)
    assert conn.calls == 1
    assert gateway.stats.misses == 10
    assert sum(gateway.stats.fetches.values()) == 1


async def test_failed_fetch_is_not_cached():
    conn = FakeConnector({"a": "x"})
    conn.failures = [SourceError("boom")]
    gateway, _ = make_gateway(conn)
    with pytest.raises(SourceError):
        await gateway.get("a")

    assert "a" not in gateway.cache
    assert (await gateway.get("a")).data == "x"
    assert conn.calls == 2


class AlwaysUnknown:
    def check(self, record, age_seconds, mutability):
        return Validity.UNKNOWN


async def test_unknown_validity_defers_to_callers_max_age():
    conn = FakeConnector({"a": "x"})
    gateway, clock = make_gateway(conn, policy=AlwaysUnknown())
    await gateway.get("a")
    clock.advance(30)
    assert (await gateway.get("a")).label.source == "upstream"  # no max_age: don't trust it
    clock.advance(30)
    assert (await gateway.get("a", max_age=60)).label.source == "cache"


def test_safety_factor_reaches_the_limiter():
    gateway = Gateway(
        FakeConnector(), cache=RecordCache(100), policy=TTLPolicy(HOUR), safety_factor=0.8
    )
    assert gateway.limiter.max_requests == 8
