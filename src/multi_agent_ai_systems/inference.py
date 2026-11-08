"""The gateway's LLM path: reuse a generated response instead of paying for it again.

generate() serves an exact-match stored response when its lifetime allows, otherwise makes one
merged, rate-limited provider call. Only reusable requests are cached or merged. Responses are
private to the workload that asked. Generated text is unverified, whoever reuses it.
"""

import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Protocol

from multi_agent_ai_systems.allocation import DEFAULT_WORKLOAD, AllocationPolicy
from multi_agent_ai_systems.answers import DependencyChecker, Outcome
from multi_agent_ai_systems.cache import RecordCache
from multi_agent_ai_systems.clock import Clock, RealClock
from multi_agent_ai_systems.connectors.base import (
    Mutability,
    Record,
    SourceError,
    ThrottledError,
)
from multi_agent_ai_systems.dependencies import (
    Dependency,
    dependency_from_json,
    dependency_to_json,
)
from multi_agent_ai_systems.llm import (
    Completion,
    GenerationRequest,
    InferenceProvider,
    Message,
    ModelPrice,
    estimate_tokens,
    request_key,
)
from multi_agent_ai_systems.merging import RequestMerger
from multi_agent_ai_systems.ratelimit import RateLimiter
from multi_agent_ai_systems.store import SQLiteRecordStore
from multi_agent_ai_systems.validity import Validity, ValidityPolicy


@dataclass(frozen=True)
class Generation:
    text: str
    prompt_tokens: int
    completion_tokens: int
    finish_reason: str
    model: str
    model_version: str
    source: str  # "cache" (memory), "store" (disk), "merged" or "upstream"
    age_seconds: float  # how long the response has been stored; 0 if just generated
    answer_kind: str = "generated"  # unverified, unlike an answer computed from source data


class ContextError(Exception):
    """A context specification is invalid or could not be resolved. No LLM call was made."""


@dataclass(frozen=True)
class Grounding:
    text: str  # added to the prompt as a system message
    dependencies: tuple[Dependency, ...]  # what that text relied on


class ContextSource(Protocol):
    """A plugin that resolves context specifications into prompt text the gateway can check
    later. Raises ContextError for specifications it cannot answer."""

    checker: DependencyChecker

    def normalize(self, spec: str) -> str:
        """The canonical form of spec, without fetching anything."""
        ...

    async def ground(
        self,
        specs: Sequence[str],
        *,
        max_age: float | None = None,
        workload: str = DEFAULT_WORKLOAD,
        agent: str = "anonymous",
    ) -> Grounding: ...

@dataclass
class InferenceCounts:
    cache_hits: int = 0
    store_hits: int = 0
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
    invalidated: int = 0  # stored responses dropped because a dependency changed


@dataclass
class InferenceStats:
    by_workload: dict[str, InferenceCounts] = field(default_factory=dict)  # per quota bucket
    changed_probes: Counter[str] = field(default_factory=Counter)  # by probe name

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
        store: SQLiteRecordStore | None = None,
        clock: Clock | None = None,
        context_source: ContextSource | None = None,
        price: ModelPrice | None = None,
    ):
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self._clock = clock or RealClock()
        self.provider = provider
        self.cache = cache
        self.store = store #None: memory only
        self.policy = policy  # the response lifetime, separate from the data records' TTL
        self.max_attempts = max_attempts
        self.backoff_seconds = backoff_seconds
        self.limiter = RateLimiter(
            provider.limits.requests,
            safety_factor=safety_factor,
            clock=self._clock,
            policy=allocation,
        )
        tokens = provider.limits.tokens
        self.token_limiter = (  # a window of tokens; None if the provider sets no such limit
            RateLimiter(tokens, safety_factor=safety_factor, clock=self._clock)
            if tokens is not None
            else None
        )
        self._merger = RequestMerger(self._generate_and_store)
        self._stored_at: dict[str, float] = {}  # gateway-clock time each response was stored
        self.attempts = 0
        self.stats = InferenceStats()
        self.context_source = context_source
        self.price = price  # published prices; None for a local model with no provider bill

    async def generate(
        self,
        request: GenerationRequest,
        *,
        context: Sequence[str] = (),
        max_age: float | None = None,
        data_max_age: float | None = None,
        workload: str = DEFAULT_WORKLOAD,
        agent: str = "anonymous",
    ) -> Generation:
        """context lists specifications the context source resolves and adds to the prompt;
        the response is reused only while what they resolved to still holds. max_age limits
        the stored response's age, data_max_age the age of the records it is checked against.
        workload decides who may reuse this response and whose share of the limits a call
        uses. Cache hits and joining a call in flight use no provider limits."""
        start = self._clock.now()
        counts = self._counts(workload)
        specs = self._normalize(context)
        version = await self.provider.model_version()

        if not request.reusable:
            grounded, _ = await self._ground(request, specs, data_max_age, workload, agent)
            completion, seconds = await self._call(grounded, workload, agent)
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
            context=specs,  # the specifications, not their values
        )
        found = self._find(key, max_age)
        if found is not None and await self._still_valid(
            key, found[0], counts, data_max_age, workload, agent
        ):
            record, age, source = found
            counts.cache_hits += 1
            if source == "store":
                counts.store_hits += 1
            _avoid(counts, record.data)
            self._add_overhead(counts, start, 0.0)
            return self._generation(record.data, version, source, age)

        counts.misses += 1
        ticket = _Ticket()
        record = await self._merger.get(
            key, ticket, request, specs, data_max_age, workload, agent, version
        )
        if ticket.started:
            self._add_overhead(counts, start, record.data["generation_seconds"])
            return self._generation(record.data, version, "upstream", 0.0)
        counts.merged += 1
        _avoid(counts, record.data)
        return self._generation(record.data, version, "merged", 0.0)

    async def close(self) -> None:
        await self._merger.close()
        self.limiter.close()
        if self.token_limiter is not None:
            self.token_limiter.close()

    async def _generate_and_store(
        self,
        key: str,
        ticket: _Ticket,
        request: GenerationRequest,
        specs: tuple[str, ...],
        data_max_age: float | None,
        workload: str,
        agent: str,
        version: str,
    ) -> Record:
        """Runs once per merged generation, charged to the workload that started it."""
        ticket.started = True
        grounded, dependencies = await self._ground(
            request, specs, data_max_age, workload, agent
        )
        completion, seconds = await self._call(grounded, workload, agent)
        data = _data(completion, seconds)
        _spend(self._counts(workload), data)
        if dependencies:
            data["dependencies"] = [dependency_to_json(d) for d in dependencies]
        record = Record(
            key=key,
            data=data,
            version=version,
            size_bytes=len(json.dumps(data).encode()),
            fetched_at=datetime.now(UTC),
        )
        # A response cut off by max_tokens is returned but never reused.
        if completion.finish_reason == "stop":
            if self.store is not None:
                self.store.put(record)
            if self.cache.put(record):
                self._stored_at[key] = self._clock.now()
        return record

    def _normalize(self, context: Sequence[str]) -> tuple[str, ...]:
        if not context:
            return ()
        if self.context_source is None:
            raise ValueError("this gateway has no context source")
        return tuple(self.context_source.normalize(spec) for spec in context)

    async def _ground(
        self,
        request: GenerationRequest,
        specs: tuple[str, ...],
        data_max_age: float | None,
        workload: str,
        agent: str,
    ) -> tuple[GenerationRequest, tuple[Dependency, ...]]:
        """Add what the specifications resolve to as a system message, first in the prompt."""
        if not specs:
            return request, ()
        grounding = await self.context_source.ground(
            specs, max_age=data_max_age, workload=workload, agent=agent
        )
        messages = (Message("system", grounding.text), *request.messages)
        return replace(request, messages=messages), grounding.dependencies

    async def _still_valid(
        self,
        key: str,
        record: Record,
        counts: InferenceCounts,
        data_max_age: float | None,
        workload: str,
        agent: str,
    ) -> bool:
        """A response with dependencies is reused only if every probe still matches."""
        items = record.data.get("dependencies")
        if not items:
            return True
        check = await self.context_source.checker.check(
            [dependency_from_json(item) for item in items],
            max_age=data_max_age,
            workload=workload,
            agent=agent,
        )
        if check.outcome is Outcome.MATCH:
            return True
        if check.outcome is Outcome.CHANGED:
            counts.invalidated += 1
            for probe in check.changed:
                self.stats.changed_probes[probe.partition(":")[0]] += 1
        if check.outcome is Outcome.CHANGED or check.permanent:
            self.cache.remove(key)
            if self.store is not None:
                self.store.remove(key)
        return False
    
    def _find(self, key: str, max_age: float | None) -> tuple[Record, float, str] | None:
        """A stored response that may still be served: (record, age, where it was)."""
        record = self.cache.get(key)
        if record is not None:
            age = self._clock.now() - self._stored_at[key]
            return (record, age, "cache") if self._servable(record, age, max_age) else None
        if self.store is None:
            return None
        stored = self.store.get(key)
        if stored is None or not self._servable(stored.record, stored.age_seconds, max_age):
            return None
        if self.cache.put(stored.record):  # keeps its age, so a restart cannot extend its life
            self._stored_at[key] = self._clock.now() - stored.age_seconds
        return stored.record, stored.age_seconds, "store"

    def _servable(self, record: Record, age: float, max_age: float | None) -> bool:
        verdict = self.policy.check(record, age, Mutability.OVERWRITTEN)
        return verdict is Validity.VALID and (max_age is None or age <= max_age)

    async def _call(
        self, request: GenerationRequest, workload: str, agent: str
    ) -> tuple[Completion, float]:
        """Every attempt waits for the provider's limiter. Returns the completion and how
        long the successful attempt took."""
        attempt = 0
        while True:
            attempt += 1
            if self.token_limiter is not None:
                await self.token_limiter.acquire(
                    workload, agent, cost=estimate_tokens(request)
                )
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