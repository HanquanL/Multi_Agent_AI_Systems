# Findings

What we measured or verified while building the gateway, with how we got it.
Each entry is marked **verified** (measured or checked) or **inferred** (reasoned, not yet checked).
Problems we hit and how we solved them are in [challenges.md](challenges.md).

## F1. The live EDGAR connector works
*2026-10-22 · verified*

`uv run --env-file .env pytest -m live -s` against Apple (CIK 320193): 2 tests passed, and each fetch
sent exactly one upstream request (`requests_sent == 1`).
Raw output: [runs/2026-10-22-live-edgar.txt](runs/2026-10-22-live-edgar.txt)

## F2. EDGAR lists recent filings newest first
*2026-10-22 · verified*

In the live `submissions` response, `filings.recent.filingDate[0]` is the latest date in the list.
This confirms the assumption behind how the connector picks the `submissions` version.

## F3. The `submissions` version is not a financial filing
*2026-10-22 · verified 2026-10-23*

Apple's `submissions` version was `0001140361-26-040540`. The prefix `0001140361` is a filing agent's
CIK, not Apple's `0000320193`. A second live run printed `filings.recent.form[0]`: the filing is a
**Form 4** (an insider transaction report), which contains no financial statements.
Raw output: [runs/2026-10-23-live-edgar-form.txt](runs/2026-10-23-live-edgar-form.txt)

Consequence: the `submissions` version changes on every filing of any type, so it is a noisy signal
for invalidating financial answers.

## F4. The `companyfacts` version is the company's own filing
*2026-10-22 · verified*

Apple's `companyfacts` version was `0000320193-26-000020`, the latest accession by filing date
across all facts. It is a better change signal for financial questions, but still per company,
not per fact.

## F5. Response sizes
*2026-10-22 · verified (Apple only)*

| Endpoint | Size (decompressed) |
|---|---|
| `submissions` | 163,917 bytes |
| `companyfacts` | 3,789,099 bytes |

Inferred: if large companies are similar, 250 of them need about 0.95 GB, and a 256 MB cache holds
about 70. A byte-bounded cache will see real eviction pressure. Sizes for more companies are needed
before choosing a capacity.

## F6. Live latency
*2026-10-22 · verified, single run*

The two live tests (two requests, including the 3.8 MB download) took 1.74 s in total on a home
network on 2026-10-22, and 1.06 s on 2026-10-23. Two samples; not a benchmark.

## F7. Record state and answer state are independent
*2026-10-19 to 2026-10-22 · partly supported by F3, partly reasoning*

All four combinations occur:

| Record | Answer | Example |
|---|---|---|
| unchanged | correct | a historical figure, e.g. FY2024 revenue |
| changed | correct | a new Form 4 arrives; revenue is unaffected (see F3) |
| unchanged | wrong | "latest revenue" after a new 10-K; no existing filing changed |
| changed | wrong | a relevant fact is revised |

This is the basis for comparing three invalidation policies: TTL, whole-record version, and
fact-aware.

## F8. A standard token bucket allows bursts above the limit
*2026-10-19 analysis · 2026-10-23 tested*

A token bucket with capacity C and refill rate R per second can admit up to C + R requests in one
1-second window. With C = R = 10 that is 20, which breaks the gate "never over 10 requests per
second at any moment". We use a sliding window instead; `test_never_more_than_limit_in_any_window`
sends 55 concurrent requests and confirms no 1-second window ever holds more than 10.

## F9. Findings changed the experiment design, not the core's size
*2026-10-22 · verified · updated 2026-10-23*

| Date | Built so far | Lines |
|---|---|---|
| 2026-10-22 | connector interface, EDGAR connector | ~300 |
| 2026-10-23 | + rate limiter, request merging, upstream path | 486 |
| 2026-10-23 | + byte-bounded cache, validity policy, gateway | 797 |

Current breakdown: `edgar.py` 190, `gateway.py` 144, `cache.py` 133, `base.py` 90, `ratelimit.py`
59, `upstream.py` 58, `merging.py` 47, `validity.py` 34, `clock.py` 21, `config.py` 18. Only
`edgar.py` (190 lines) is source-specific; the other ~600 lines are domain-free. Core files contain no
SEC or EDGAR references; `config.py` holds the only SEC-specific setting outside the EDGAR
connector, and `test_boundaries.py` enforces this.

## F10. `time.monotonic()` is coarse on Windows with Python 3.11
*2026-10-23 · verified*

| Clock | Windows implementation | Resolution |
|---|---|---|
| `time.monotonic()` | `GetTickCount64()` | 15.625 ms |
| `time.perf_counter()` | `QueryPerformanceCounter()` | 100 ns |

Spotted from rounded timestamps (`53479.0`, `53479.187`) in a test failure. A clock this coarse can
make the limiter see a window as clear up to one tick early, so 11 requests could fall in one real
second. The real clock now uses `time.perf_counter()`.

## F11. Merged requests cost one slot and one upstream request
*2026-10-23 · verified (offline test)*

In `test_merged_requests_use_one_limiter_slot`, 10 concurrent requests for the same key go through
`RequestMerger` and `Upstream`: the connector is called once, `Upstream.attempts == 1`, and the rate
limiter records one admission. Without merging, the same burst would take 10 slots, more than a
full second of EDGAR capacity at 8 per second.

## F12. Retries are rate limited like first attempts
*2026-10-23 · verified (offline test, simulated clock)*

In `test_every_retry_passes_the_limiter`, a fetch that fails twice and then succeeds, with a limit of
one request per second and no backoff, finishes at simulated time 2.0 s: three attempts, three
limiter slots. A retry cannot skip the limiter.

## F13. The offline suite stays fast
*2026-10-23 · verified*

53 offline tests ran in about 0.5 s on 2026-10-23; after the cache and gateway, 80 tests run in
about 0.54 s. The three slowest (about 0.1 s each) are the rate limiter's cancellation, timeout and
shutdown tests, which use the real clock on purpose; everything else uses the simulated clock.

## F14. Apple's data did not change across several days
*2026-10-23 · verified · extended 2026-10-26*

The 2026-10-22 and 2026-10-23 live runs returned identical versions and sizes for both endpoints
(`submissions` `0001140361-26-040540`, 163,917 bytes; `companyfacts` `0000320193-26-000020`,
3,789,099 bytes). The 2026-10-26 end-to-end demo (F19) returned the same `submissions` version and
size again, so Apple's filing list was unchanged for at least four days. Refetching either endpoint
a day later would have cost two requests and about 4 MB of download for no new information. A few
snapshots of one company are anecdotal, but this is the kind of waste (category 2: refetch after TTL
expiry with unchanged data) the request profiling will measure.

**Update 2026-11-02.** The `submissions` version changed between 2026-10-26 and 2026-11-02, from
`0001140361-26-040540` (163,917 bytes) to `0001140361-26-041201` (163,586 bytes), seen in the F22
run. The new accession number again starts with the filing agent's CIK (see F3); its form type was
not checked. The size went down, so a newer version does not mean more data.

## F15. A stale hit is invisible to the TTL but visible in the label
*2026-10-23 · verified (offline test, simulated clock)*

In `test_stale_hit_inside_ttl_is_visible_in_the_label`, the source changes from version `v1` to `v2`
60 seconds after the gateway cached `v1`. With a one-hour TTL the gateway still serves `v1` from the
cache: a stale hit. The freshness label reports `version=v1` and `age_seconds=60`, so the agent can
see what it got. This is the cache-safety problem the experiments will measure at scale: how often
TTL-based serving returns outdated data, and what preventing it costs in upstream requests.

## F16. The gateway classifies every upstream fetch
*2026-10-23 · verified (offline tests)*

Each upstream fetch is tagged with one reason: `FIRST`, `REFETCH_UNCHANGED`, `REFETCH_CHANGED`,
`REFETCH_UNKNOWN`, `EVICTED`, or `UNCACHEABLE`. Tests check each one, and check that 10 merged
callers produce one fetch and one reason, not ten. These counts are the input for the planned
request profiling (share of waste = unchanged refetches + eviction refetches).

## F17. Three MCP clients share one upstream fetch
*2026-10-26 · verified (offline test, in-process MCP clients)*

In `test_three_clients_share_one_fetch`, three separate MCP clients call `get_record` for the same
key at the same time through the real MCP protocol (in-process, no network). The gateway records
three misses and the source is called once. This is the build-order acceptance test for "two or
three clients connected at once". Not yet measured over HTTP against live EDGAR.

## F18. The MCP layer adds visible but small test time
*2026-10-26 · verified*

With the 7 MCP server tests added, the offline suite (87 tests) takes about 1.07 s, up from about
0.54 s for 80 tests. MCP client setup is the main cost; it is still fast enough to run on every
change.

## F19. The gateway works end to end against live EDGAR
*2026-10-26 · verified (live run)*

`scripts/demo_3_clients.py` against a server started with
`python -m multi_agent_ai_systems --http 8000`: three separate MCP clients over streamable HTTP ask
for Apple's `submissions` at the same time, then again.

| | Agent requests | Served from | SEC requests |
|---|---|---|---|
| Round 1 | 3 | upstream, all three merged into one fetch | 1 |
| Round 2 | 3 | cache (age 0.08–0.09 s) | 0 |

Gateway stats: 3 misses, 3 hits, one fetch with reason `first`, `upstream_attempts = 1`, one cache
entry of 163,917 bytes (0.06% of 256 MB), no evictions, rate limit 8 per second (0.8 safety
factor). Every layer worked together live: MCP over HTTP, shared gateway, merging, cache, freshness
labels, rate limiter, EDGAR connector.

Six agent requests cost one SEC request in this run. That is a demonstration, not a measured
saving: one key, three agents, two rounds. Real savings and stale-answer rates are for the
evaluation harness.
Raw output: [runs/2026-10-26-demo-three-clients.txt](runs/2026-10-26-demo-three-clients.txt)

## F20. Per-workload quotas split the limit even when one workload arrives first
*2026-10-27 · verified (offline tests, simulated clock)*

In `test_quotas_split_each_window`, the limit is 4 per second and workloads A and B have a quota of
2 each. All 8 of A's requests are queued before any of B's. Plain arrival order would give A the
first two seconds entirely; with `QuotaFIFO`, every one-second window admits exactly two A and two
B, and all 16 requests finish at simulated time 3.0 s. When B is idle, A may use the whole limit
(`test_idle_capacity_is_borrowed`: four A requests with quota 1, all admitted at t = 0), so quotas
cost no throughput. Workloads not in the configuration share one `default` bucket. The rate gate
tests from F8 still pass unchanged on the new limiter.

Suite: 94 offline tests in 1.18 s (was about 1.07 s for 87), so the dispatcher adds no visible cost.

## F21. Only the workload that starts a fetch pays for it
*2026-11-02 · verified (offline tests, simulated clock)*

With workload identity passed from `get_record` down to the rate limiter:

- Workloads A, B and C ask for the same key at once: the source is called once, A (first) is
  charged one limiter slot, and B and C use none of their share. A cache hit uses no share either.
- Through the whole gateway (`test_quota_protects_a_workload_that_arrives_late`), at 4 requests per
  second with quotas A = 2 and B = 2, A queues 8 fetches before B queues 4. B's longest wait is
  1.0 s; plain arrival order would make it 2.0 s. Once B is done, A uses the whole limit, so A's
  longest wait is 2.0 s instead of 3.0 s: quotas cost no throughput.

`gateway_stats` now reports hits, misses, fetches started, slots used and longest wait per workload.
Workload names are caller-supplied and unverified until access keys exist (C14). Suite: 100 offline
tests in 1.29 s.

## F22. Records survive a restart and are reused without a new SEC request
*2026-11-02 · verified (live run)*

Server started with `--db gateway.db`; `scripts/demo_3_clients.py` run, the server stopped and
started again with the same command, and the demo run again 4.8 minutes later.

| | Round 1 served from | Round 2 served from | SEC requests |
|---|---|---|---|
| Run 1 (cold) | upstream, 3 merged into 1 | memory cache | 1 |
| Run 2 (after restart) | 1 from the SQLite store, 2 from memory | memory cache | 0 |

In run 2 the first request to arrive read the record from SQLite (`store_hits: 1`) and copied it
into memory; the other five requests were memory hits. Store reads are synchronous, so only one
disk read happened for the key. The age continued across the restart (287.6 s, matching the time
between runs) because the store records wall-clock time: restarting does not reset the TTL.
Without the store, run 2 would have cost a new SEC request. Offline: 111 tests.
Raw output: [runs/2026-11-02-store-restart.txt](runs/2026-11-02-store-restart.txt)

## F23. Financial period identification and duplicate observations in SEC EDGAR
*2026-11-02 · verified (live, Apple only)*

Examining Apple's `companyfacts` (all `NetIncomeLoss` facts ending in 2024) showed several
challenges for deterministic fact retrieval:

1. The `fy` and `fp` fields can describe the filing that contains a fact rather than the period the
   fact measures. Apple's FY2024 net income (2023-10-01 to 2024-09-28, $93,736,000,000) appears with
   `fy=2024 FY` in the 2024 10-K and with `fy=2025 FY` in the 2025 10-K, as a comparative.
2. Historical values can appear in several filings, including later comparative disclosures, so one
   reporting period can have several observations. Here every period in 2024 appeared twice, with
   equal values.
3. Quarterly filings can contain both standalone quarterly values and year-to-date values for the
   same end date (fiscal Q2 2024: 3 months $23,636,000,000; 6 months $57,552,000,000). Only the
   period duration tells them apart.
4. Fiscal Q4 may lack a directly reported standalone quarterly fact. No 3-month fact ends on
   2024-09-28; that quarter exists only inside the annual value. It must be reported as unavailable
   or carefully derived.
5. Calendar-based `frame` labels (e.g. `CY2024Q1` for Apple's fiscal Q2) appear on only one of the
   duplicate observations and should not be treated as authoritative fiscal-period identifiers.

Apple reports revenue under several concept names across years
(`RevenueFromContractWithCustomerExcludingAssessedTax`, `Revenues`, `SalesRevenueNet`).

A second check the same day covered the other five metrics in the spec. Revenue (current concept),
operating income and diluted EPS show the same pattern as net income: two observations per 2024
period, year-to-date values next to quarter values, and no 3-month fact for fiscal Q4. Total assets
and cash are instant facts (no start date); each year-end balance (2024-09-28) appeared in five
filings: the 2024 10-K and, as the prior year-end comparative, the three 2025 10-Qs and the 2025
10-K. The `submissions` record lists each filing's `reportDate` (2024 10-K: 2024-09-28) and
`acceptanceDateTime`, which give the filing metadata the selection rules need. These
findings motivate explicit period-resolution rules, source provenance on every answer, and
conservative handling of ambiguous or unavailable facts; see [xbrl-selection.md](xbrl-selection.md).
Raw output: [runs/2026-11-02-companyfacts-netincome.txt](runs/2026-11-02-companyfacts-netincome.txt),
[runs/2026-11-02-companyfacts-metrics.txt](runs/2026-11-02-companyfacts-metrics.txt)