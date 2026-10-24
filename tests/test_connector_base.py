from datetime import UTC, datetime

import pytest
from fakes import FakeConnector

from multi_agent_ai_systems.connectors.base import (
    Connector,
    NotFoundError,
    Record,
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
