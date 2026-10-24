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

On 2026-10-22 the package was about 300 lines (`edgar.py` 190, `base.py` 90). After the rate
limiter, request merging and the upstream path it is 486 lines: `edgar.py` 190, `base.py` 90,
`ratelimit.py` 59, `upstream.py` 58, `merging.py` 47, `clock.py` 21, `config.py` 18. Core files
contain no SEC or EDGAR references; `config.py` holds the only SEC-specific setting outside the
EDGAR connector, and `test_boundaries.py` now enforces this.

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

53 offline tests run in about 0.5 s. The three slowest (about 0.1 s each) are the rate limiter's
cancellation, timeout and shutdown tests, which use the real clock on purpose; everything else uses
the simulated clock.

## F14. Apple's data did not change between the two live runs
*2026-10-23 · verified*

The 2026-10-22 and 2026-10-23 live runs returned identical versions and sizes for both endpoints
(`submissions` `0001140361-26-040540`, 163,917 bytes; `companyfacts` `0000320193-26-000020`,
3,789,099 bytes). Refetching either one a day later would have cost two requests and about 4 MB of
download for no new information. Two snapshots of one company is anecdotal, but it is the kind of
waste (category 2: refetch after TTL expiry with unchanged data) the request profiling will measure.