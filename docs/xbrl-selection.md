# XBRL fact selection for `get_fact`

*Draft, 2026-11-02. To be reviewed before the resolver is implemented.*

This document specifies how the EDGAR plugin answers `get_fact(company, metric, period)` from a
company's `companyfacts` record. The rules are deterministic: the same record and the same
question always give the same answer or the same error. They are written before the code so that
the resolver, its tests, and the evaluation all follow one definition. Background: F23 in
[findings.md](findings.md).

The rules belong to the EDGAR plugin only. The gateway core never sees them; it only stores the
probes and expected values the plugin reports (see "Dependencies" below).

## Inputs

| Field | Allowed values |
|---|---|
| `company` | a CIK (any number of digits up to 10) or a ticker; tickers are mapped to CIKs with SEC's ticker list, fetched as a record through the gateway |
| `metric` | one of the metrics in the table below |
| `period` | `FY2024` (fiscal year), `2024Q2` (fiscal quarter), `latest_annual`, `latest_quarter`, `latest_reported_quarter` |

Anything else is rejected with an error before any record is fetched.

## Metrics

Each metric has a fact type and a list of allowed XBRL concepts (taxonomy `us-gaap`).

| Metric | Type | Allowed concepts | Unit |
|---|---|---|---|
| `Revenue` | duration | `RevenueFromContractWithCustomerExcludingAssessedTax`, `Revenues`, `SalesRevenueNet` | USD |
| `NetIncome` | duration | `NetIncomeLoss` | USD |
| `OperatingIncome` | duration | `OperatingIncomeLoss` | USD |
| `EPSDiluted` | duration | `EarningsPerShareDiluted` | USD/shares |
| `TotalAssets` | instant | `Assets` | USD |
| `CashAndEquivalents` | instant | `CashAndCashEquivalentsAtCarryingValue` | USD |

The concept lists are a set of allowed names, not a priority order (R5). They will need review
against more companies than Apple before the evaluation.

## Rules

**R1. Eligible facts.** Only facts from forms `10-K`, `10-Q`, `10-K/A` and `10-Q/A`, in an allowed
concept and the metric's unit. In replay, only facts with `filed` on or before the simulated time;
a later comparative or amendment must never influence an answer simulated earlier.

**R2. Period identity.**
- Duration facts are identified by their exact `(start, end)` dates. Duration in days decides the
  kind: 350 to 380 days is an annual period, 84 to 98 days is a quarter. Any other duration (for
  example a 6- or 9-month year-to-date value) is not used.
- Instant facts have no start date and are identified by their `end` date alone.

**R3. Period labels.** A filing's own period ends at the latest `end` date among all facts in the
record that carry that filing's `accn`, across all concepts. A reporting period is labelled with the `fy` and `fp` of a filing
in which it is the filing's own period; values from filings where it is only a comparative never
supply a label. For example, FY2024 (ending 2024-09-28) is named by the 2024 10-K, whose own period
ends on that date, and not by the 2025 10-K, whose own period ends 2025-09-27.
- `fp = FY` gives `FY{fy}`; `fp = Q1..Q3` gives `{fy}Q{n}`.
- An instant fact takes the label of the filing whose own period ends on its date: the year-end
  balance for `FY{fy}`, the quarter-end balance for `{fy}Q{n}`.
- If no filing labels the period, or two filings label it differently, the result is an
  "unresolved period" error.

**R4. Choosing the value.** Among all eligible facts for the exact period, across all allowed
concepts of the metric, take the one with the latest `filed` date. This lets restatements and
amendments replace earlier values. If several facts share that latest date and their values
differ, the result is an "ambiguous value" error. Accession numbers are not used to break ties:
their prefix is the filer agent's CIK, so their order says nothing about time (F3).

**R5. Concepts.** The metric's concept list only decides which facts are eligible. R4 then chooses
among them by filing date; one concept never overrides a more recently filed fact in another.

**R6. Latest periods.**
- `latest_annual`: the labelled annual period with the latest end date.
- `latest_quarter`: the most recent completed fiscal quarter. It ends at the latest end date among
  all eligible duration facts of the metric. If no quarter-length fact exists for that end date
  (usually fiscal Q4, which is reported only inside the annual value), the result is a
  "not reported" error. The plugin does not derive Q4 in the first version.
- `latest_reported_quarter`: the labelled quarter period with the latest end date that has a
  quarter-length fact.
- For instant metrics, `latest_quarter` and `latest_reported_quarter` both mean the most recent
  labelled quarter-end or year-end balance, and `latest_annual` the most recent year-end balance.

**R7. Errors, not guesses.** Unsupported inputs, unknown companies, no eligible fact for the
period, unresolved labels, and ambiguous values are returned as explicit errors. The resolver never
returns a nearby or approximate value.

**R8. Provenance.** Every answer carries: value, unit, metric, concept, period label, start and
end dates (end only for instants), form, accession number, filing date, and `answer_kind`
(`exact` for a reported value; `derived` is reserved for later, e.g. Q4 = annual minus nine-month
year-to-date).

## Dependencies

The answer records what it relied on, as probes on the `companyfacts` record (versioned so a rule
change cannot silently keep old answers):

| Probe | Expected value | Changes when |
|---|---|---|
| `fact.v1:{metric}:{start}..{end}` (or `@{end}` for instants) | `(value, accn, filed)` chosen by R4 | the value is restated or a newer filing reports it |
| `label.v1:{period}` | the `(start, end)` the label resolved to | a label resolves differently (rare) |
| `latest.v1:{metric}:{selector}` | the period the selector resolved to (only for `latest_*` questions) | a new period is reported |

A cached answer is reusable only when the `companyfacts` record it is checked against is fresh
enough under the record layer's policy and every probe returns its expected value. If freshness
cannot be established, the outcome is unknown, and the caller's `max_age` decides.

## Worked example (Apple, from the 2026-11-02 data)

| Question | Answer | Why |
|---|---|---|
| `NetIncome`, `FY2024` | 93,736,000,000 USD, 2023-10-01 to 2024-09-28, 10-K `0000320193-25-000079` filed 2025-10-31 | R3: labelled by the 2024 10-K; R4: the 2025 10-K comparative has the latest filing date, same value |
| `NetIncome`, `2024Q2` | 23,636,000,000 USD, 2023-12-31 to 2024-03-30 | R2: the 6-month value 57,552,000,000 ends the same day but is year-to-date |
| `NetIncome`, `2024Q4` | "not reported" | no 3-month fact ends 2024-09-28 |

## Test cases the resolver must pass

1. The worked examples above, from the real rows in
   [runs/2026-11-02-companyfacts-netincome.txt](runs/2026-11-02-companyfacts-netincome.txt).
2. A comparative never supplies a label (FY2024 is not labelled FY2025).
3. Year-to-date facts are ignored for quarter questions.
4. An instant metric resolves by end date and takes the year-end or quarter-end label.
5. A restated value filed later replaces the original, and the `fact.v1` probe changes.
6. Two facts filed on the same date with different values give "ambiguous value".
7. A replay time before a filing's date excludes that filing.
8. `latest_quarter` right after a 10-K gives "not reported"; `latest_reported_quarter` gives Q3.
9. A metric reported under two concepts in different years resolves by filing date, not concept
   order.

## Known limits of this version

- Rules checked against one company (Apple) only.
- No Q4 derivation, no segment or dimensional facts, no non-USD units, no IFRS (`ifrs-full`)
  filers.
- The ticker list is fetched from SEC; a ticker that changed hands maps to whatever SEC lists now.
- Answer correctness still needs separate evidence: a hand-checked sample of answers against the
  filings themselves.
