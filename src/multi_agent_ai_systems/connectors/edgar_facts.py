"""get_fact for SEC EDGAR: pick one reported value from a company's XBRL facts.

Implements docs/xbrl-selection.md as pure functions over the companyfacts and submissions
record bodies; fetching those records is the caller's job. EDGAR-specific: the gateway
core never imports this module.
"""

import re
from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Any


class FactErrorKind(Enum):
    UNSUPPORTED = "unsupported"  # metric or period outside what we answer
    UNRESOLVED_PERIOD = "unresolved_period"  # no filing labels this period (R3)
    NOT_REPORTED = "not_reported"  # the period exists, but no fact of the right length (R6)
    AMBIGUOUS_VALUE = "ambiguous_value"  # same-day facts disagree and cannot be ordered (R4)


class FactError(Exception):
    """An explicit refusal to answer (R7). The resolver never returns a nearby value."""

    def __init__(self, kind: FactErrorKind, message: str):
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class Metric:
    instant: bool  # a balance at one date, not a flow over a period (R2)
    concepts: frozenset[str]  # allowed us-gaap concepts, not a priority order (R5)
    unit: str


METRICS = {
    "Revenue": Metric(
        False,
        frozenset(
            {
                "RevenueFromContractWithCustomerExcludingAssessedTax",
                "Revenues",
                "SalesRevenueNet",
            }
        ),
        "USD",
    ),
    "NetIncome": Metric(False, frozenset({"NetIncomeLoss"}), "USD"),
    "OperatingIncome": Metric(False, frozenset({"OperatingIncomeLoss"}), "USD"),
    "EPSDiluted": Metric(False, frozenset({"EarningsPerShareDiluted"}), "USD/shares"),
    "TotalAssets": Metric(True, frozenset({"Assets"}), "USD"),
    "CashAndEquivalents": Metric(
        True, frozenset({"CashAndCashEquivalentsAtCarryingValue"}), "USD"
    ),
}

LATEST = ("latest_annual", "latest_quarter", "latest_reported_quarter")
ELIGIBLE_FORMS = frozenset({"10-K", "10-Q", "10-K/A", "10-Q/A"})  # R1
ANNUAL_DAYS = range(350, 381)  # R2: duration = end - start, in days
QUARTER_DAYS = range(84, 99)


@dataclass(frozen=True)
class Period:
    """A parsed period question: FY{fy}, {fy}Q{quarter}, or one of LATEST."""

    selector: str  # "FY", "Q", or a name from LATEST
    fy: int | None = None
    quarter: int | None = None


_FY = re.compile(r"FY([0-9]{4})")
_QUARTER = re.compile(r"([0-9]{4})Q([1-4])")


def parse_period(text: str) -> Period:
    if text in LATEST:
        return Period(text)
    if m := _FY.fullmatch(text):
        return Period("FY", fy=int(m[1]))
    if m := _QUARTER.fullmatch(text):
        return Period("Q", fy=int(m[1]), quarter=int(m[2]))
    raise FactError(FactErrorKind.UNSUPPORTED, f"unsupported period: {text!r}")


@dataclass(frozen=True)
class Fact:
    """One answer with its provenance (R8)."""

    value: int | float
    unit: str
    metric: str
    concept: str
    period: str  # the label, e.g. "FY2024" or "2024Q2"
    start: date | None  # None for instant metrics
    end: date
    form: str
    accn: str
    filed: date
    answer_kind: str = "exact"  # "derived" is reserved for later (e.g. Q4)

@dataclass(frozen=True)
class _Row:
    """One eligible XBRL fact (R1)."""

    concept: str
    start: date | None
    end: date
    fy: int | None
    fp: str | None
    form: str
    filed: date
    accn: str
    value: int | float

    def has_length(self, days: range) -> bool:
        return self.start is not None and (self.end - self.start).days in days


@dataclass(frozen=True)
class _Filing:
    report_date: date | None
    accepted: str  # acceptanceDateTime, ISO 8601 so it sorts as text; "" if unknown


def _eligible(companyfacts: dict[str, Any], spec: Metric, as_of: date | None) -> list[_Row]:
    """R1 forms and replay time, the metric's concepts and unit, the metric's fact type (R2)."""
    gaap = companyfacts.get("facts", {}).get("us-gaap", {})
    rows = []
    for concept in sorted(spec.concepts):
        for f in gaap.get(concept, {}).get("units", {}).get(spec.unit, []):
            filed = date.fromisoformat(f["filed"])
            if f.get("form") not in ELIGIBLE_FORMS or (as_of is not None and filed > as_of):
                continue
            start = f.get("start")
            if (start is None) != spec.instant:
                continue
            rows.append(
                _Row(
                    concept=concept,
                    start=date.fromisoformat(start) if start else None,
                    end=date.fromisoformat(f["end"]),
                    fy=f.get("fy"),
                    fp=f.get("fp"),
                    form=f["form"],
                    filed=filed,
                    accn=f["accn"],
                    value=f["val"],
                )
            )
    return rows


def _filings(submissions: dict[str, Any]) -> dict[str, _Filing]:
    recent = submissions.get("filings", {}).get("recent", {})
    accns = recent.get("accessionNumber", [])
    reports = recent.get("reportDate", [""] * len(accns))
    accepted = recent.get("acceptanceDateTime", [""] * len(accns))
    return {
        accn: _Filing(date.fromisoformat(report) if report else None, time or "")
        for accn, report, time in zip(accns, reports, accepted, strict=True)
    }


def _own_end(accn: str, fp: str, rows: list[_Row], filings: dict[str, _Filing]) -> date | None:
    """R3: the end of the filing's own period. The submissions report date when the filing is
    listed; otherwise the latest end among its facts, only if one of the matching length ends
    there."""
    filing = filings.get(accn)
    if filing is not None:
        return filing.report_date
    own = [r for r in rows if r.accn == accn]
    latest = max(r.end for r in own)
    days = ANNUAL_DAYS if fp == "FY" else QUARTER_DAYS
    if any(r.end == latest and r.has_length(days) for r in own):
        return latest
    return None


def _label_end(rows: list[_Row], filings: dict[str, _Filing], fy: int, fp: str) -> date:
    """R3: the end date of the period labelled fy/fp, taken only from filings in which it is
    their own period. Comparatives never supply a label."""
    ends = {
        r.end
        for r in rows
        if r.fy == fy and r.fp == fp and _own_end(r.accn, fp, rows, filings) == r.end
    }
    if len(ends) != 1:
        found = "no filing" if not ends else f"filings disagree ({sorted(ends)})"
        raise FactError(FactErrorKind.UNRESOLVED_PERIOD, f"{fp} {fy}: {found}")
    return ends.pop()


def _choose(candidates: list[_Row], filings: dict[str, _Filing]) -> _Row:
    """R4: the latest filed fact; same-day facts that disagree need acceptanceDateTime."""
    latest = max(r.filed for r in candidates)
    tied = [r for r in candidates if r.filed == latest]
    if len({r.value for r in tied}) > 1:
        accepted = {r.accn: filings[r.accn].accepted if r.accn in filings else "" for r in tied}
        if "" in accepted.values():
            raise FactError(FactErrorKind.AMBIGUOUS_VALUE, f"facts filed {latest} disagree")
        top = max(accepted.values())
        tied = [r for r in tied if accepted[r.accn] == top]
        if len({r.value for r in tied}) > 1:
            raise FactError(FactErrorKind.AMBIGUOUS_VALUE, f"facts accepted {top} disagree")
    # Equal values: any is correct; pick one deterministically for the provenance.
    return min(tied, key=lambda r: (r.accn, r.concept))


def resolve_fact(
    companyfacts: dict[str, Any],
    submissions: dict[str, Any],
    metric: str,
    period: str,
    *,
    as_of: date | None = None,
) -> Fact:
    """Answer metric for period, or raise FactError. as_of is the replay time (R1)."""
    spec = METRICS.get(metric)
    if spec is None:
        raise FactError(FactErrorKind.UNSUPPORTED, f"unsupported metric: {metric!r}")
    wanted = parse_period(period)
    if wanted.selector in LATEST:
        raise NotImplementedError("latest_* selectors come in the next step")
    rows = _eligible(companyfacts, spec, as_of)
    filings = _filings(submissions)

    annual = wanted.selector == "FY"
    if annual or wanted.quarter == 4:  # {fy}Q4 ends on the FY{fy} end date
        end = _label_end(rows, filings, wanted.fy, "FY")
    else:
        end = _label_end(rows, filings, wanted.fy, f"Q{wanted.quarter}")

    if spec.instant:
        candidates = [r for r in rows if r.end == end]
    else:
        days = ANNUAL_DAYS if annual else QUARTER_DAYS
        candidates = [r for r in rows if r.end == end and r.has_length(days)]
    if not candidates:
        raise FactError(FactErrorKind.NOT_REPORTED, f"no {metric} fact for {period}")
    if len({r.start for r in candidates}) > 1:
        raise FactError(FactErrorKind.UNRESOLVED_PERIOD, f"several {period} periods end {end}")

    chosen = _choose(candidates, filings)
    return Fact(
        value=chosen.value,
        unit=spec.unit,
        metric=metric,
        concept=chosen.concept,
        period=period,
        start=chosen.start,
        end=chosen.end,
        form=chosen.form,
        accn=chosen.accn,
        filed=chosen.filed,
    )