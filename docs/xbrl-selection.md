# XBRL fact selection for `get_fact`

*Draft, 2026-11-02. To be reviewed before the resolver is implemented.*

This document specifies how the EDGAR plugin answers `get_fact(company, metric, period)` from a
company's `companyfacts` record, using its `submissions` record for each filing's report date. The rules are deterministic: the same record and the same
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

| Metric | Type | Allowed concepts | Unit | Checked against Apple data |
|---|---|---|---|---|
| `Revenue` | duration | `RevenueFromContractWithCustomerExcludingAssessedTax`, `Revenues`, `SalesRevenueNet` | USD | first concept: yes, 2024 periods; `Revenues` and `SalesRevenueNet` present but their periods not checked |
| `NetIncome` | duration | `NetIncomeLoss` | USD | yes, 2024 periods |
| `OperatingIncome` | duration | `OperatingIncomeLoss` | USD | yes, 2024 periods |
| `EPSDiluted` | duration | `EarningsPerShareDiluted` | USD/shares | yes, 2024 periods |
| `TotalAssets` | instant | `Assets` | USD | yes, 2024 dates |
| `CashAndEquivalents` | instant | `CashAndCashEquivalentsAtCarryingValue` | USD | yes, 2024 dates |

All checks: 2026-11-02, Apple only; raw output in
[runs/2026-11-02-companyfacts-netincome.txt](runs/2026-11-02-companyfacts-netincome.txt) and
[runs/2026-11-02-companyfacts-metrics.txt](runs/2026-11-02-companyfacts-metrics.txt).

The concept lists are a set of allowed names, not a priority order (R5). A metric counts as tested
only after its presence, unit and periods have been checked against saved Apple data. The lists
will need review against more companies before the evaluation.

## Rules

**R1. Eligible facts.** Only facts from forms `10-K`, `10-Q`, `10-K/A` and `10-Q/A`, in an allowed
concept and the metric's unit. In replay, only facts with `filed` on or before the simulated time;
a later comparative or amendment must never influence an answer simulated earlier.

**R2. Period identity.**
- Duration facts are identified by their exact `(start, end)` dates. Duration in days decides the
  kind: 350 to 380 days is an annual period, 84 to 98 days is a quarter. Any other duration (for
  example a 6- or 9-month year-to-date value) is not used.
- Instant facts have no start date and are identified by their `end` date alone.
- If two different periods of the wanted length end on the same date (for example after a change
  of fiscal year), the result is an "unresolved period" error rather than a choice between them.

**R3. Period labels.** A reporting period is labelled with the `fy` and `fp` of a filing in which it
is the filing's own period; values from filings where it is only a comparative never supply a
label. For example, FY2024 (ending 2024-09-28) is named by the 2024 10-K, whose own period ends on
that date, and not by the 2025 10-K, whose own period ends 2025-09-27.

A filing's own period end is its **report date** from the company's `submissions` record
(`filings.recent.reportDate`, matched by accession number). Fallback, only when the accession is
not listed there: the latest `end` date among that filing's facts, accepted only if that filing has
a fact of the matching length ending on that date (annual for `fp = FY`, quarter for `Q1..Q3`);
otherwise the result is an "unresolved period" error.
- `fp = FY` gives `FY{fy}`; `fp = Q1..Q3` gives `{fy}Q{n}`.
- `{fy}Q4` is the quarter ending on the `FY{fy}` end date. No filing labels it directly, since
  fiscal Q4 is reported inside the 10-K. A duration metric then needs a quarter-length fact
  ending on that date (otherwise "not reported", R6); an instant metric gives the year-end balance.
- An instant fact takes the label of the filing whose own period ends on its date: the year-end
  balance for `FY{fy}`, the quarter-end balance for `{fy}Q{n}`.
- If no filing labels the period, or two filings label it differently, the result is an
  "unresolved period" error.

**R4. Choosing the value.** Among all eligible facts for the exact period, across all allowed
concepts of the metric, take the one with the latest `filed` date. This lets restatements and
amendments replace earlier values. If several facts share that latest date and their values
differ, the one whose filing has the latest `acceptanceDateTime` in the `submissions` record wins.
If that timestamp is missing for any of them, or also tied, the result is an "ambiguous value"
error. Accession numbers are not used to break ties: their prefix is the filer agent's CIK, so
their order says nothing about time (F3).

**R5. Concepts.** The metric's concept list only decides which facts are eligible. R4 then chooses
among them by filing date; one concept never overrides a more recently filed fact in another.

**R6. Latest periods.**
- `latest_annual`: the labelled annual period with the latest end date.
- `latest_quarter`: the most recent completed fiscal quarter. It ends at the latest end date that
  carries a label (R3); a fiscal year end is named `{fy}Q4`. If no quarter-length fact exists for
  that end date (usually fiscal Q4, which is reported only inside the annual value), the result is
  a "not reported" error. The plugin does not derive Q4 in the first version. Only labelled end
  dates are considered, because a period no filing names could not be answered anyway (R3).
- `latest_reported_quarter`: the labelled quarter period with the latest end date that has a
  quarter-length fact.
- For instant metrics, `FY2024` is the balance at the FY2024 year-end and `2024Q2` the balance at
  the fiscal Q2 end. `latest_annual` is the most recent fiscal year-end balance. `latest_quarter`
  is the most recent fiscal quarter-end balance, including the Q4 balance reported in a 10-K (a
  balance at year-end is also the Q4 quarter-end balance). `latest_reported_quarter` is the same as
  `latest_quarter` for instant metrics.
- For duration metrics, a fiscal Q4 that cannot be resolved is never replaced by Q3.

**R7. Errors, not guesses.** Unsupported inputs, unknown companies, no eligible fact for the
period, unresolved labels, and ambiguous values are returned as explicit errors. The resolver never
returns a nearby or approximate value.

**R8. Provenance.** Every answer carries: value, unit, metric, concept, period label, start and
end dates (end only for instants), form, accession number, filing date, and `answer_kind`
(`exact` for a reported value; `derived` is reserved for later, e.g. Q4 = annual minus nine-month
year-to-date). The period label is the resolved one: a `latest_quarter` question answers with,
for example, `2024Q3`, so the caller knows which period the value belongs to.

## Dependencies

The answer records what it relied on, as probes. A probe is evaluated by re-applying these rules
to the current `companyfacts` and `submissions` records of the company; every probe reads both,
because labels need report dates (R3) and ties need acceptance times (R4). The `v1` in each name
is the version of these rules: a rule change gets a new version, so old answers cannot silently
survive it.

| Probe | Expected value | Changes when |
|---|---|---|
| `fact.v1:{metric}:{unit}:{start}..{end}` (or `@{end}` for instants) | the value chosen by R4 | the value is restated or disappears |
| `label.v1:{metric}:{period}` | the end date the label resolves to | a label resolves differently (rare) |
| `latest.v1:{metric}:{selector}` | the label the selector resolves to (only for `latest_*` questions) | a new period is reported |
| `provenance.v1:{metric}:{unit}:{start}..{end}` | `[accn, filed]` of the fact chosen by R4 (optional) | a newer filing reports the value, even unchanged |

`fact.v1` compares the value only. Each new 10-K repeats earlier periods as comparatives, so the
latest-filed fact for a past period moves to a new filing every year with the same value;
comparing the accession too would invalidate answers that are still right (the over-invalidation
of C8). The accession and filing date stay in the answer as provenance. An answer that depends on
which filing reported the value, such as an LLM response naming the latest source filing, adds
`provenance.v1` explicitly. A probe that can no longer be answered (the fact disappeared, or values
became ambiguous) returns the error instead of a value, which counts as changed.

The `submissions` record changes with every filing of any type (F3), but these probes only look at
the facts and filings an answer used, so unrelated filings do not invalidate it. This is the
fact-aware behaviour the research compares with whole-record versions.

A cached answer is reusable only when every record it is checked against is fresh enough under
the record layer's policy and every probe returns its expected value. A matching value on stale
data is no proof of validity. If freshness cannot be established, the outcome is unknown, and the
caller's `max_age` decides. The evaluation counts value-changing invalidations separately from
provenance-only changes, to measure how many needless invalidations value-only probes avoid.

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
10. The labelling filing's own period comes from the `submissions` report date; the fallback is
    used only for an accession missing there, and an inconsistent fallback gives "unresolved
    period".
11. For an instant metric, `latest_quarter` right after a 10-K returns the year-end (Q4) balance.

The saved Apple rows cover test cases 1 to 4, 8, 10 (report dates) and 11. Cases 5 to 7 and 9
need small hand-built records, since Apple's 2024 data contains no restatement, no same-day
conflict and no concept change.

## Known limits of this version

- Rules checked against one company (Apple) and 2024 periods only.
- Instant balances are heavily duplicated: Apple's 2024 year-end balances appear in five filings,
  four of them as comparatives. R3 and R4 handle this, but it multiplies the facts each question
  must scan.
- Q4 derivation is deliberately absent. It is not a simple subtraction for every metric: diluted
  EPS for Q4 is not annual EPS minus nine-month EPS, because share counts differ by period.
- `filings.recent` in `submissions` lists only a company's most recent filings; older report
  dates are in additional files that this version does not fetch, so older periods rely on the
  fallback in R3.
- All of this logic lives in the EDGAR plugin; the gateway core stays domain-agnostic.
- No Q4 derivation, no segment or dimensional facts, no non-USD units, no IFRS (`ifrs-full`)
  filers.
- The ticker list is fetched from SEC; a ticker that changed hands maps to whatever SEC lists now.
- Answer correctness still needs separate evidence: a hand-checked sample of answers against the
  filings themselves.
