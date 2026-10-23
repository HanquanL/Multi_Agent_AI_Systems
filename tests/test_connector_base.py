from datetime import UTC, datetime

import pytest

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


class FakeConnector(Connector):
    name = "fake"
    capabilities = Capabilities(
        change_discovery=ChangeDiscovery.POLL,
        versioning=Versioning.VERSION_ID,
        rate_limit=RateLimit(max_requests=10, period_seconds=1.0),
    )

    def __init__(self, data: dict[str, str]):
        self.data = data
        self.calls = 0

    def mutability(self, key: str) -> Mutability:
        return Mutability.OVERWRITTEN

    async def fetch(self, key: str) -> Record:
        self.calls += 1
        if key not in self.data:
            raise NotFoundError(key)
        return Record(
            key=key,
            data=self.data[key],
            version="v1",
            size_bytes=len(self.data[key]),
            fetched_at=datetime.now(UTC),
        )


def test_connector_must_implement_all_methods():
    class Incomplete(Connector):
        pass

    with pytest.raises(TypeError):
        Incomplete()


async def test_fetch_returns_record():
    conn = FakeConnector({"a": "hello"})
    record = await conn.fetch("a")
    assert record.data == "hello"
    assert record.version == "v1"
    assert conn.calls == 1


async def test_fetch_missing_key_raises():
    conn = FakeConnector({})
    with pytest.raises(NotFoundError):
        await conn.fetch("missing")


def test_record_is_immutable():
    record = Record(key="a", data=1, version=None, size_bytes=1, fetched_at=datetime.now(UTC))
    with pytest.raises(AttributeError):
        record.data = 2
