"""Semantic reuse inside the inference gateway: served only when verified in active mode."""

from fakes import FakeClock, FakeProvider

from multi_agent_ai_systems.cache import RecordCache
from multi_agent_ai_systems.connectors.base import SourceError
from multi_agent_ai_systems.inference import InferenceGateway
from multi_agent_ai_systems.llm import GenerationRequest, Message
from multi_agent_ai_systems.semantic import (
    Judgement,
    ReuseMode,
    RuleVerifier,
    SemanticReuse,
    Verdict,
    VerifierChain,
    IndexEntry,
    comparison_scope
)
from multi_agent_ai_systems.server import inference_payload
from multi_agent_ai_systems.validity import TTLPolicy


LARGEST = "What is the largest planet?"
BIGGEST = "Which planet is the biggest?"
SMALLEST = "What is the smallest planet?"
VECTORS = {LARGEST: (1.0, 0.0), BIGGEST: (0.99, 0.1), SMALLEST: (0.98, 0.2)}


class FakeEmbedder:
    def __init__(self):
        self.calls = 0
        self.failures = []

    async def model_version(self):
        return "emb-1"

    async def embed(self, text):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return VECTORS[text]


class FixedJudge:
    name = "judge"

    def __init__(self, verdict):
        self.verdict = verdict
        self.calls = 0

    async def verify(self, question, earlier):
        self.calls += 1
        return Judgement(self.verdict, "fixed", self.name)


def make(mode=ReuseMode.ACTIVE, verdict=Verdict.SAME, modes=None,
        cache_bytes=1_000_000):
    provider = FakeProvider()
    embedder = FakeEmbedder()
    judge = FixedJudge(verdict)
    semantic = SemanticReuse(
        embedder, VerifierChain(RuleVerifier(), judge), modes or {"team-a": mode}
    )
    gateway = InferenceGateway(
        provider,
        cache=RecordCache(cache_bytes),
        policy=TTLPolicy(3600),
        clock=FakeClock(),
        semantic=semantic,
    )
    return gateway, provider, embedder, judge, semantic


def ask(question, temperature=0.0):
    return GenerationRequest((Message("user", question),), max_tokens=50, temperature=temperature)


async def test_a_verified_paraphrase_reuses_the_answer_in_active_mode():
    gateway, provider, _, _, _ = make()
    await gateway.generate(ask(LARGEST), workload="team-a")
    result = await gateway.generate(ask(BIGGEST), workload="team-a")

    assert (result.source, result.similar_to) == ("similar", LARGEST)
    assert result.similarity > 0.9
    assert provider.calls == 1
    assert gateway.stats.semantic["verified"] == 1
    assert gateway.stats.workload("default").semantic_hits == 1


async def test_a_rule_conflict_generates_without_asking_the_judge():
    gateway, provider, _, judge, _ = make()
    await gateway.generate(ask(LARGEST), workload="team-a")
    result = await gateway.generate(ask(SMALLEST), workload="team-a")

    assert result.source == "upstream"
    assert provider.calls == 2
    assert judge.calls == 0
    assert gateway.stats.semantic["rejected"] == 1


async def test_an_uncertain_verdict_generates():
    gateway, provider, _, _, _ = make(verdict=Verdict.UNSURE)
    await gateway.generate(ask(LARGEST), workload="team-a")
    result = await gateway.generate(ask(BIGGEST), workload="team-a")

    assert result.source == "upstream"
    assert provider.calls == 2
    assert gateway.stats.semantic["uncertain"] == 1


async def test_shadow_mode_never_serves_but_logs_both_answers():
    gateway, provider, _, _, semantic = make(mode=ReuseMode.SHADOW)
    semantic.show_log = True
    await gateway.generate(ask(LARGEST), workload="team-a")
    result = await gateway.generate(ask(BIGGEST), workload="team-a")

    assert result.source == "upstream"
    assert provider.calls == 2
    entry = semantic.log[-1]
    assert (entry["decision"], entry["served"]) == ("verified", False)
    assert entry["candidate"] == LARGEST
    assert "reused_answer" in entry
    assert "generated_answer" in entry


async def test_workloads_left_exact_pay_nothing():
    gateway, provider, embedder, _, _ = make()
    await gateway.generate(ask(LARGEST), workload="team-b")
    await gateway.generate(ask(BIGGEST), workload="team-b")

    assert provider.calls == 2
    assert embedder.calls == 0


async def test_answers_are_never_shared_across_workloads():
    modes = {"team-a": ReuseMode.ACTIVE, "team-b": ReuseMode.ACTIVE}
    gateway, provider, _, _, _ = make(modes=modes)
    await gateway.generate(ask(LARGEST), workload="team-a")
    result = await gateway.generate(ask(BIGGEST), workload="team-b")

    assert result.source == "upstream"
    assert provider.calls == 2


async def test_an_embedding_failure_falls_back_to_the_model():
    gateway, provider, embedder, _, _ = make()
    await gateway.generate(ask(LARGEST), workload="team-a")
    embedder.failures = [SourceError("embedding server down")]
    result = await gateway.generate(ask(BIGGEST), workload="team-a")

    assert result.source == "upstream"
    assert provider.calls == 2
    assert gateway.stats.semantic["embedding_failed"] == 1


async def test_requests_that_may_not_share_answers_skip_semantic_lookup():
    gateway, _, embedder, _, _ = make()
    await gateway.generate(ask(LARGEST, temperature=0.7), workload="team-a")
    assert embedder.calls == 0

async def test_stats_report_decisions_and_what_judging_cost():
    gateway, _, _, _, semantic = make()
    semantic.show_log = True
    await gateway.generate(ask(LARGEST), workload="team-a")
    await gateway.generate(ask(BIGGEST), workload="team-a")

    payload = inference_payload(gateway)["semantic"]
    assert payload["modes"] == {"team-a": "active"}
    assert payload["decisions"] == {"no_candidate": 1, "verified": 1}
    assert payload["judge_cost"]["calls"] == 1
    assert payload["recent"][-1]["served"] is True

async def test_the_decision_log_keeps_no_text_unless_asked():
    gateway, _, _, _, _ = make()
    await gateway.generate(ask(LARGEST), workload="team-a")
    await gateway.generate(ask(BIGGEST), workload="team-a")

    entry = inference_payload(gateway)["semantic"]["recent"][-1]
    assert entry["decision"] == "verified"
    assert not {"question", "candidate", "reused_answer", "generated_answer"} & entry.keys()

async def test_a_reused_wording_is_an_exact_hit_next_time():
    gateway, provider, _, judge, _ = make()
    await gateway.generate(ask(LARGEST), workload="team-a")
    await gateway.generate(ask(BIGGEST), workload="team-a")
    again = await gateway.generate(ask(BIGGEST), workload="team-a")

    assert again.source == "cache"
    assert judge.calls == 1  # judged once, not on every repeat
    assert provider.calls == 1


async def test_small_requests_skip_the_lookup():
    gateway, provider, embedder, _, semantic = make()
    semantic.min_tokens = 10_000
    await gateway.generate(ask(LARGEST), workload="team-a")
    await gateway.generate(ask(BIGGEST), workload="team-a")

    assert embedder.calls == 0
    assert provider.calls == 2

async def test_shadow_does_not_count_an_invalid_answer_as_reusable():
    gateway, _, _, _, _ = make(mode=ReuseMode.SHADOW)
    await gateway.generate(ask(LARGEST), workload="team-a")

    async def dependencies_changed(*args):
        return False

    gateway._still_valid = dependencies_changed
    await gateway.generate(ask(BIGGEST), workload="team-a")
    assert gateway.stats.semantic["verified_invalid"] == 1
    assert gateway.stats.semantic["verified"] == 0


async def test_answers_that_could_not_be_stored_are_not_indexed():
    gateway, _, _, _, semantic = make(cache_bytes=10)  # too small for any answer
    await gateway.generate(ask(LARGEST), workload="team-a")
    assert len(semantic.index) == 0


async def test_dead_index_entries_do_not_crowd_out_live_ones():
    gateway, _, _, _, semantic = make()
    await gateway.generate(ask(LARGEST), workload="team-a")
    scope = comparison_scope(
        ask(BIGGEST), scope="team-a", provider="fake-llm", model="fake-1", model_version="v1"
    )
    for n in range(3):  # closer than the live answer, but their answers are gone
        semantic.index.add(IndexEntry(f"llm/gone-{n}", scope, "gone", VECTORS[BIGGEST], "emb-1"))

    result = await gateway.generate(ask(BIGGEST), workload="team-a")
    assert (result.source, result.similar_to) == ("similar", LARGEST)
    assert len(semantic.index) == 1  # the dead entries were removed