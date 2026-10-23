import json

import httpx
import pytest

from multi_agent_ai_systems.connectors.base import (
    Mutability,
    NotFoundError,
    SourceError,
    ThrottledError,
)
from multi_agent_ai_systems.connectors.edgar import (
    EdgarConnector,
    archive_key,
    companyfacts_key,
    normalize_cik,
    submissions_key,
)

UA = "Test User test@example.com"
ARCHIVE_PATH = "edgar/data/320193/000032019324000123/aapl-20240928.htm"

SUBMISSIONS = {
    "cik": "320193",
    "filings": {"recent": {"accessionNumber": ["0000320193-24-000123", "0000320193-24-000081"]}},
}

COMPANYFACTS = {
    "facts": {
        "us-gaap": {
            "Revenues": {
                "units": {
                    "USD": [
                        {"val": 1, "accn": "0000320193-23-000106", "filed": "2023-11-03"},
                        {"val": 2, "accn": "0000320193-24-000123", "filed": "2024-11-01"},
                    ]
                }
            }
        }
    }
}


def make_connector(handler):
    return EdgarConnector(UA, transport=httpx.MockTransport(handler))


# --- keys ---


def test_cik_normalization():
    assert normalize_cik(320193) == "0000320193"
    assert normalize_cik("0000320193") == "0000320193"
    assert submissions_key(320193) == submissions_key("0000320193")


@pytest.mark.parametrize("bad", ["", "abc", "-1", "12345678901"])
def test_invalid_cik_rejected(bad):
    with pytest.raises(ValueError):
        normalize_cik(bad)


@pytest.mark.parametrize("key", ["nonsense", "submissions/320193", "archive/foo.htm"])
async def test_bad_key_sends_nothing(key):
    async with make_connector(lambda request: httpx.Response(200)) as conn:
        with pytest.raises(ValueError):
            await conn.fetch(key)
        assert conn.requests_sent == 0


# --- successful fetches ---


async def test_fetch_submissions():
    body = json.dumps(SUBMISSIONS).encode()
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, content=body)

    async with make_connector(handler) as conn:
        record = await conn.fetch(submissions_key(320193))
        assert conn.requests_sent == 1

    assert str(seen[0].url) == "https://data.sec.gov/submissions/CIK0000320193.json"
    assert seen[0].headers["User-Agent"] == UA
    assert record.data == SUBMISSIONS
    assert record.version == "0000320193-24-000123"
    assert record.size_bytes == len(body)


async def test_companyfacts_version_is_latest_filed():
    body = json.dumps(COMPANYFACTS).encode()
    async with make_connector(lambda request: httpx.Response(200, content=body)) as conn:
        record = await conn.fetch(companyfacts_key(320193))
    assert record.version == "0000320193-24-000123"


async def test_archive_is_raw_bytes_and_immutable():
    key = archive_key(ARCHIVE_PATH)
    async with make_connector(lambda request: httpx.Response(200, content=b"<html>")) as conn:
        record = await conn.fetch(key)
        assert conn.mutability(key) is Mutability.IMMUTABLE
    assert record.data == b"<html>"
    assert record.version == "000032019324000123"


async def test_aggregate_keys_are_overwritten():
    async with make_connector(lambda request: httpx.Response(200)) as conn:
        assert conn.mutability(submissions_key(1)) is Mutability.OVERWRITTEN
        assert conn.mutability(companyfacts_key(1)) is Mutability.OVERWRITTEN


# --- failures: each one is a single request, never retried ---


async def test_404_raises_not_found():
    async with make_connector(lambda request: httpx.Response(404)) as conn:
        with pytest.raises(NotFoundError):
            await conn.fetch(submissions_key(1))
        assert conn.requests_sent == 1


async def test_429_raises_throttled_with_retry_after():
    def handler(request):
        return httpx.Response(429, headers={"Retry-After": "5"})

    async with make_connector(handler) as conn:
        with pytest.raises(ThrottledError) as info:
            await conn.fetch(submissions_key(1))
    assert info.value.retry_after == 5.0


async def test_server_error_is_not_retried():
    async with make_connector(lambda request: httpx.Response(503)) as conn:
        with pytest.raises(SourceError):
            await conn.fetch(submissions_key(1))
        assert conn.requests_sent == 1


async def test_timeout_raises_source_error():
    def handler(request):
        raise httpx.ReadTimeout("too slow", request=request)

    async with make_connector(handler) as conn:
        with pytest.raises(SourceError):
            await conn.fetch(submissions_key(1))
        assert conn.requests_sent == 1


async def test_bad_json_raises_source_error():
    async with make_connector(lambda request: httpx.Response(200, content=b"not json")) as conn:
        with pytest.raises(SourceError):
            await conn.fetch(submissions_key(1))
