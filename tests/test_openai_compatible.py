"""OpenAI-compatible provider, tested against fake HTTP responses."""

import json

import httpx
import pytest
from fakes import FakeClock

from multi_agent_ai_systems.cache import RecordCache
from multi_agent_ai_systems.connectors.base import RateLimit, SourceError, ThrottledError
from multi_agent_ai_systems.connectors.openai_compatible import OpenAICompatibleProvider
from multi_agent_ai_systems.inference import InferenceGateway
from multi_agent_ai_systems.llm import GenerationRequest, Message, ProviderLimits
from multi_agent_ai_systems.validity import TTLPolicy

REQUEST = GenerationRequest((Message("user", "hello"),), max_tokens=50)
USAGE = {"prompt_tokens": 3, "completion_tokens": 1}
OK = {
    "choices": [{"message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
    "usage": USAGE,
}
MODELS = {"data": [{"id": "small", "created": 100}]}


def make(handler, **options):
    return OpenAICompatibleProvider(
        "http://llm.test/v1/",
        "small",
        limits=ProviderLimits(RateLimit(60, 60.0)),
        name="test-llm",
        transport=httpx.MockTransport(handler),
        **options,
    )


def replies(body=OK, seen=None):
    def handler(request):
        if seen is not None:
            seen.append(request)
        return httpx.Response(200, json=body)

    return handler


async def test_generate_sends_the_request_and_reads_the_answer():
    seen = []
    async with make(replies(seen=seen)) as provider:
        completion = await provider.generate(REQUEST)

    assert (completion.text, completion.finish_reason) == ("hi", "stop")
    assert (completion.prompt_tokens, completion.completion_tokens) == (3, 1)
    assert str(seen[0].url) == "http://llm.test/v1/chat/completions"
    assert json.loads(seen[0].content) == {
        "model": "small",
        "messages": [{"role": "user", "content": "hello"}],
        "max_tokens": 50,
        "temperature": 0.0,
        "stream": False,
    }


async def test_optional_settings_are_sent_when_given():
    seen = []
    request = GenerationRequest(
        (Message("user", "hello"),), max_tokens=50, top_p=0.5, seed=7, stop=("END",)
    )
    async with make(replies(seen=seen)) as provider:
        await provider.generate(request)
    sent = json.loads(seen[0].content)
    assert (sent["top_p"], sent["seed"], sent["stop"]) == (0.5, 7, ["END"])


async def test_api_key_is_sent_only_when_configured():
    with_key, without_key = [], []
    async with make(replies(seen=with_key), api_key="test-key") as provider:
        await provider.generate(REQUEST)
    async with make(replies(seen=without_key)) as provider:
        await provider.generate(REQUEST)
    assert with_key[0].headers["Authorization"] == "Bearer test-key"
    assert "Authorization" not in without_key[0].headers


async def test_cut_off_answer_keeps_its_finish_reason():
    body = {**OK, "choices": [{"message": {"content": "h"}, "finish_reason": "length"}]}
    async with make(replies(body)) as provider:
        assert (await provider.generate(REQUEST)).finish_reason == "length"


async def test_throttling_reports_retry_after():
    def handler(request):
        return httpx.Response(429, headers={"Retry-After": "5"})

    async with make(handler) as provider:
        with pytest.raises(ThrottledError) as info:
            await provider.generate(REQUEST)
    assert info.value.retry_after == 5.0


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (500, {}),
        (200, {"choices": [], "usage": USAGE}),
        (200, {"choices": OK["choices"]}),  # no usage means no cost accounting
    ],
)
async def test_bad_responses_are_source_errors(status, body):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json=body)

    async with make(handler) as provider:
        with pytest.raises(SourceError):
            await provider.generate(REQUEST)
    assert len(calls) == 1  # never retries by itself


@pytest.mark.parametrize("error", [httpx.ReadTimeout, httpx.ConnectError])
async def test_network_failures_are_source_errors(error):
    def handler(request):
        raise error("failed", request=request)

    async with make(handler) as provider:
        with pytest.raises(SourceError):
            await provider.generate(REQUEST)


async def test_model_version_is_looked_up_once_within_its_ttl():
    seen = []
    async with make(replies(MODELS, seen), clock=FakeClock()) as provider:
        assert await provider.model_version() == "small@100"
        assert await provider.model_version() == "small@100"
    assert len(seen) == 1


async def test_model_update_changes_the_version_after_the_ttl():
    clock = FakeClock()
    created = [100]

    def handler(request):
        return httpx.Response(200, json={"data": [{"id": "small", "created": created[0]}]})

    async with make(handler, clock=clock, version_ttl=300.0) as provider:
        assert await provider.model_version() == "small@100"
        created[0] = 200
        clock.advance(301)
        assert await provider.model_version() == "small@200"


async def test_model_not_served_is_an_error():
    async with make(replies({"data": [{"id": "other", "created": 1}]})) as provider:
        with pytest.raises(SourceError, match="does not serve"):
            await provider.model_version()


async def test_pinned_version_needs_no_lookup():
    seen = []
    async with make(replies(seen=seen), version="release-1") as provider:
        assert await provider.model_version() == "release-1"
    assert seen == []


async def test_last_known_version_survives_a_failed_refresh():
    clock = FakeClock()
    responses = [httpx.Response(200, json=MODELS), httpx.Response(503)]

    async with make(lambda request: responses.pop(0), clock=clock) as provider:
        assert await provider.model_version() == "small@100"
        clock.advance(301)
        assert await provider.model_version() == "small@100"


async def test_gateway_makes_one_http_generation_for_a_repeat():
    posts = []

    def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=MODELS)
        posts.append(request)
        return httpx.Response(200, json=OK)

    clock = FakeClock()
    provider = make(handler, clock=clock)
    llm = InferenceGateway(
        provider, cache=RecordCache(1_000_000), policy=TTLPolicy(3600), clock=clock
    )
    first = await llm.generate(REQUEST)
    second = await llm.generate(REQUEST)
    await llm.close()
    await provider.aclose()

    assert (first.source, second.source) == ("upstream", "cache")
    assert second.model_version == "small@100"
    assert len(posts) == 1