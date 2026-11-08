"""The gateway's LLM path: reuse a generated response instead of paying for it again.

generate() serves an exact-match stored response when its lifetime allows, otherwise makes one
merged, rate-limited provider call. Only reusable requests are cached or merged. Responses are
private to the workload that asked. Generated text is unverified, whoever reuses it.
"""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from multi_agent_ai_systems.allocation import DEFAULT_WORKLOAD, AllocationPolicy
from multi_agent_ai_systems.cache import RecordCache
from multi_agent_ai_systems.clock import Clock, RealClock
from multi_agent_ai_systems.connectors.base import (
    Mutability,
    Record,
    SourceError,
    ThrottledError,
)
from multi_agent_ai_systems.llm import (
    Completion,
    GenerationRequest,
    InferenceProvider,
    request_key,
)
from multi_agent_ai_systems.merging import RequestMerger
from multi_agent_ai_systems.ratelimit import RateLimiter
from multi_agent_ai_systems.validity import Validity, ValidityPolicy


@dataclass(frozen=True)
class Generation:
    text: str
    prompt_tokens: int
    completion_tokens: int
    finish_reason: str
    model: str
    model_version: str
    source: str  # "cache", "merged" or "upstream"
    age_seconds: float  # how long the response has been stored; 0 if just generated
    answer_kind: str = "generated"  # unverified, unlike an answer computed from source data


@dataclass
class InferenceCounts:
    cache_hits: int = 0
    merged: int = 0  # joined a generation already in flight
    misses: int = 0  # looked in the cache and had to go on
    calls: int = 0  # generations this workload started; joiners are not charged
    prompt_tokens_spent: int = 0
    completion_tokens_spent: int = 0
    prompt_tokens_avoided: int = 0
    completion_tokens_avoided: int = 0
    generation_seconds_spent: float = 0.0
    generation_seconds_avoided: float = 0.0
    overhead_seconds: float = 0.0  # delay the gateway added: lookups, queueing, backoff


@dataclass
class InferenceStats:
    by_workload: dict[str, InferenceCounts] = field(default_factory=dict)  # per quota bucket

    def workload(self, bucket: str) -> InferenceCounts:
        return self.by_workload.setdefault(bucket, InferenceCounts())


@dataclass
class _Ticket:
    """Tells a caller whether its own call started the generation."""

    started: bool = False


class InferenceGateway:
    def __init__(
        self,
        provider: InferenceProvider,
        *,
        cache: RecordCache,
        policy: ValidityPolicy,
        safety_factor: float = 1.0,
        max_attempts: int = 1,
        backoff_seconds: float = 1.0,
        allocation: AllocationPolicy | None = None,
        clock: Clock | None = None,
    ):
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self._clock = clock or RealClock()
        self.provider = provider
        self.cache = cache
        self.policy = policy  # the response lifetime, separate from the data records' TTL
        self.max_attempts = max_attempts
        self.backoff_seconds = backoff_seconds
        self.limiter = RateLimiter(
            provider.limits.requests,
            safety_factor=safety_factor,
            clock=self._clock,
            policy=allocation,
        )
        self._merger = RequestMerger(self._generate_and_store)
        self._stored_at: dict[str, float] = {}  # gateway-clock time each response was stored
        self.attempts = 0
        self.stats = InferenceStats()

    async def generate(
        self,
        request: GenerationRequest,
        *,
        max_age: float | None = None,
        workload: str = DEFAULT_WORKLOAD,
        agent: str = "anonymous",
    ) -> Generation:
        """workload decides who may reuse this response and whose share of the provider's
        limits a call uses. Cache hits and joining a call in flight use none."""
        start = self._clock.now()
        counts = self._counts(workload)
        version = await self.provider.model_version()

        if not request.reusable:
            completion, seconds = await self._call(request, workload, agent)
            data = _data(completion, seconds)
            _spend(counts, data)
            self._add_overhead(counts, start, seconds)
            return self._generation(data, version, "upstream", 0.0)

        key = request_key(
            request,
            scope=workload,  # the raw name: sharing a quota bucket must not share answers
            provider=self.provider.name,
            model=self.provider.model,
            model_version=version,
        )
        record = self.cache.get(key)
        if record is not None:
            age = self._clock.now() - self._stored_at[key]
            verdict = self.policy.check(record, age, Mutability.OVERWRITTEN)
            if verdict is Validity.VALID and (max_age is None or age <= max_age):
                counts.cache_hits += 1
                _avoid(counts, record.data)
                self._add_overhead(counts, start, 0.0)
                return self._generation(record.data, version, "cache", age)

        counts.misses += 1
        ticket = _Ticket()
        record = await self._merger.get(key, ticket, request, workload, agent, version)
        if ticket.started:
            self._add_overhead(counts, start, record.data["generation_seconds"])
            return self._generation(record.data, version, "upstream", 0.0)
        counts.merged += 1
        _avoid(counts, record.data)
        return self._generation(record.data, version, "merged", 0.0)

    async def close(self) -> None:
        await self._merger.close()
        self.limiter.close()

    async def _generate_and_store(
        self,
        key: str,
        ticket: _Ticket,
        request: GenerationRequest,
        workload: str,
        agent: str,
        version: str,
    ) -> Record:
        """Runs once per merged generation, charged to the workload that started it."""
        ticket.started = True
        completion, seconds = await self._call(request, workload, agent)
        data = _data(completion, seconds)
        _spend(self._counts(workload), data)
        record = Record(
            key=key,
            data=data,
            version=version,
            size_bytes=len(json.dumps(data).encode()),
            fetched_at=datetime.now(UTC),
        )
        # A response cut off by max_tokens is returned but never reused.
        if completion.finish_reason == "stop" and self.cache.put(record):
            self._stored_at[key] = self._clock.now()
        return record

    async def _call(
        self, request: GenerationRequest, workload: str, agent: str
    ) -> tuple[Completion, float]:
        """Every attempt waits for the provider's limiter. Returns the completion and how
        long the successful attempt took."""
        attempt = 0
        while True:
            attempt += 1
            await self.limiter.acquire(workload, agent)
            self.attempts += 1
            start = self._clock.now()
            try:
                completion = await self.provider.generate(request)
            except SourceError as exc:
                if attempt >= self.max_attempts:
                    raise
                await self._clock.sleep(self._retry_delay(exc))
                continue
            return completion, self._clock.now() - start

    def _retry_delay(self, exc: SourceError) -> float:
        if isinstance(exc, ThrottledError) and exc.retry_after is not None:
            return exc.retry_after
        return self.backoff_seconds

    def _counts(self, workload: str) -> InferenceCounts:
        return self.stats.workload(self.limiter.policy.bucket(workload))

    def _add_overhead(self, counts: InferenceCounts, start: float, generation: float) -> None:
        counts.overhead_seconds += self._clock.now() - start - generation

    def _generation(
        self, data: dict[str, Any], version: str, source: str, age: float
    ) -> Generation:
        return Generation(
            text=data["text"],
            prompt_tokens=data["prompt_tokens"],
            completion_tokens=data["completion_tokens"],
            finish_reason=data["finish_reason"],
            model=self.provider.model,
            model_version=version,
            source=source,
            age_seconds=age,
        )


def _data(completion: Completion, seconds: float) -> dict[str, Any]:
    return {
        "text": completion.text,
        "prompt_tokens": completion.prompt_tokens,
        "completion_tokens": completion.completion_tokens,
        "finish_reason": completion.finish_reason,
        "generation_seconds": seconds,
    }


def _spend(counts: InferenceCounts, data: dict[str, Any]) -> None:
    counts.calls += 1
    counts.prompt_tokens_spent += data["prompt_tokens"]
    counts.completion_tokens_spent += data["completion_tokens"]
    counts.generation_seconds_spent += data["generation_seconds"]


def _avoid(counts: InferenceCounts, data: dict[str, Any]) -> None:
    counts.prompt_tokens_avoided += data["prompt_tokens"]
    counts.completion_tokens_avoided += data["completion_tokens"]
    counts.generation_seconds_avoided += data["generation_seconds"]