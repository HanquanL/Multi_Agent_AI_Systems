"""Connector for SEC EDGAR.

Keys:
    submissions/<cik>    company filing list, overwritten by each new filing
    companyfacts/<cik>   all XBRL facts for a company, overwritten by each new filing
    archive/<path>       a filing document under /Archives/edgar/data/, immutable

CIKs in keys are always 10 digits, zero-padded. Build keys with the helpers below
so equivalent requests get the same key.
"""

import json
from datetime import UTC, datetime
from typing import Any, Self

import httpx

from multi_agent_ai_systems.connectors.base import (
    Capabilities,
    ChangeDiscovery,
    Connector,
    Mutability,
    NotFoundError,
    RateLimit,
    Record,
    SourceError,
    ThrottledError,
    Versioning,
)

DATA_URL = "https://data.sec.gov"
ARCHIVE_URL = "https://www.sec.gov/Archives"
KINDS = ("submissions", "companyfacts", "archive")


def normalize_cik(cik: int | str) -> str:
    """Return the CIK as a 10-digit, zero-padded string."""
    digits = str(cik).strip()
    if not digits.isdigit() or len(digits) > 10:
        raise ValueError(f"invalid CIK: {cik!r}")
    return digits.zfill(10)


def submissions_key(cik: int | str) -> str:
    return f"submissions/{normalize_cik(cik)}"

def companyfacts_key(cik: int | str) -> str:
    return f"companyfacts/{normalize_cik(cik)}"


def archive_key(path: str) -> str:
    """Key for a filing document, e.g. 'edgar/data/320193/000032019324000123/x.htm'."""
    return f"archive/{path.strip('/')}"


class EdgarConnector(Connector):
    name = "edgar"
    capabilities = Capabilities(
        change_discovery=ChangeDiscovery.POLL,  # nightly index and RSS; nothing is pushed
        versioning=Versioning.VERSION_ID,  # accession numbers
        rate_limit=RateLimit(max_requests=10, period_seconds=1.0),
    )

    def __init__(
        self,
        user_agent: str,
        *,
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.requests_sent = 0
        self._client = httpx.AsyncClient(
            headers={"User-Agent": user_agent},
            timeout=timeout,
            transport=transport,
            event_hooks={"request": [self._count_request]},
        )

    async def _count_request(self, request: httpx.Request) -> None:
        self.requests_sent += 1

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    def mutability(self, key: str) -> Mutability:
        kind, _ = _split_key(key)
        return Mutability.IMMUTABLE if kind == "archive" else Mutability.OVERWRITTEN

    async def fetch(self, key: str) -> Record:
        kind, rest = _split_key(key)
        url = _url_for(kind, rest)  # bad keys fail here, before anything is sent
        try:
            response = await self._client.get(url)
        except httpx.TimeoutException as exc:
            raise SourceError(f"timed out fetching {key}") from exc
        except httpx.TransportError as exc:
            raise SourceError(f"network error fetching {key}: {exc}") from exc

        _raise_for_status(key, response)
        body = response.content
        if kind == "archive":
            data: Any = body
            version = rest.split("/")[3]
        else:
            data = _parse_json(key, body)
            if kind == "submissions":
                version = _submissions_version(data)
            else:
                version = _companyfacts_version(data)

        return Record(
            key=key,
            data=data,
            version=version,
            size_bytes=len(body),
            fetched_at=datetime.now(UTC),
        )


def _split_key(key: str) -> tuple[str, str]:
    kind, sep, rest = key.partition("/")
    if not sep or kind not in KINDS or not rest:
        raise ValueError(f"unknown EDGAR key: {key!r}")
    return kind, rest


def _url_for(kind: str, rest: str) -> str:
    if kind == "archive":
        parts = rest.split("/")
        if len(parts) < 5 or parts[:2] != ["edgar", "data"]:
            raise ValueError(f"archive key must be edgar/data/<cik>/<accession>/<file>: {rest!r}")
        return f"{ARCHIVE_URL}/{rest}"
    if normalize_cik(rest) != rest:
        raise ValueError(f"CIK in key must be 10 digits; use the key helpers: {rest!r}")
    if kind == "submissions":
        return f"{DATA_URL}/submissions/CIK{rest}.json"
    return f"{DATA_URL}/api/xbrl/companyfacts/CIK{rest}.json"


def _raise_for_status(key: str, response: httpx.Response) -> None:
    status = response.status_code
    if status == 200:
        return
    if status == 404:
        raise NotFoundError(key)
    if status == 429:
        raise ThrottledError(f"throttled fetching {key}", retry_after=_retry_after(response))
    # SEC may signal rate limiting with 403 instead of 429. Open question; measure in 2c.
    raise SourceError(f"HTTP {status} fetching {key}")


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None  # HTTP-date form; not worth parsing until we see SEC send it


def _parse_json(key: str, body: bytes) -> dict[str, Any]:
    try:
        return json.loads(body)
    except ValueError as exc:
        raise SourceError(f"bad JSON for {key}") from exc


def _submissions_version(data: dict[str, Any]) -> str | None:
    """Accession number of the newest filing. EDGAR lists recent filings newest first."""
    accessions = data.get("filings", {}).get("recent", {}).get("accessionNumber", [])
    return accessions[0] if accessions else None


def _companyfacts_version(data: dict[str, Any]) -> str | None:
    """Accession number of the most recently filed fact."""
    latest: tuple[str, str] | None = None
    for taxonomy in data.get("facts", {}).values():
        for concept in taxonomy.values():
            for entries in concept.get("units", {}).values():
                for entry in entries:
                    candidate = (entry.get("filed", ""), entry.get("accn", ""))
                    if latest is None or candidate > latest:
                        latest = candidate
    return latest[1] if latest else None
