"""Semantic reuse contracts: what may be compared, what must be rejected, what stays uncertain."""

import pytest
from fakes import FakeProvider

from multi_agent_ai_systems.connectors.base import SourceError
from multi_agent_ai_systems.llm import GenerationRequest, Message
from multi_agent_ai_systems.semantic import (
    Candidate,
    Decision,
    IndexEntry,
    Judgement,
    MemoryIndex,
    RuleVerifier,
    Verdict,
    VerifierChain,
    comparison_scope,
    decide,
    LLMJudge,
)


def req(question, *, system=None, temperature=0.0, max_tokens=50):
    first = (Message("system", system),) if system else ()
    return GenerationRequest(
        (*first, Message("user", question)), max_tokens=max_tokens, temperature=temperature
    )


def scope_of(request, workload="team-a", model_version="v1"):
    return comparison_scope(
        request, scope=workload, provider="p", model="m", model_version=model_version
    )


# --- what may be compared ---


def test_paraphrases_share_a_scope():
    assert scope_of(req("What is the largest planet?")) == scope_of(
        req("Which planet is the biggest?")
    )


@pytest.mark.parametrize(
    "other",
    [
        lambda: scope_of(req("Q"), workload="team-b"),
        lambda: scope_of(req("Q"), model_version="v2"),
        lambda: scope_of(req("Q", system="Answer in French.")),
        lambda: scope_of(req("Q", max_tokens=100)),
    ],
)
def test_anything_but_the_question_changes_the_scope(other):
    assert other() != scope_of(req("Q"))


def test_requests_that_cannot_share_answers_have_no_scope():
    assert scope_of(req("Q", temperature=0.7)) is None
    ends_with_answer = GenerationRequest(
        (Message("user", "Q"), Message("assistant", "A")), max_tokens=50
    )
    assert scope_of(ends_with_answer) is None


# --- what must be rejected, and what stays uncertain ---

MUST_REJECT = [
    ("What is the largest planet in our solar system?", "What is the smallest planet in our solar system?"),
    ("What was the population of France in 2023?", "What was the population of France in 2024?"),
    ("Is Pluto a planet?", "Is Pluto not a planet?"),
    ("Can this file be deleted?", "Should this file be deleted?"),
    ("What happened before the war?", "What happened after the war?"),
]


@pytest.mark.parametrize(("question", "earlier"), MUST_REJECT)
async def test_rules_reject_conflicting_questions(question, earlier):
    judgement = await RuleVerifier().verify(question, earlier)
    assert judgement.verdict is Verdict.DIFFERENT


async def test_rules_never_approve_reuse():
    judgement = await RuleVerifier().verify(
        "Which planet is the biggest?", "What is the largest planet?"
    )
    assert judgement.verdict is Verdict.UNSURE


class FixedJudge:
    name = "judge"

    def __init__(self, verdict):
        self.verdict = verdict
        self.calls = 0

    async def verify(self, question, earlier):
        self.calls += 1
        return Judgement(self.verdict, "fixed", self.name)


async def test_a_rule_conflict_stops_the_chain_before_the_judge():
    judge = FixedJudge(Verdict.SAME)
    judgement = await VerifierChain(RuleVerifier(), judge).verify(
        "What is the largest planet?", "What is the smallest planet?"
    )
    assert judgement.verdict is Verdict.DIFFERENT
    assert judge.calls == 0


async def test_without_a_judge_nothing_is_verified():
    judgement = await VerifierChain(RuleVerifier()).verify(
        "Which planet is the biggest?", "What is the largest planet?"
    )
    assert judgement.verdict is Verdict.UNSURE


# --- the index ---


def entry(key, vector, scope="s", embedder="e"):
    return IndexEntry(key, scope, f"question {key}", tuple(vector), embedder)


def test_search_returns_close_questions_best_first():
    index = MemoryIndex()
    index.add(entry("a", [1, 0]))
    index.add(entry("b", [0.9, 0.1]))
    index.add(entry("c", [0, 1]))
    found = index.search("s", (1, 0), "e", min_similarity=0.9)
    assert [candidate.key for candidate in found] == ["a", "b"]


def test_search_stays_in_scope_and_embedder():
    index = MemoryIndex()
    index.add(entry("a", [1, 0], scope="other"))
    index.add(entry("b", [1, 0], embedder="other"))
    assert index.search("s", (1, 0), "e") == []


def test_removed_and_oldest_entries_are_not_found():
    index = MemoryIndex(max_entries=2)
    for key in "abc":
        index.add(entry(key, [1, 0]))
    index.remove("c")
    assert [candidate.key for candidate in index.search("s", (1, 0), "e")] == ["b"]
    assert len(index) == 1


# --- the decision ---

CANDIDATES = [
    Candidate("k1", "What is the largest planet?", 0.97),
    Candidate("k2", "What is the smallest planet?", 0.95),
]


async def test_no_candidates():
    decision = await decide("Q", [], RuleVerifier())
    assert decision.decision is Decision.NO_CANDIDATE


async def test_verified_only_when_a_verifier_says_same():
    chain = VerifierChain(RuleVerifier(), FixedJudge(Verdict.SAME))
    decision = await decide("Which planet is the biggest?", CANDIDATES, chain)
    assert (decision.decision, decision.candidate.key) == (Decision.VERIFIED, "k1")


async def test_rule_rejections_are_reported_as_rejected():
    decision = await decide("Which planet is the biggest?", [CANDIDATES[1]], RuleVerifier())
    assert decision.decision is Decision.REJECTED
    assert decision.judgement.reason == "direction words differ"


async def test_uncertain_falls_back_with_the_best_candidate():
    decision = await decide("Which planet is the biggest?", CANDIDATES, RuleVerifier())
    assert (decision.decision, decision.candidate.key) == (Decision.UNCERTAIN, "k1")

# --- the LLM judge ---


@pytest.mark.parametrize(
    ("reply", "verdict"),
    [
        ("SAME", Verdict.SAME),
        ("Same.", Verdict.SAME),
        ("DIFFERENT", Verdict.DIFFERENT),
        ("Probably the same", Verdict.UNSURE),
        ("", Verdict.UNSURE),
    ],
)
async def test_judge_accepts_only_a_clear_one_word_reply(reply, verdict):
    judge = LLMJudge(FakeProvider(text=reply))
    assert (await judge.verify("B?", "A?")).verdict is verdict


async def test_judge_failure_is_unsure():
    provider = FakeProvider()
    provider.failures = [SourceError("down")]
    judgement = await LLMJudge(provider).verify("B?", "A?")
    assert judgement.verdict is Verdict.UNSURE
    assert "unavailable" in judgement.reason


async def test_judge_sends_both_questions_and_counts_its_cost():
    provider = FakeProvider(text="SAME")
    judge = LLMJudge(provider)
    await judge.verify("Which planet is the biggest?", "What is the largest planet?")
    sent = provider.requests[0]
    assert (sent.temperature, sent.max_tokens) == (0.0, 3)
    assert "Question A: What is the largest planet?" in sent.messages[1].content
    assert judge.calls == 1
    assert judge.prompt_tokens > 0