"""get_fact resolver (docs/xbrl-selection.md), checked on Apple's real rows."""

from datetime import date

import pytest

from multi_agent_ai_systems.connectors.edgar_facts import FactError, FactErrorKind, resolve_fact

# Apple NetIncomeLoss facts ending in 2024, copied from
# docs/runs/2026-11-02-companyfacts-netincome.txt: (start, end, fy, fp, form, filed, accn, val)
APPLE_NET_INCOME = """
2023-10-01 2024-03-30 2024 Q2 10-Q 2024-05-03 0000320193-24-000069 57552000000
2023-10-01 2024-03-30 2025 Q2 10-Q 2025-05-02 0000320193-25-000057 57552000000
2023-12-31 2024-03-30 2024 Q2 10-Q 2024-05-03 0000320193-24-000069 23636000000
2023-12-31 2024-03-30 2025 Q2 10-Q 2025-05-02 0000320193-25-000057 23636000000
2023-10-01 2024-06-29 2024 Q3 10-Q 2024-08-02 0000320193-24-000081 79000000000
2023-10-01 2024-06-29 2025 Q3 10-Q 2025-08-01 0000320193-25-000073 79000000000
2024-03-31 2024-06-29 2024 Q3 10-Q 2024-08-02 0000320193-24-000081 21448000000
2024-03-31 2024-06-29 2025 Q3 10-Q 2025-08-01 0000320193-25-000073 21448000000
2023-10-01 2024-09-28 2024 FY 10-K 2024-11-01 0000320193-24-000123 93736000000
2023-10-01 2024-09-28 2025 FY 10-K 2025-10-31 0000320193-25-000079 93736000000
2024-09-29 2024-12-28 2025 Q1 10-Q 2025-01-31 0000320193-25-000008 36330000000
2024-09-29 2024-12-28 2026 Q1 10-Q 2026-01-30 0000320193-26-000006 36330000000
"""

# Apple's 10-K and 10-Q filings, as printed in docs/runs/2026-11-02-companyfacts-metrics.txt:
# accession form reportDate filingDate
APPLE_FILINGS = """
0000320193-25-000079 10-K 2025-09-27 2025-10-31
0000320193-25-000073 10-Q 2025-06-28 2025-08-01
0000320193-25-000057 10-Q 2025-03-29 2025-05-02
0000320193-25-000008 10-Q 2024-12-28 2025-01-31
0000320193-24-000123 10-K 2024-09-28 2024-11-01
0000320193-24-000081 10-Q 2024-06-29 2024-08-02
0000320193-24-000069 10-Q 2024-03-30 2024-05-03
0000320193-24-000006 10-Q 2023-12-30 2024-02-02
"""

# Apple Assets facts ending in 2024, as printed in
# docs/runs/2026-11-02-companyfacts-metrics.txt (instant: no start)
APPLE_ASSETS = """
None 2024-03-30 2024 Q2 10-Q 2024-05-03 0000320193-24-000069 337411000000
None 2024-06-29 2024 Q3 10-Q 2024-08-02 0000320193-24-000081 331612000000
None 2024-09-28 2024 FY 10-K 2024-11-01 0000320193-24-000123 364980000000
None 2024-09-28 2025 Q1 10-Q 2025-01-31 0000320193-25-000008 364980000000
None 2024-09-28 2025 Q2 10-Q 2025-05-02 0000320193-25-000057 364980000000
None 2024-09-28 2025 Q3 10-Q 2025-08-01 0000320193-25-000073 364980000000
None 2024-09-28 2025 FY 10-K 2025-10-31 0000320193-25-000079 364980000000
None 2024-12-28 2025 Q1 10-Q 2025-01-31 0000320193-25-000008 344085000000
"""


def fact_rows(text: str) -> list[tuple]:
    """Fact rows as printed in docs/runs; a start of "None" marks an instant fact."""
    rows = []
    for line in text.split("\n"):
        if not line.strip():
            continue
        start, end, fy, fp, form, filed, accn, val = line.split()
        value = float(val) if "." in val else int(val)
        start = None if start == "None" else start
        rows.append((start, end, int(fy), fp, form, filed, accn, value))
    return rows


def filing_rows(text: str) -> list[tuple]:
    return [tuple(line.split()) for line in text.split("\n") if line.strip()]

def companyfacts(concepts: dict[str, tuple[str, list[tuple]]]) -> dict:
    """A companyfacts body from {concept: (unit, rows)}."""
    return {
        "facts": {
            "us-gaap": {
                concept: {"units": {unit: [_fact(row) for row in rows]}}
                for concept, (unit, rows) in concepts.items()
            }
        }
    }


def _fact(row: tuple) -> dict:
    start, end, fy, fp, form, filed, accn, val = row
    fact = {"end": end, "val": val, "accn": accn, "fy": fy, "fp": fp, "form": form, "filed": filed}
    if start is not None:  # instant facts have no start, as in SEC's data
        fact["start"] = start
    return fact


def submissions(filings: list[tuple], accepted: dict[str, str] | None = None) -> dict:
    """A submissions body from (accession, form, reportDate, filingDate) rows."""
    accepted = accepted or {}
    accns = [f[0] for f in filings]
    return {
        "filings": {
            "recent": {
                "accessionNumber": accns,
                "form": [f[1] for f in filings],
                "reportDate": [f[2] for f in filings],
                "filingDate": [f[3] for f in filings],
                "acceptanceDateTime": [accepted.get(a, "") for a in accns],
            }
        }
    }


APPLE_FACTS = companyfacts(
    {
        "NetIncomeLoss": ("USD", fact_rows(APPLE_NET_INCOME)),
        "Assets": ("USD", fact_rows(APPLE_ASSETS)),
    }
)
APPLE_SUBMISSIONS = submissions(filing_rows(APPLE_FILINGS))


def resolve(metric: str, period: str, **kwargs) -> object:
    return resolve_fact(APPLE_FACTS, APPLE_SUBMISSIONS, metric, period, **kwargs)


def error_kind(metric: str, period: str, **kwargs) -> FactErrorKind:
    with pytest.raises(FactError) as err:
        resolve(metric, period, **kwargs)
    return err.value.kind


def test_annual_value_comes_from_the_latest_filing():  # spec case 1, R4
    fact = resolve("NetIncome", "FY2024")
    assert fact.value == 93_736_000_000
    assert (fact.start, fact.end) == (date(2023, 10, 1), date(2024, 9, 28))
    assert (fact.form, fact.accn) == ("10-K", "0000320193-25-000079")
    assert fact.filed == date(2025, 10, 31)
    assert (fact.metric, fact.concept, fact.unit) == ("NetIncome", "NetIncomeLoss", "USD")
    assert fact.period == "FY2024"
    assert fact.answer_kind == "exact"


def test_quarter_ignores_year_to_date():  # spec case 3, R2
    fact = resolve("NetIncome", "2024Q2")
    assert fact.value == 23_636_000_000  # not the 6-month 57,552,000,000
    assert (fact.start, fact.end) == (date(2023, 12, 31), date(2024, 3, 30))


def test_fiscal_q4_is_not_reported():  # spec case 1, R6
    assert error_kind("NetIncome", "2024Q4") is FactErrorKind.NOT_REPORTED


def test_comparative_never_labels_a_period():  # spec case 2, R3
    # The 2025 10-K repeats FY2024 with fy=2025, fp=FY. FY2025 must not resolve to it.
    assert error_kind("NetIncome", "FY2025") is FactErrorKind.UNRESOLVED_PERIOD


@pytest.mark.parametrize(
    ("metric", "period"),
    [("GrossMargin", "FY2024"), ("NetIncome", "2024Q5"), ("NetIncome", "2024"), ("NetIncome", "")],
)
def test_unsupported_inputs_are_rejected(metric, period):  # R7
    assert error_kind(metric, period) is FactErrorKind.UNSUPPORTED

def test_instant_metric_resolves_by_end_date():  # spec case 4, R2/R3
    fact = resolve("TotalAssets", "FY2024")
    assert fact.value == 364_980_000_000
    assert (fact.start, fact.end) == (None, date(2024, 9, 28))
    assert fact.accn == "0000320193-25-000079"  # five filings report it; the latest wins
    assert resolve("TotalAssets", "2024Q2").value == 337_411_000_000


def test_instant_q4_is_the_year_end_balance():  # R3, R6
    fact = resolve("TotalAssets", "2024Q4")
    assert (fact.value, fact.end) == (364_980_000_000, date(2024, 9, 28))


def test_replay_ignores_filings_not_yet_public():  # spec case 7, R1
    fact = resolve("NetIncome", "FY2024", as_of=date(2025, 10, 30))
    assert (fact.accn, fact.filed) == ("0000320193-24-000123", date(2024, 11, 1))
    # The day before the 2024 10-K was filed, FY2024 had no label yet.
    kind = error_kind("NetIncome", "FY2024", as_of=date(2024, 10, 31))
    assert kind is FactErrorKind.UNRESOLVED_PERIOD


# Hand-built records for cases Apple's 2024 data does not contain.
# Accessions are made up; the CIK prefix 0000000001 is not a real filer.
ANNUAL_FILINGS = """
0000000001-24-000001 10-K 2024-09-28 2024-11-01
0000000001-25-000001 10-K/A 2024-09-28 2025-02-03
"""


def resolve_built(facts: dict, filings: str, metric: str, period: str, **kwargs) -> object:
    body = submissions(filing_rows(filings), kwargs.pop("accepted", None))
    return resolve_fact(companyfacts(facts), body, metric, period, **kwargs)


def test_restated_value_replaces_the_original():  # spec case 5, R4
    rows = fact_rows(
        """
        2023-10-01 2024-09-28 2024 FY 10-K 2024-11-01 0000000001-24-000001 100
        2023-10-01 2024-09-28 2024 FY 10-K/A 2025-02-03 0000000001-25-000001 90
        """
    )
    fact = resolve_built({"NetIncomeLoss": ("USD", rows)}, ANNUAL_FILINGS, "NetIncome", "FY2024")
    assert (fact.value, fact.form, fact.accn) == (90, "10-K/A", "0000000001-25-000001")


SAME_DAY = """
2023-10-01 2024-09-28 2024 FY 10-K 2024-11-01 0000000001-24-000001 100
2023-10-01 2024-09-28 2024 FY 10-K/A 2024-11-01 0000000001-25-000001 101
"""
SAME_DAY_FILINGS = """
0000000001-24-000001 10-K 2024-09-28 2024-11-01
0000000001-25-000001 10-K/A 2024-09-28 2024-11-01
"""


def test_same_day_conflict_without_acceptance_time_is_ambiguous():  # spec case 6, R4
    facts = {"NetIncomeLoss": ("USD", fact_rows(SAME_DAY))}
    with pytest.raises(FactError) as err:
        resolve_built(facts, SAME_DAY_FILINGS, "NetIncome", "FY2024")
    assert err.value.kind is FactErrorKind.AMBIGUOUS_VALUE


def test_same_day_conflict_is_broken_by_acceptance_time():  # R4
    facts = {"NetIncomeLoss": ("USD", fact_rows(SAME_DAY))}
    accepted = {
        "0000000001-24-000001": "2024-11-01T16:30:00.000Z",
        "0000000001-25-000001": "2024-11-01T18:05:00.000Z",
    }
    fact = resolve_built(facts, SAME_DAY_FILINGS, "NetIncome", "FY2024", accepted=accepted)
    assert (fact.value, fact.accn) == (101, "0000000001-25-000001")


@pytest.mark.parametrize(
    ("old_concept", "new_concept"),
    [
        ("SalesRevenueNet", "RevenueFromContractWithCustomerExcludingAssessedTax"),
        ("RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"),
    ],
)
def test_concept_change_resolves_by_filing_date(old_concept, new_concept):  # spec case 9, R5
    old = fact_rows("2016-09-25 2017-09-30 2017 FY 10-K 2017-11-03 0000000001-17-000001 229")
    new = fact_rows("2016-09-25 2017-09-30 2018 FY 10-K 2018-11-05 0000000001-18-000001 230")
    filings = """
    0000000001-17-000001 10-K 2017-09-30 2017-11-03
    0000000001-18-000001 10-K 2018-09-29 2018-11-05
    """
    facts = {old_concept: ("USD", old), new_concept: ("USD", new)}
    fact = resolve_built(facts, filings, "Revenue", "FY2017")
    assert (fact.value, fact.concept) == (230, new_concept)