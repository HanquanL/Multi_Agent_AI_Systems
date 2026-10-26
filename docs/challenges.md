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

**Status (2026-10-23).** Record-level validity is built: `TTLPolicy` treats immutable records as
always valid and expires overwritten ones after a TTL. Answer-level dependency tracking is not built
yet; it belongs in an optional domain validity policy (see C9), which the gateway can already accept
through the `ValidityPolicy` interface.

## C3. A standard token bucket breaks the rate gate
*2026-10-19, before writing the rate limiter*

**Problem.** See F8: capacity 10 with refill 10/s admits up to 20 requests in one second.

**Resolution.** A sliding-window limiter: a request is admitted only if fewer than N were admitted
in the previous window. A safety factor keeps us below the source's real limit (0.8 for EDGAR,
i.e. 8 per rolling second). One limiter is shared by every agent and every attempt, retries
included. Queued requests use no capacity; a request uses capacity when it is admitted, right before
it is sent. Cancelled and timed-out waiters leave without using capacity, and `close()` rejects
queued and new requests on shutdown. Even spacing (pacing) can be added later without replacing
the algorithm.

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

**Resolution.** `RequestMerger` keeps one in-flight task per key.

1. When the fetch finishes, successfully or not, a done-callback removes the key, and `get()`
   ignores a task that has already finished. The next request always starts a fresh fetch.
2. The fetch runs as its own task, and callers wait on it through `asyncio.shield`, so cancelling
   or timing out one caller only ends that caller's wait.
3. The EDGAR connector only accepts 10-digit CIKs and provides key helpers, so the merger can treat
   keys as plain strings and stay domain-free.

The merger sits in front of `Upstream`, the single path to a connector, so 10 concurrent requests
for one key cost one limiter slot and one upstream request (F11).

## C6. Testing failures, timeouts, cancellation and recovery
*2026-10-19, same lecture*

**Resolution.** Covered at every layer built so far, all offline:

- **Connector** (`test_edgar.py`): HTTP 404, 429 and 5xx, network timeouts, unparseable responses;
  none of them retried.
- **Rate limiter** (`test_ratelimit.py`): cancelled and timed-out waiters use no capacity and do not
  block the queue; `close()` rejects queued and new callers.
- **Merging and retries** (`test_recovery.py`): a failure reaches every waiter and does not stick;
  cancelling the first caller or timing out a caller leaves the fetch running for the others;
  `close()` cancels in-flight fetches; every retry passes the limiter; missing records are not
  retried; throttled retries wait for `retry_after`; retries stop at `max_attempts`.

Restart recovery (requests queued when the gateway restarts) is not covered yet. The in-memory
design simply loses them, and callers see an error.

## C7. Keeping the kinds of correctness separate
*2026-10-19, same lecture*

**Problem.** The gateway can be wrong in four different ways: cache answers, coordination,
quota enforcement, and recovery.

**Resolution.** Tests are split by file so a failure says which kind broke: `test_cache.py`
(freshness and answer correctness), `test_merging.py` (coordination), `test_ratelimit.py` (quota
enforcement), `test_eviction.py` (storage limits), and `test_recovery.py` (recovery).

## C8. Version IDs are too coarse for some data
*2026-10-22, first live EDGAR run*

**Problem.** See F3: the `submissions` version changes on any filing, including ones that do not
touch financial figures. "Version changed" does not mean "answer changed", so invalidating on the
version alone causes needless refetches.

**Resolution.** Record versions stay as cheap change signals. Answer validity should depend on the
specific facts an answer used. Fact-aware invalidation is a research policy to compare against
TTL and whole-record versioning, not part of the first version.

**Known effect on measurement.** The gateway labels a refetch "changed" when the record version
differs. For `submissions`, a Form 4 counts as a change, so `REFETCH_CHANGED` overcounts changes that
matter to financial answers, and `REFETCH_UNCHANGED` (pure waste) undercounts. The request profiling
must report this caveat until a fact-aware policy exists.

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
connectors with different capabilities show the core is reusable. `test_boundaries.py` reads the
imports of every core module (without running them) and fails if any of them imports the EDGAR
connector.

## C10. Clock resolution can break a strict rate limit
*2026-10-23, from a limiter test failure*

**Problem.** See F10: on Windows with Python 3.11, `time.monotonic()` only ticks every 15.6 ms.
That is enough to let one extra request into a 1-second window at the edges.

**Resolution.** The real clock uses `time.perf_counter()` (100 ns resolution). Lesson: a
correctness guarantee depends on the resolution of the clock, not just the algorithm.

## C11. Bounded storage without losing durable data
*2026-10-22, design requirement; built 2026-10-23*

**Problem.** One company's `companyfacts` is about 3.8 MB (F5), so an unbounded cache does not work
for hundreds of companies, and counting entries says nothing about memory. Eviction must also never
touch durable data such as configuration, quotas, or experiment results.

**Resolution.** `RecordCache` budgets by bytes (each record's `size_bytes`, the response body
length). When an insert would cross the high watermark (90%), it evicts down to the low watermark
(80%) before inserting, so usage never reaches the hard capacity. Records larger than the low
watermark are served but not cached. The eviction policy is a replaceable interface (LRU by default,
FIFO included to prove it swaps). The cache holds only disposable source data; durable data lives
elsewhere, so eviction cannot reach it. Evictions are counted separately from invalidations.

## C12. Counting why each upstream fetch happened, exactly once
*2026-10-23, while building the gateway*

**Problem.** The request profiling needs a reason for every upstream request: first miss, refetch
of unchanged data (waste), refetch after a real change, or refetch after eviction (waste). With
request merging, ten callers can share one fetch; classifying per caller would count one request
ten times. And at the moment of a miss, the gateway cannot yet know whether the data changed.

**Resolution.** The cache update and the classification run inside the merged fetch
(`Gateway._fetch_and_store`), so each upstream fetch is classified once. The changed/unchanged
decision is made after the refetch by comparing the old and new record versions. Evicted and
too-large keys are tracked so their refetches get their own reasons. Caveat: record versions are
noisy for some data (C8).

## C13. The transport decides whether anything is shared
*2026-10-26, while building the MCP server*

**Problem.** MCP's default transport, stdio, makes each agent start its own server process. Every
agent would then get its own gateway: its own cache, its own rate limiter, no request merging, and
no combined limit on what all agents send to the source. The whole point of the project would be
lost without any error. A second trap: the SDK's per-connection startup hook runs once per
client, so a gateway created there is not shared either.

**Resolution.** The gateway is built once, outside the server, and `build_server(gateway)` only
wraps it in tools. For many agents the server runs over streamable HTTP (one process at
`127.0.0.1:8000/mcp`); stdio remains for single-agent use. A test connects three MCP clients at
once and checks that they share one fetch (F17).

## Out of scope, by choice

Hot keys, partitioning, replication, and distributed NoSQL databases were considered and left out
of the first version. They matter only when the gateway runs as several instances.