"""The LLM path (inference.py): reuse, merging, privacy, limits and cost accounting."""

import asyncio

import pytest
from fakes import FakeClock, FakeProvider

from multi_agent_ai_systems.cache import RecordCache
from multi_agent_ai_systems.connectors.base import SourceError, ThrottledError
from multi_agent_ai_systems.inference import InferenceGateway
from multi_agent_ai_systems.llm import GenerationRequest, Message, ModelPrice
from multi_agent_ai_systems.validity import TTLPolicy
from multi_agent_ai_systems.connectors.base import RateLimit, SourceError, ThrottledError
from multi_agent_ai_systems.llm import (
    GenerationRequest,
    Message,
    ModelPrice,
    ProviderLimits,
    estimate_tokens,
)

DAY = 86_400.0
HELLO = (Message("user", "hello"),)


def req(**changes):
    return GenerationRequest(**{"messages": HELLO, "max_tokens": 50, **changes})


def make(provider=None, **kwargs):
    clock = FakeClock()
    provider = provider or FakeProvider()
    gateway = InferenceGateway(
        provider, cache=RecordCache(1_000_000), policy=TTLPolicy(DAY), clock=clock, **kwargs
    )
    return gateway, provider, clock


async def test_repeat_request_is_served_without_another_call():
    gateway, provider, _ = make()
    first = await gateway.generate(req())
    second = await gateway.generate(req(), agent="another-agent")  # same workload shares
    assert (first.source, second.source) == ("upstream", "cache")
    assert (first.text, second.text) == ("an answer", "an answer")
    assert second.answer_kind == "generated"
    assert provider.calls == 1
    counts = gateway.stats.workload("default")
    assert (counts.calls, counts.cache_hits) == (1, 1)
    assert (counts.prompt_tokens_spent, counts.completion_tokens_spent) == (1, 2)
    assert (counts.prompt_tokens_avoided, counts.completion_tokens_avoided) == (1, 2)


async def test_concurrent_identical_requests_share_one_generation():
    gateway, provider, _ = make()
    provider.gate = asyncio.Event()
    tasks = [asyncio.create_task(gateway.generate(req())) for _ in range(3)]
    counts = gateway.stats.workload("default")
    for _ in range(100):
        if counts.misses == 3:
            break
        await asyncio.sleep(0.01)
    provider.gate.set()
    results = await asyncio.gather(*tasks)

    assert sorted(r.source for r in results) == ["merged", "merged", "upstream"]
    assert provider.calls == 1
    assert (counts.calls, counts.merged) == (1, 2)
    assert counts.completion_tokens_avoided == 4


async def test_workloads_never_share_responses():
    gateway, provider, _ = make()  # both land in the same "default" quota bucket
    await gateway.generate(req(), workload="team-a")
    second = await gateway.generate(req(), workload="team-b")
    assert second.source == "upstream"
    assert provider.calls == 2


async def test_random_requests_are_never_reused():
    gateway, provider, _ = make()
    await gateway.generate(req(temperature=0.7))
    second = await gateway.generate(req(temperature=0.7))
    assert second.source == "upstream"
    assert provider.calls == 2
    assert len(gateway.cache) == 0
    assert gateway.stats.workload("default").calls == 2


async def test_caller_can_opt_in_to_reusing_a_random_request():
    gateway, provider, _ = make()
    await gateway.generate(req(temperature=0.7, allow_reuse=True))
    second = await gateway.generate(req(temperature=0.7, allow_reuse=True))
    assert second.source == "cache"
    assert provider.calls == 1


async def test_expired_response_is_generated_again():
    gateway, provider, clock = make()
    await gateway.generate(req())
    clock.advance(DAY + 1)
    assert (await gateway.generate(req())).source == "upstream"
    assert provider.calls == 2


async def test_max_age_refuses_older_responses():
    gateway, provider, clock = make()
    await gateway.generate(req())
    clock.advance(60)
    hit = await gateway.generate(req())
    assert (hit.source, hit.age_seconds) == ("cache", 60)
    assert (await gateway.generate(req(), max_age=30)).source == "upstream"
    assert provider.calls == 2


async def test_model_update_does_not_reuse_old_answers():
    gateway, provider, _ = make()
    await gateway.generate(req())
    provider.version = "v2"
    second = await gateway.generate(req())
    assert (second.source, second.model_version) == ("upstream", "v2")
    assert provider.calls == 2


async def test_truncated_response_is_returned_but_not_reused():
    gateway, provider, _ = make()
    provider.finish_reason = "length"
    first = await gateway.generate(req())
    second = await gateway.generate(req())
    assert (first.finish_reason, second.source) == ("length", "upstream")
    assert provider.calls == 2


async def test_failure_is_not_cached():
    gateway, provider, _ = make()
    provider.failures.append(SourceError("down"))
    with pytest.raises(SourceError):
        await gateway.generate(req())
    assert (await gateway.generate(req())).source == "upstream"
    assert gateway.stats.workload("default").calls == 1  # the failed call spent nothing


async def test_throttled_call_is_retried_after_the_providers_delay():
    gateway, provider, clock = make(max_attempts=2)
    provider.failures.append(ThrottledError("slow down", retry_after=5.0))
    assert (await gateway.generate(req())).source == "upstream"
    assert (provider.calls, gateway.attempts) == (2, 2)
    assert clock.now() == pytest.approx(5.0)
    assert gateway.limiter.stats["default"].admitted == 2  # each attempt passed the limiter


async def test_cache_hit_uses_no_provider_quota():
    gateway, _, _ = make()
    await gateway.generate(req())
    await gateway.generate(req())
    assert gateway.limiter.stats["default"].admitted == 1


class SlowProvider(FakeProvider):
    """Takes a fixed time on the fake clock."""

    def __init__(self, clock, seconds):
        super().__init__()
        self.clock, self.seconds = clock, seconds

    async def generate(self, request):
        self.clock.advance(self.seconds)
        return await super().generate(request)


async def test_generation_time_is_counted_apart_from_overhead():
    clock = FakeClock()
    gateway = InferenceGateway(
        SlowProvider(clock, 2.0), cache=RecordCache(1_000_000), policy=TTLPolicy(DAY), clock=clock
    )
    await gateway.generate(req())
    await gateway.generate(req())
    counts = gateway.stats.workload("default")
    assert counts.generation_seconds_spent == pytest.approx(2.0)
    assert counts.generation_seconds_avoided == pytest.approx(2.0)
    assert counts.overhead_seconds == pytest.approx(0.0)


def test_price_gives_an_estimate_from_tokens():
    price = ModelPrice(input_per_million=1.0, output_per_million=2.0)
    assert price.usd(1_000_000, 500_000) == pytest.approx(2.0)

class TokenLimitedProvider(FakeProvider):
    limits = ProviderLimits(
        requests=RateLimit(max_requests=100, period_seconds=60.0),
        tokens=RateLimit(max_requests=100, period_seconds=60.0),  # 100 tokens a minute
    )


def test_estimate_reserves_the_whole_output_allowance():
    assert estimate_tokens(req()) == 55  # "hello": 1 + 4 per message, plus max_tokens 50


async def test_call_waits_when_the_token_limit_is_used_up():
    gateway, _, clock = make(TokenLimitedProvider())
    await gateway.generate(req())  # reserves 55
    assert clock.now() == 0
    await gateway.generate(req(messages=(Message("user", "hello there"),)))  # reserves 56
    assert clock.now() == pytest.approx(60.0)


async def test_request_larger_than_the_token_limit_is_rejected():
    gateway, provider, _ = make(TokenLimitedProvider())
    with pytest.raises(ValueError):
        await gateway.generate(req(max_tokens=200))
    assert provider.calls == 0


async def test_cache_hit_reserves_no_tokens():
    gateway, _, clock = make(TokenLimitedProvider())
    await gateway.generate(req())
    await gateway.generate(req())
    assert clock.now() == 0
    assert gateway.token_limiter.stats["default"].admitted == 1