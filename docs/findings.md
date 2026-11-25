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

## F24. `get_fact` works on a company's full history, and a new filing moved provenance but not values
*2026-11-08 · verified (live, Apple only, one run)*

Fourteen `get_fact` questions over HTTP against a freshly started gateway cost 2 requests to SEC
(`companyfacts` and `submissions`, once each). Repeating `NetIncome FY2024` returned `miss`, then
`match`. This was the resolver's first run on all of Apple's facts rather than rows filtered to
2024; no question gave an "unresolved period" or "ambiguous value" error.

Every value checked on 2026-11-02 came back unchanged: net income FY2024 $93,736,000,000 and fiscal
Q2 2024 $23,636,000,000, revenue FY2024 $391,035,000,000, operating income FY2024
$123,216,000,000, diluted EPS Q2 2024 $1.53, total assets FY2024 $364,980,000,000, cash FY2024
$29,943,000,000. Fiscal Q4 of a duration metric was refused as "not reported" (2024Q4, and
`latest_quarter` right after the 10-K as 2026Q4), never replaced by Q3; for total assets, 2024Q4
and `latest_quarter` returned the year-end balance.

Apple filed its FY2026 10-K (`0000320193-26-000026`) on 2026-11-03, the day after the F23 runs. Net
income, revenue and operating income for FY2024 now resolve to that filing instead of
`0000320193-25-000079`, with the same values. This is the unchanged-comparative case: a
dependency comparing `(value, accn, filed)` would have invalidated answers cached before that
filing, while the value-only `fact.v1` keeps them. Total assets and cash for FY2024 stayed on
`0000320193-25-000079`.

Limits: one company and one run. The data did not change between asks, so no invalidation occurred
(`changed_probes` is empty); the comparison above is between this run and the 2026-11-02 runs. The
three values first seen here (net income FY2026 $132,175,000,000, net income 2026Q3
$29,789,000,000, total assets 2026Q4 $404,548,000,000) have not been checked against the filings.
Refused questions are counted as `miss`, so 13 misses include 2 refusals; 11 answers were stored.
Offline: 171 tests.
Raw output: [runs/2026-11-08-get-fact-live.txt](runs/2026-11-08-get-fact-live.txt)

## F25. The LLM path works live: repeats, merged requests and grounded answers avoid model calls
*2026-11-08 · verified (live, local model, one short session)*

Requests went through the `generate` MCP tool to `llama3.2:3b` on Ollama 0.40.15 (ID
`a80c4f17acd5`). In the first complete run, 10 requests made 7 model calls: one exact repeat came
from the cache and two of three identical concurrent requests merged. The same prompt from a
different workload generated its own answer, so responses stayed private. All token counters
matched a hand calculation in each run (run 1: 348 + 133 spent, 103 + 8 avoided).

A grounded request for Apple's FY2024 net income returned the correct value, $93,736,000,000. Its
repeat came from the cache in 0.01 s after the dependency check passed, with no additional SEC
request. Responses cut off by `max_tokens` (`finish_reason` `length`) were regenerated every time;
this included a grounded request first sent with too small a limit.

After the gateway restarted on the same SQLite file, 10 requests made 2 model calls, both for the
cut-off prompt that is never stored; 4 came from the store and 4 from memory. Rechecking the grounded
answer cost 2 SEC requests, because this server kept data records in memory only (no `--db`).

With Ollama stopped, all ten requests returned a readable `source error` and nothing was stored.

Limits: one model, one machine, a handful of prompts. No fact changed, so live invalidation was not
observed (`changed_probes` is empty; offline tests cover it). The restart came minutes after
storage, so this is not the 24-hour acceptance run. Model generation took about 0.04-0.9 s once
loaded, so time saved on short local answers is small. The first call's 2.29 s included model load
and is counted in `generation_seconds_spent` and in every later `generation_seconds_avoided` for
that answer, so those totals are inflated and should not be quoted. Dollar estimates are `null`
because a local model has no provider price. The answer's value was right, but it also said the
data was "not for Apple specifically", because the prompt identifies the company only by CIK;
generated text remains unverified. Quantization was not recorded.
Offline: 268 tests.
Raw output: [runs/2026-11-08-llm-live.txt](runs/2026-11-08-llm-live.txt)

## F26. Stored LLM answers are reused a week later, across a full shutdown
*2026-11-15 · verified (live, local model, one run)*

This is the 24-hour acceptance run. The answers stored on 2026-11-08 (F25) were served after the
gateway and Ollama had been shut down for a week. The same 10 requests as in F25 made 2 model
calls, both for the prompt cut off by `max_tokens`, which is never stored; 4 answers came from the
SQLite store and 4 from memory. The answers were 604,314 s old (7 days minus 486 s), within the
default 7-day response lifetime. The grounded answer was rechecked with 2 SEC requests, its fact
still matched, and it was reused with the same value, $93,736,000,000. Token counters matched a
hand calculation (68 + 6 spent, 383 + 169 avoided).

Ollama updated itself from 0.40.15 to 0.40.16 between storing and reusing, and the answers were
still reused. The model version in the key is the model ID plus the `created` time from `/models`,
so it tracks the model files, not the runtime that executes them. The weights were unchanged here
(same ID `a80c4f17acd5`), so reuse was correct, but a runtime update can change output slightly
and the gateway cannot see it.

Limits: one run, one model. Whether the server was started with `--llm-ttl 1209600` was not
recorded; every age is below the default lifetime, so the result holds either way. The cut-off
call's 25.38 s includes loading the model. The run shows survival across a shutdown and a week of
ageing, not a week of continuous uptime. Model: `llama3.2:3b`, 3.2B parameters, Q4_K_M.
Offline: 268 tests at the code used (commit 7497b74).
Raw output: [runs/2026-11-15-llm-24h-restart.txt](runs/2026-11-15-llm-24h-restart.txt)

## F27. The gateway runs without any data source, and non-financial prompts are reused
*2026-11-15 · verified (live, local model, one session)*

The gateway started with only `--llm-model` and `--llm-db`: no `.env`, no `--edgar`, no SEC
settings. Its stats had no data-source section. Three agents in one workload sent the same five
non-financial prompts (a translation, a fact, a one-word answer, a definition and a haiku)
concurrently, then again. 31 requests, counting one from a second workload, made 6 model calls:
in round 1, 5 calls served 15 requests (10 merged into calls in flight); round 2 was 15 cache hits
in 0.02 s; the second workload's identical prompt was generated separately. After a restart on the
same SQLite file, the same 31 requests made 0 model calls: 6 came from the store and 25 from
memory, including the second workload's own answer. Token counters matched a hand calculation in
both runs (run 2: 1,191 + 394 avoided).

Limits: the prompts were chosen to repeat, so 6 calls for 62 requests is not a general savings
rate; that needs the with/without-gateway experiment. LLM counters are pooled under `default`
because the LLM path has no per-workload allocation policy, so per-workload numbers are not
available, even though answers stay private per workload. `generation_seconds` and
`overhead_seconds` add up each request's own latency, so concurrent requests count shared waiting
more than once: run 1 reports 11.18 s of generation in a round that took 2.83 s, and run 2 reports
4.50 s of overhead for rounds that took 0.37 s. Time comparisons must use elapsed wall-clock time.
Offline: 274 tests (commit 2a5e6f7).
Raw output: [runs/2026-11-15-llm-only-nonfinance.txt](runs/2026-11-15-llm-only-nonfinance.txt)

## F28. Semantic reuse makes the intended decision in four demo cases, but cold models make it slow
*2026-11-25 · verified (live, four hand-picked cases, one run; transcribed from screenshots)*

Demo step 7 asks a question and then a differently worded one, in a workload with semantic
reuse active (similarity floor 0.8 for the demo; answers `llama3.2:3b`, embeddings
`nomic-embed-text:latest`, judge `qwen2.5:7b`). All four cases ended as designed. "Which
planet is the biggest?" was served the answer to "What is the largest planet?" (similarity
0.9463, judge SAME, source `similar`). "Should this file be deleted?" after "Can this file be
deleted?" was rejected by the rules (modal verbs differ) without calling the judge. "Explain
TCP in detail." after "Explain TCP in one sentence." was rejected by the judge (DIFFERENT).
"Who wrote Macbeth?" after "Who wrote Hamlet?" was left uncertain (UNSURE) and generated. In
the three cases that did not reuse, the second question was generated normally.

Cold models dominated the time. The first case took 88.94 s: 40.22 s for the first question
(loading the answer model and the embedding model) and 48.72 s for the reused one (loading the
judge). The demo warm-up loaded only the answer model at that point; it now also loads the
embedding model and the judge (commit 804e2fa).

Reuse did not pay off on these short answers. The reused answer avoided 106 tokens; one judge
call costs about 120 prompt tokens (119-120 per call in the live checks of 2026-11-24), and
judge tokens are counted separately (`judge_cost`), not in "tokens spent". Semantic reuse
saves only when the answer costs much more to generate than a judgement.

Limits: four hand-picked cases chosen to show each decision, one run, numbers transcribed from
screenshots. They show the decision path works, not that the judge is reliable; that needs a
labelled set. The 0.8 similarity floor was chosen so that every case reached a decision; it is
not a recommended setting. In the first case the counters show 2 model calls for one
generation; the tokens (139 spent = 106 for the question + 33) suggest a warm-up request ran in
the same seconds (inferred, not checked). The answer model's replies were weak in places (it
refused the file question and called Hamlet's authorship debated); this does not affect the
reuse decisions.
Offline: 320 tests (commit 1315542).
Raw output: [runs/2026-11-25-semantic-demo-step7.txt](runs/2026-11-25-semantic-demo-step7.txt)

## F29. On the development set, the 7B judge makes one false reuse; the 3B judge is unusable, and similarity is no safety signal
*2026-11-25 · verified (development set of 58 pairs, tuned on; three runs)*

`bench/judge_eval.py` ran the development set (`bench/pairs.jsonl` v0.2: 22 pairs whose
earlier answer may be reused, 36 where it may not) through embedding similarity, the
reject-only rules and an LLM judge, calling the models directly.

The judge decides safety. On its own, `qwen2.5:7b` called 1 of the 36 non-reusable pairs SAME;
`llama3.2:3b` called 25 of them SAME, including Hamlet/Macbeth, capital/population of France
and TCP/UDP delivery. With the rules in front, the 3B chain still accepted 15. The 7B chain's
one false reuse was d23, "Answer in French: what is the capital of Japan?" against the same
question in English: the judge did not treat the answer language as a requirement. Adding
"length, language or unit" to the judge prompt did not change that verdict.

Embedding similarity does not separate safe from unsafe pairs. The highest similarities in the
set belong to pairs with opposite meaning: "Is Paris more populous than London?" against the
reverse (0.995), "Did Germany invade Poland?" against the reverse (0.991), and "Is a tomato a
fruit?" against "... not a fruit?" (0.983), above most true paraphrases (0.755-0.989). With the
7B judge, raising the similarity floor from 0.80 to 0.90 left false reuse at 1/36 and lowered
coverage from 21/22 to 18/22; only at 0.95 did the false reuse disappear, at coverage 10/22. The
floor mainly decides how many pairs pay for a judge call (27 of 36 non-reusable pairs reach the
verifiers at 0.80, 16 at 0.90).

The rules first blocked 2 reusable pairs for "modal verbs differ": "Can you explain ..." and
"how can I ..." used "can" as a polite request or as "how do I". After the rule ignored those
phrases (commit e6ce328), the 7B chain judged all 22 reusable pairs SAME; at floor 0.80 coverage
rose from 19/22 to 21/22 with false reuse unchanged at 1/36 (precision 0.95). Judging cost
124-130 prompt tokens per call and took a median of 0.06 s with the model loaded.

Limits: 58 hand-written short questions with no conversation history; the set was written by
the same assistant that tuned the rules and prompt, and both changes were made after seeing
these results, so the numbers are a development result, not a measured error rate. "No false
reuse" at floor 0.95 means none among 36 pairs. Run A's per-pair file was overwritten by run C;
both outputs are in the run log, and run B was made with the rules before the change. The
reported numbers will come from a separate test set written by others, run once with the
configuration fixed beforehand (judge `qwen2.5:7b`, current rules and prompt, floor 0.80).
Offline: 322 tests (commit e6ce328).
Raw output: [runs/2026-11-25-judge-eval-dev.txt](runs/2026-11-25-judge-eval-dev.txt)