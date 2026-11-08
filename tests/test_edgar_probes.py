"""EDGAR probes (docs/xbrl-selection.md, "Dependencies"): what an answer relies on, and
whether it still holds on newer records."""

from datetime import date

import pytest
from test_edgar_facts import (
    ANNUAL_FILINGS,
    APPLE_FACTS,
    APPLE_SUBMISSIONS,
    RIGHT_AFTER_10K,
    companyfacts,
    fact_rows,
    filing_rows,
    submissions,
)

from multi_agent_ai_systems.connectors.edgar_facts import dependencies, evaluate, resolve_fact
from multi_agent_ai_systems.dependencies import Dependency

APPLE = (APPLE_FACTS, APPLE_SUBMISSIONS)
KEYS = ("companyfacts/0000320193", "submissions/0000320193")
BEFORE_2025_10K = date(2025, 10, 30)  # the 2025 10-K, repeating FY2024, was filed 2025-10-31


def deps_for(metric, question, *, bodies=APPLE, as_of=None, provenance=False):
    fact = resolve_fact(*bodies, metric, question, as_of=as_of)
    return dependencies("320193", question, fact, provenance=provenance)


def current(dep, *, bodies=APPLE, as_of=None):
    return evaluate(dep.probe, bodies, as_of=as_of)


def by_name(deps):
    return {dep.probe.split(":")[0]: dep for dep in deps}


def test_explicit_period_depends_on_its_label_and_value():
    assert deps_for("NetIncome", "FY2024") == [
        Dependency(KEYS, "label.v1:NetIncome:FY2024", "2024-09-28"),
        Dependency(KEYS, "fact.v1:NetIncome:USD:2023-10-01..2024-09-28", 93_736_000_000),
    ]


def test_latest_question_also_depends_on_the_selection():
    deps = deps_for("NetIncome", "latest_reported_quarter", as_of=RIGHT_AFTER_10K)
    assert [dep.probe for dep in deps] == [
        "latest.v1:NetIncome:latest_reported_quarter",
        "label.v1:NetIncome:2024Q3",
        "fact.v1:NetIncome:USD:2024-03-31..2024-06-29",
    ]
    assert deps[0].expected == "2024Q3"


def test_instant_value_probe_names_only_the_date():
    assert deps_for("TotalAssets", "FY2024")[-1].probe == "fact.v1:TotalAssets:USD:@2024-09-28"


@pytest.mark.parametrize(
    ("metric", "question", "as_of"),
    [
        ("NetIncome", "FY2024", None),
        ("NetIncome", "2024Q2", None),
        ("NetIncome", "latest_reported_quarter", RIGHT_AFTER_10K),
        ("TotalAssets", "2024Q4", None),
        ("TotalAssets", "latest_quarter", RIGHT_AFTER_10K),
    ],
)
def test_probes_match_on_the_records_they_came_from(metric, question, as_of):
    for dep in deps_for(metric, question, as_of=as_of, provenance=True):
        assert current(dep, as_of=as_of) == dep.expected, dep.probe


def test_unchanged_comparative_keeps_the_value_but_moves_provenance():
    deps = by_name(deps_for("NetIncome", "FY2024", as_of=BEFORE_2025_10K, provenance=True))
    assert deps["provenance.v1"].expected == ["0000320193-24-000123", "2024-11-01"]
    # The 2025 10-K repeats FY2024 with the same value: still valid, newer source.
    assert current(deps["fact.v1"]) == deps["fact.v1"].expected
    assert current(deps["label.v1"]) == deps["label.v1"].expected
    assert current(deps["provenance.v1"]) == ["0000320193-25-000079", "2025-10-31"]


def test_restated_value_changes_the_value_probe():
    original = "2023-10-01 2024-09-28 2024 FY 10-K 2024-11-01 0000000001-24-000001 100"
    restated = "2023-10-01 2024-09-28 2024 FY 10-K/A 2025-02-03 0000000001-25-000001 90"
    filings = submissions(filing_rows(ANNUAL_FILINGS))
    day1 = (companyfacts({"NetIncomeLoss": ("USD", fact_rows(original))}), filings)
    both = fact_rows(original + "\n" + restated)
    day2 = (companyfacts({"NetIncomeLoss": ("USD", both)}), filings)
    deps = by_name(deps_for("NetIncome", "FY2024", bodies=day1))
    assert deps["fact.v1"].expected == 100
    assert current(deps["fact.v1"], bodies=day2) == 90
    assert current(deps["label.v1"], bodies=day2) == deps["label.v1"].expected


def test_new_quarter_changes_the_selection_but_not_the_old_value():
    deps = by_name(deps_for("NetIncome", "latest_reported_quarter", as_of=RIGHT_AFTER_10K))
    after_q1 = date(2025, 2, 15)  # the FY2025 Q1 10-Q was filed 2025-01-31
    assert current(deps["latest.v1"], as_of=after_q1) == "2025Q1"
    assert current(deps["fact.v1"], as_of=after_q1) == deps["fact.v1"].expected  # 2024Q3 as before


def test_probe_that_can_no_longer_be_answered_returns_the_error():
    dep = by_name(deps_for("NetIncome", "FY2024"))["fact.v1"]
    assert current(dep, as_of=date(2024, 10, 31)) == {"error": "not_reported"}


@pytest.mark.parametrize(
    "probe",
    [
        "fact.v2:NetIncome:USD:2023-10-01..2024-09-28",  # a rule version we do not know
        "fact.v1:NetIncome:EUR:2023-10-01..2024-09-28",  # wrong unit
        "label.v1:NetIncome:latest_quarter",  # a selector is not a label
    ],
)
def test_unknown_probes_are_rejected(probe):
    with pytest.raises(ValueError):
        evaluate(probe, APPLE)