"""get_fact for SEC EDGAR: pick one reported value from a company's XBRL facts.

Implements docs/xbrl-selection.md as pure functions over the companyfacts and submissions
record bodies; fetching those records is the caller's job. EDGAR-specific: the gateway
core never imports this module.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Any

from multi_agent_ai_systems.connectors.edgar import companyfacts_key, submissions_key
from multi_agent_ai_systems.dependencies import Dependency


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
    period: str  # the resolved label, e.g. "FY2024" or "2024Q2", also for latest_* questions

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

@dataclass(frozen=True)
class _Context:
    """What the rules read from a company's two records, for one metric."""

    spec: Metric
    rows: list[_Row]
    filings: dict[str, _Filing]
    labels: dict[date, set[tuple[int, str]]]


def _context(
    companyfacts: dict[str, Any], submissions: dict[str, Any], metric: str, as_of: date | None
) -> _Context:
    spec = METRICS.get(metric)
    if spec is None:
        raise FactError(FactErrorKind.UNSUPPORTED, f"unsupported metric: {metric!r}")
    rows = _eligible(companyfacts, spec, as_of)
    filings = _filings(submissions)
    return _Context(spec, rows, filings, _labels(rows, filings))


def _label_fp(wanted: Period) -> str:
    """The filing fp that names the wanted period; {fy}Q4 ends on the FY{fy} end (R3)."""
    if wanted.selector == "FY" or wanted.quarter == 4:
        return "FY"
    return f"Q{wanted.quarter}"


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


def _labels(rows: list[_Row], filings: dict[str, _Filing]) -> dict[date, set[tuple[int, str]]]:
    """R3: every (fy, fp) label, keyed by the end date of the period it names. Only a filing
    whose own period ends on that date supplies a label; comparatives never do."""
    own: dict[str, date | None] = {}
    labels: dict[date, set[tuple[int, str]]] = {}
    for r in rows:
        if r.fy is None or r.fp not in ("FY", "Q1", "Q2", "Q3"):
            continue
        if r.accn not in own:
            own[r.accn] = _own_end(r.accn, r.fp, rows, filings)
        if own[r.accn] == r.end:
            labels.setdefault(r.end, set()).add((r.fy, r.fp))
    return labels


def _label_end(labels: dict[date, set[tuple[int, str]]], fy: int, fp: str) -> date:
    """The end date of the period labelled fy/fp; exactly one, and labelled only that way."""
    ends = [end for end, names in labels.items() if (fy, fp) in names]
    if len(ends) != 1:
        found = "no filing" if not ends else f"filings disagree ({sorted(ends)})"
        raise FactError(FactErrorKind.UNRESOLVED_PERIOD, f"{fp} {fy}: {found}")
    _name(labels, ends[0], quarter=False)  # raises if another filing labels it differently
    return ends[0]


def _name(labels: dict[date, set[tuple[int, str]]], end: date, *, quarter: bool) -> str:
    """The label of the period ending on end. A fiscal year end is "{fy}Q4" when a quarter is
    wanted."""
    names = labels[end]
    if len(names) != 1:
        raise FactError(
            FactErrorKind.UNRESOLVED_PERIOD, f"period ending {end} has labels {sorted(names)}"
        )
    fy, fp = next(iter(names))
    if fp == "FY":
        return f"{fy}Q4" if quarter else f"FY{fy}"
    return f"{fy}{fp}"


def _latest(
    selector: str, spec: Metric, rows: list[_Row], labels: dict[date, set[tuple[int, str]]]
) -> tuple[date, str]:
    """R6: the end date and label a latest_* question resolves to."""
    if not labels:
        raise FactError(FactErrorKind.UNRESOLVED_PERIOD, "no filing labels any period")
    if selector == "latest_annual":
        ends = [end for end, names in labels.items() if any(fp == "FY" for _, fp in names)]
    elif selector == "latest_reported_quarter" and not spec.instant:
        ends = [
            end
            for end in labels
            if any(r.end == end and r.has_length(QUARTER_DAYS) for r in rows)
        ]
    else:  # latest_quarter; for instant metrics also latest_reported_quarter
        ends = list(labels)
    if not ends:
        raise FactError(FactErrorKind.NOT_REPORTED, f"no period for {selector}")
    end = max(ends)
    return end, _name(labels, end, quarter=selector != "latest_annual")


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
    wanted = parse_period(period)
    ctx = _context(companyfacts, submissions, metric, as_of)

    if wanted.selector in LATEST:
        end, label = _latest(wanted.selector, ctx.spec, ctx.rows, ctx.labels)
    else:
        end, label = _label_end(ctx.labels, wanted.fy, _label_fp(wanted)), period

    if ctx.spec.instant:
        candidates = [r for r in ctx.rows if r.end == end]
    else:
        days = ANNUAL_DAYS if label.startswith("FY") else QUARTER_DAYS
        candidates = [r for r in ctx.rows if r.end == end and r.has_length(days)]
    if not candidates:
        raise FactError(FactErrorKind.NOT_REPORTED, f"no {metric} fact for {label}")
    if len({r.start for r in candidates}) > 1:
        raise FactError(FactErrorKind.UNRESOLVED_PERIOD, f"several {label} periods end {end}")

    chosen = _choose(candidates, ctx.filings)
    return Fact(
        value=chosen.value,
        unit=ctx.spec.unit,
        metric=metric,
        concept=chosen.concept,
        period=label,
        start=chosen.start,
        end=chosen.end,
        form=chosen.form,
        accn=chosen.accn,
        filed=chosen.filed,
    )

# Probes (docs/xbrl-selection.md, "Dependencies"). Each re-applies the rules above to the
# current records. The "v1" in each name is the version of those rules.


def dependencies(
    cik: int | str, question: str, fact: Fact, *, provenance: bool = False
) -> list[Dependency]:
    """What an answer to question (the period as asked) relies on. provenance=True adds
    provenance.v1, for answers that depend on which filing reported the value."""
    keys = (companyfacts_key(cik), submissions_key(cik))
    span = _span(fact.start, fact.end)
    deps = []
    if question in LATEST:
        deps.append(Dependency(keys, f"latest.v1:{fact.metric}:{question}", fact.period))
    deps.append(Dependency(keys, f"label.v1:{fact.metric}:{fact.period}", fact.end.isoformat()))
    deps.append(Dependency(keys, f"fact.v1:{fact.metric}:{fact.unit}:{span}", fact.value))
    if provenance:
        deps.append(
            Dependency(
                keys,
                f"provenance.v1:{fact.metric}:{fact.unit}:{span}",
                [fact.accn, fact.filed.isoformat()],
            )
        )
    return deps


def evaluate(probe: str, bodies: Sequence[Any], *, as_of: date | None = None) -> Any:
    """The probe's current value, from the bodies of its dependency's records in order
    (companyfacts, submissions). A probe the rules can no longer answer gives
    {"error": kind}, which never equals a value. Raises ValueError for a malformed probe or one
    this version does not know."""
    companyfacts, submissions = bodies
    name, _, rest = probe.partition(":")
    args = rest.split(":")
    try:
        if name == "latest.v1" and len(args) == 2 and args[1] in LATEST:
            ctx = _context(companyfacts, submissions, args[0], as_of)
            return _latest(args[1], ctx.spec, ctx.rows, ctx.labels)[1]
        if name == "label.v1" and len(args) == 2 and args[1] not in LATEST:
            wanted = parse_period(args[1])
            ctx = _context(companyfacts, submissions, args[0], as_of)
            return _label_end(ctx.labels, wanted.fy, _label_fp(wanted)).isoformat()
        if name in ("fact.v1", "provenance.v1") and len(args) == 3:
            metric, unit, span = args
            ctx = _context(companyfacts, submissions, metric, as_of)
            if unit != ctx.spec.unit:
                raise ValueError(f"{unit!r} is not the unit of {metric}")
            row = _fact_at(ctx, *_parse_span(span))
            return row.value if name == "fact.v1" else [row.accn, row.filed.isoformat()]
    except FactError as exc:
        return {"error": exc.kind.value}
    raise ValueError(f"unknown probe: {probe!r}")


def _fact_at(ctx: _Context, start: date | None, end: date) -> _Row:
    """R4 for one exact period."""
    candidates = [r for r in ctx.rows if r.start == start and r.end == end]
    if not candidates:
        raise FactError(FactErrorKind.NOT_REPORTED, f"no fact for {_span(start, end)}")
    return _choose(candidates, ctx.filings)


def _span(start: date | None, end: date) -> str:
    return f"@{end.isoformat()}" if start is None else f"{start.isoformat()}..{end.isoformat()}"


def _parse_span(span: str) -> tuple[date | None, date]:
    if span.startswith("@"):
        return None, date.fromisoformat(span[1:])
    start, _, end = span.partition("..")
    return date.fromisoformat(start), date.fromisoformat(end)