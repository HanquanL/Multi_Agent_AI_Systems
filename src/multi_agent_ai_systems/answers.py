"""Answer cache: reuse a computed answer while everything it relied on still holds.

Domain-agnostic. An answer is stored with its dependencies (dependencies.py). On a repeat
question, each dependency's records are read through the gateway, so they are as fresh as the
record layer's policy and the caller's max_age require, and each probe is evaluated by the
plugin that made it. Reuse needs every probe to return its expected value.
"""

import json
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from multi_agent_ai_systems.allocation import DEFAULT_WORKLOAD
from multi_agent_ai_systems.cache import RecordCache
from multi_agent_ai_systems.connectors.base import Record, SourceError
from multi_agent_ai_systems.dependencies import (
    Dependency,
    dependency_from_json,
    dependency_to_json,
)
from multi_agent_ai_systems.gateway import Gateway

# (probe, bodies of the dependency's records in order) -> the probe's current value.
# Raises ValueError for a probe the plugin does not know.
Evaluator = Callable[[str, Sequence[Any]], Any]


class Outcome(Enum):
    MISS = "miss"  # nothing stored for this question
    MATCH = "match"  # every probe returned its expected value: reuse the answer
    CHANGED = "changed"  # some probe returned something else: compute a new answer
    UNKNOWN = "unknown"  # validity could not be checked: compute a new answer


@dataclass(frozen=True)
class Lookup:
    outcome: Outcome
    answer: Any = None  # set only for MATCH
    changed: tuple[str, ...] = ()  # the probes that no longer match, for CHANGED


@dataclass(frozen=True)
class Check:
    outcome: Outcome  # MATCH, CHANGED or UNKNOWN
    changed: tuple[str, ...] = ()  # the probes that no longer match
    permanent: bool = False  # UNKNOWN that can never be resolved: the probe is not known


class DependencyChecker:
    """Checks dependencies against records read through the gateway, so they are as fresh as
    the record layer's policy and the caller's max_age require."""

    def __init__(self, gateway: Gateway, evaluate: Evaluator):
        self._gateway = gateway
        self._evaluate = evaluate

    async def check(
        self,
        dependencies: Sequence[Dependency],
        *,
        max_age: float | None = None,
        workload: str = DEFAULT_WORKLOAD,
        agent: str = "anonymous",
    ) -> Check:
        bodies: dict[str, Any] = {}  # each record is read once per check
        changed = []
        for dep in dependencies:
            try:
                for k in dep.keys:
                    if k not in bodies:
                        response = await self._gateway.get(
                            k, max_age=max_age, workload=workload, agent=agent
                        )
                        bodies[k] = response.data
                current = self._evaluate(dep.probe, [bodies[k] for k in dep.keys])
            except SourceError:
                return Check(Outcome.UNKNOWN)  # may work later
            except ValueError:
                return Check(Outcome.UNKNOWN, permanent=True)
            if current != dep.expected:
                changed.append(dep.probe)
        if changed:
            return Check(Outcome.CHANGED, changed=tuple(changed))
        return Check(Outcome.MATCH)
    
@dataclass
class AnswerStats:
    outcomes: Counter[Outcome] = field(default_factory=Counter)
    changed_probes: Counter[str] = field(default_factory=Counter)  # by probe name, e.g. "fact.v1"


class AnswerCache:
    """Answers keyed by question, kept in a byte-bounded RecordCache like source records."""

    def __init__(self, gateway: Gateway, evaluate: Evaluator, cache: RecordCache):
        self.checker = DependencyChecker(gateway, evaluate)
        self._cache = cache
        self.stats = AnswerStats()

    def __len__(self) -> int:
        return len(self._cache)

    def put(self, key: str, answer: Any, dependencies: Sequence[Dependency]) -> bool:
        """Store a copy of answer. It must be JSON-compatible. Returns False if too large."""
        body = json.dumps(
            {"answer": answer, "dependencies": [dependency_to_json(d) for d in dependencies]}
        )
        record = Record(
            key=key,
            data=json.loads(body),  # a copy, exactly as it would come back from disk
            version=None,
            size_bytes=len(body.encode()),
            fetched_at=datetime.now(UTC),
        )
        return self._cache.put(record)

    async def get(
        self,
        key: str,
        *,
        max_age: float | None = None,
        workload: str = DEFAULT_WORKLOAD,
        agent: str = "anonymous",
    ) -> Lookup:
        """Check a stored answer against fresh records. Reading a dependency's record may
        fetch it from the source, charged to workload like any other request."""
        record = self._cache.get(key)
        if record is None:
            return self._count(Lookup(Outcome.MISS))
        check = await self.checker.check(
            [dependency_from_json(item) for item in record.data["dependencies"]],
            max_age=max_age,
            workload=workload,
            agent=agent,
        )
        if check.outcome is Outcome.UNKNOWN:
            if check.permanent:
                self._cache.remove(key)
            return self._count(Lookup(Outcome.UNKNOWN))
        if check.outcome is Outcome.CHANGED:
            self._cache.remove(key)
            for probe in check.changed:
                self.stats.changed_probes[probe.partition(":")[0]] += 1
            return self._count(Lookup(Outcome.CHANGED, changed=check.changed))
        return self._count(Lookup(Outcome.MATCH, record.data["answer"]))

    def _count(self, lookup: Lookup) -> Lookup:
        self.stats.outcomes[lookup.outcome] += 1
        return lookup