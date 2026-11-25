"""Semantic reuse: find an earlier answer to a differently worded question and judge whether it
may be served. Domain-agnostic.

Similarity only proposes candidates. Rules can only reject. Only a verifier may call two
questions the same, and the gateway's reuse policy, not the index or a verifier, makes the
final call. Anything uncertain falls back to a normal model call.
"""

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from enum import Enum
from typing import Protocol

from multi_agent_ai_systems.connectors.base import SourceError
from multi_agent_ai_systems.llm import GenerationRequest, InferenceProvider, Message, request_key



def comparison_scope(
    request: GenerationRequest,
    *,
    scope: str,
    provider: str,
    model: str,
    model_version: str,
    context: Sequence[str] = (),
) -> str | None:
    """Two requests may share an answer only if everything except the final user question is
    equal: workload, model and version, settings, earlier messages and context. None when the
    request cannot take part: it is not reusable, or it does not end with a user question."""
    if not request.reusable or request.messages[-1].role != "user":
        return None
    blank = replace(request, messages=(*request.messages[:-1], Message("user", "")))
    key = request_key(
        blank,
        scope=scope,
        provider=provider,
        model=model,
        model_version=model_version,
        context=context,
    )
    return "scope/" + key.removeprefix("llm/")


class Verdict(Enum):
    SAME = "same"
    DIFFERENT = "different"
    UNSURE = "unsure"


@dataclass(frozen=True)
class Judgement:
    verdict: Verdict
    reason: str
    by: str  # which verifier decided


class Verifier(Protocol):
    async def verify(self, question: str, earlier: str) -> Judgement: ...


_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
_WORD = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")
_NEGATIONS = frozenset(
    "not no never none nobody nothing neither nor without cannot can't don't doesn't didn't "
    "isn't aren't wasn't weren't won't wouldn't shouldn't couldn't hasn't haven't".split()
)
_MODALS = frozenset("can could may might must shall should will would ought".split())
_DIRECTIONS = [  # questions must agree on whether they use a word from each group
    frozenset("largest biggest greatest most maximum highest max longest".split()),
    frozenset("smallest least minimum lowest min fewest shortest".split()),
    frozenset("before earlier".split()),
    frozenset("after later".split()),
    frozenset("increase rise grow gain".split()),
    frozenset("decrease fall drop shrink decline loss".split()),
    frozenset("best".split()),
    frozenset("worst".split()),
    frozenset("first".split()),
    frozenset("last".split()),
]


def _words(text: str) -> set[str]:
    return set(_WORD.findall(text.lower().replace("\u2019", "'")))


def rule_conflict(question: str, earlier: str) -> str | None:
    """A reason the two questions cannot need the same answer, or None if no rule fires.
    None is not evidence that they are the same."""
    if sorted(_NUMBER.findall(question)) != sorted(_NUMBER.findall(earlier)):
        return "numbers differ"
    new, old = _words(question), _words(earlier)
    if new & _NEGATIONS != old & _NEGATIONS:
        return "negation differs"
    if new & _MODALS != old & _MODALS:
        return "modal verbs differ"
    if any(bool(new & group) != bool(old & group) for group in _DIRECTIONS):
        return "direction words differ"
    return None


class RuleVerifier:
    """Fast rejection. Never says SAME: passing the rules proves nothing."""

    name = "rules"

    async def verify(self, question: str, earlier: str) -> Judgement:
        reason = rule_conflict(question, earlier)
        if reason is not None:
            return Judgement(Verdict.DIFFERENT, reason, self.name)
        return Judgement(Verdict.UNSURE, "no rule conflict", self.name)

class Embedder(Protocol):
    """Turns a question into a vector. model_version names the embedding model, so vectors from
    different models are never compared."""

    async def model_version(self) -> str: ...

    async def embed(self, text: str) -> tuple[float, ...]: ...


JUDGE_INSTRUCTIONS = (
    "You decide whether two questions can share one answer. Reply SAME only if every correct, "
    "complete answer to question A is also a correct, complete answer to question B, and the "
    "reverse. Reply DIFFERENT if they ask for different information, or about different things, "
    "times, quantities or conditions, or want the answer in a different form. Reply UNSURE "
    "otherwise. Reply with exactly one word: SAME, DIFFERENT or UNSURE."
)


class LLMJudge:
    """Asks a model whether two questions need the same answer. Its SAME is evidence, not proof;
    shadow mode measures how often it is wrong. A failure or an unclear reply is UNSURE."""

    name = "llm-judge"

    def __init__(self, provider: InferenceProvider, *, max_tokens: int = 3):
        self.provider = provider
        self.max_tokens = max_tokens
        self.calls = 0  # what judging itself costs, to subtract from what reuse saves
        self.prompt_tokens = 0
        self.completion_tokens = 0

    async def verify(self, question: str, earlier: str) -> Judgement:
        request = GenerationRequest(
            (
                Message("system", JUDGE_INSTRUCTIONS),
                Message("user", f"Question A: {earlier}\nQuestion B: {question}"),
            ),
            max_tokens=self.max_tokens,
            temperature=0.0,
        )
        self.calls += 1
        try:
            completion = await self.provider.generate(request)
        except SourceError as exc:
            return Judgement(Verdict.UNSURE, f"judge unavailable: {exc}", self.name)
        self.prompt_tokens += completion.prompt_tokens
        self.completion_tokens += completion.completion_tokens
        words = completion.text.split()
        word = words[0].strip(".,!:;\"'").upper() if words else ""
        verdict = {"SAME": Verdict.SAME, "DIFFERENT": Verdict.DIFFERENT}.get(word, Verdict.UNSURE)
        return Judgement(verdict, f"judge replied {completion.text.strip()[:40]!r}", self.name)


class VerifierChain:
    """Runs verifiers in order. The first DIFFERENT ends the chain; otherwise the last verifier
    decides. A chain with no verifier that can say SAME never verifies anything."""

    def __init__(self, *verifiers: Verifier):
        self.verifiers = verifiers

    async def verify(self, question: str, earlier: str) -> Judgement:
        judgement = Judgement(Verdict.UNSURE, "no verifier", "none")
        for verifier in self.verifiers:
            judgement = await verifier.verify(question, earlier)
            if judgement.verdict is Verdict.DIFFERENT:
                break
        return judgement


@dataclass(frozen=True)
class IndexEntry:
    key: str  # the response cache key; the answer itself stays in the cache
    scope: str  # from comparison_scope
    question: str  # the final user question, kept in memory only
    vector: tuple[float, ...]
    embedder: str  # embedding model and version; vectors from different ones never mix


@dataclass(frozen=True)
class Candidate:
    key: str
    question: str
    similarity: float


def _norm(vector: Sequence[float]) -> float:
    return math.sqrt(sum(x * x for x in vector))


class MemoryIndex:
    """Earlier questions, in memory only. Oldest entries are dropped beyond max_entries. Callers
    must check that a candidate's key is still in the response cache before using it."""

    def __init__(self, max_entries: int = 10_000):
        self.max_entries = max_entries
        self._entries: dict[str, tuple[IndexEntry, float]] = {}  # key -> (entry, norm), oldest first

    def __len__(self) -> int:
        return len(self._entries)

    def add(self, entry: IndexEntry) -> None:
        self._entries.pop(entry.key, None)
        self._entries[entry.key] = (entry, _norm(entry.vector))
        while len(self._entries) > self.max_entries:
            del self._entries[next(iter(self._entries))]

    def remove(self, key: str) -> None:
        self._entries.pop(key, None)

    def search(
        self,
        scope: str,
        vector: Sequence[float],
        embedder: str,
        *,
        limit: int = 3,
        min_similarity: float = 0.9,
    ) -> list[Candidate]:
        norm = _norm(vector)
        if norm == 0:
            return []
        found = []
        for entry, entry_norm in self._entries.values():
            if (
                entry.scope != scope
                or entry.embedder != embedder
                or entry_norm == 0
                or len(entry.vector) != len(vector)
            ):
                continue
            dot = sum(a * b for a, b in zip(vector, entry.vector, strict=True))
            similarity = dot / (norm * entry_norm)
            if similarity >= min_similarity:
                found.append(Candidate(entry.key, entry.question, similarity))
        found.sort(key=lambda candidate: candidate.similarity, reverse=True)
        return found[:limit]


class Decision(Enum):
    NO_CANDIDATE = "no_candidate"  # nothing similar enough was found
    REJECTED = "rejected"  # every candidate was judged different
    UNCERTAIN = "uncertain"  # no candidate was judged the same
    VERIFIED = "verified"  # a candidate was judged the same


@dataclass(frozen=True)
class ReuseDecision:
    decision: Decision
    candidate: Candidate | None = None  # the verified one, or the best one examined
    judgement: Judgement | None = None


async def decide(
    question: str, candidates: Sequence[Candidate], verifier: Verifier
) -> ReuseDecision:
    """Check candidates best first. The first judged SAME is VERIFIED. Otherwise REJECTED if
    every candidate was DIFFERENT, else UNCERTAIN, reported with the best candidate."""
    if not candidates:
        return ReuseDecision(Decision.NO_CANDIDATE)
    best: tuple[Candidate, Judgement] | None = None
    all_different = True
    for candidate in candidates:
        judgement = await verifier.verify(question, candidate.question)
        if judgement.verdict is Verdict.SAME:
            return ReuseDecision(Decision.VERIFIED, candidate, judgement)
        best = best or (candidate, judgement)
        all_different = all_different and judgement.verdict is Verdict.DIFFERENT
    decision = Decision.REJECTED if all_different else Decision.UNCERTAIN
    return ReuseDecision(decision, *best)