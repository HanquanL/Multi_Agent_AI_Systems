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
*2026-10-22 · partly inferred*

Apple's `submissions` version was `0001140361-26-040540`. The prefix `0001140361` is a filing agent's
CIK, not Apple's `0000320193` (verified). That this filing is an insider Form 4 is inferred and still
needs checking against `filings.recent.form[0]`.

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
network. One sample; not a benchmark.

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
*2026-10-19 · verified by arithmetic, to be tested*

A token bucket with capacity C and refill rate R per second can admit up to C + R requests in one
1-second window. With C = R = 10 that is 20, which breaks the gate "never over 10 requests per
second at any moment". To be confirmed by the rate limiter tests.

## F9. Findings changed the experiment design, not the core's size
*2026-10-22 · verified*

The package is about 300 lines (`edgar.py` 190, `base.py` 90). Core files contain no SEC or EDGAR
references; `config.py` holds the only SEC-specific setting outside the EDGAR connector.