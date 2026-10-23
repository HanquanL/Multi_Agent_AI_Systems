# Challenges

Problems we hit while building the gateway and how we handled them.
Measured results are in [findings.md](findings.md).

## C1. A loophole in the scoring function
*Found ~2026-10-17 during design review*

**Problem.** Scoring by "correct answers ÷ upstream requests" alone rewards always serving from cache:
past-quarter figures never change, so a cache-only policy spends few requests and still answers
most questions correctly.

**Resolution.** Minimum accuracy is a hard gate; runs below it get no score. We also report the
requests-vs-accuracy curve. `test_scoring.py` will check the ranking of degenerate policies,
with the expected ranking written down before running.

## C2. An unchanged record does not mean a correct answer
*2026-10-19, while designing the connector interface*

**Problem.** Our first assumption was that stale cache hits can only come from records that get
overwritten. That is wrong. "What was Apple's FY2024 revenue?" stays correct forever, but
"What is Apple's latest revenue?" goes stale when a new 10-K arrives, even though no existing filing
changed. "Latest" is a property of a collection (the company's filing list), not of one record.

**Resolution.** Connectors keep reporting mutability per record (`mutability(key)`). Cached answers
must track what they depend on, including collections. The evaluator counts stale-record errors
separately from errors caused by reusing an answer to a "latest"-type question.

## C3. A standard token bucket breaks the rate gate
*2026-10-19, before writing the rate limiter*

**Problem.** See F8: capacity 10 with refill 10/s admits up to 20 requests in one second.

**Resolution (pending).** Either a sliding window (at most 10 in any 1-second window) or a bucket
with no burst (one request per 100 ms), probably with a soft cap below SEC's limit, e.g. 8 req/s.
Decided when the rate limiter is built.

## C4. Counting upstream requests exactly
*2026-10-19*

**Problem.** Counting `fetch()` calls is not exact. A call can fail before anything is sent, and an
HTTP library can retry behind the scenes.

**Resolution.** Requests are counted at the HTTP layer with an httpx event hook, which fires only
when a request is actually sent. Connectors never retry. Tests check `requests_sent` after failures.

## C5. Merging simultaneous requests safely
*2026-10-19, requirement from a lecture on distributed multi-agent systems*

**Problem.** Identical requests that arrive together must share one upstream fetch. Three edge
cases make this hard:

1. If the fetch fails, every waiting caller must get the error and the in-flight entry must be
   removed; otherwise later requests hang or get an old failure.
2. If the caller that started the fetch is cancelled, the fetch must continue for the others,
   so it has to run as its own task.
3. "Identical" needs normalized keys: CIK `320193` and `0000320193` are the same company.

**Resolution.** (3) is done: the EDGAR connector only accepts 10-digit CIKs and provides key
helpers. (1) and (2) are handled when request merging is built.

## C6. Testing failures, timeouts, cancellation and recovery
*2026-10-19, same lecture*

**Resolution.** Connector tests cover HTTP 404, 429 and 5xx, network timeouts, and unparseable
responses, all offline with a mock transport (done). Merging tests will cover a waiting caller
timing out, cancellation, and success after a failed fetch.

## C7. Keeping the kinds of correctness separate
*2026-10-19, same lecture*

**Problem.** The gateway can be wrong in four different ways: cache answers, coordination,
quota enforcement, and recovery.

**Resolution.** Tests are split by file (`test_cache`, `test_merging`, `test_ratelimit`,
`test_recovery`) so a failure says which kind broke.

## C8. Version IDs are too coarse for some data
*2026-10-22, first live EDGAR run*

**Problem.** See F3: the `submissions` version changes on any filing, including ones that do not
touch financial figures. "Version changed" does not mean "answer changed", so invalidating on the
version alone causes needless refetches.

**Resolution.** Record versions stay as cheap change signals. Answer validity should depend on the
specific facts an answer used. Fact-aware invalidation is a research policy to compare against
TTL and whole-record versioning, not part of the first version.

## C9. Keeping a general-purpose gateway from growing without bound
*2026-10-22*

**Problem.** One connector already raised mutability, coarse versions, fact dependencies, large
responses and eviction. Handling every industry's semantics in the core would make it unmanageable.

**Resolution.** Three layers:

- **Core:** scheduling, quotas, rate limiting, request merging, byte-bounded cache, eviction,
  freshness metadata. It never knows financial concepts.
- **Connectors:** source-specific fetching and capabilities.
- **Optional domain validity policies:** answer dependencies and semantic checks. They return
  valid, invalid, or unknown; unknown falls back to the caller's freshness requirement.

The core promises coordination and transparent freshness. Answer correctness is promised only when
a domain policy and ground truth exist. EDGAR is the only real connector this semester; fake
connectors with different capabilities show the core is reusable, and a test will fail if core
modules import the EDGAR connector.

## Out of scope, by choice

Hot keys, partitioning, replication, and distributed NoSQL databases were considered and left out
of the first version. They matter only when the gateway runs as several instances.