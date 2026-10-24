"""Tests that call the real SEC API. Run manually: uv run --env-file .env pytest -m live -s"""

import re

import pytest

from multi_agent_ai_systems.config import sec_user_agent
from multi_agent_ai_systems.connectors.edgar import (
    EdgarConnector,
    companyfacts_key,
    submissions_key,
)

pytestmark = pytest.mark.live

APPLE_CIK = 320193
ACCESSION = re.compile(r"\d{10}-\d{2}-\d{6}")


async def test_live_submissions():
    async with EdgarConnector(sec_user_agent()) as conn:
        record = await conn.fetch(submissions_key(APPLE_CIK))
        assert conn.requests_sent == 1

    recent = record.data["filings"]["recent"]
    assert "Apple" in record.data["name"]
    assert ACCESSION.fullmatch(record.version)
    # Our version logic assumes the newest filing comes first.
    assert recent["filingDate"][0] == max(recent["filingDate"])
    print(
        f"\nsubmissions: version={record.version} form={recent['form'][0]} "
        f"size={record.size_bytes:,} bytes"
    )


async def test_live_companyfacts():
    async with EdgarConnector(sec_user_agent()) as conn:
        record = await conn.fetch(companyfacts_key(APPLE_CIK))
        assert conn.requests_sent == 1

    assert ACCESSION.fullmatch(record.version)
    assert "us-gaap" in record.data["facts"]
    print(f"\ncompanyfacts: version={record.version} size={record.size_bytes:,} bytes")
