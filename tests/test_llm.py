"""LLM request validation and the exact-match response key (llm.py)."""

import pytest
from fakes import FakeProvider

from multi_agent_ai_systems.connectors.base import SourceError
from multi_agent_ai_systems.llm import GenerationRequest, Message, request_key

HELLO = (Message("user", "hello"),)
IDENTITY = {
    "scope": "team-a",
    "provider": "fake-llm",
    "model": "fake-1",
    "model_version": "v1",
    "context": (),
}


def req(**changes):
    return GenerationRequest(**{"messages": HELLO, "max_tokens": 50, **changes})


def key(request=None, **changes):
    return request_key(request or req(), **{**IDENTITY, **changes})


def test_equal_requests_get_equal_keys():
    assert key() == key(req())
    assert key().startswith("llm/")
    assert "hello" not in key()  # prompts never appear in keys, stats or logs


@pytest.mark.parametrize(
    "change",
    [
        {"scope": "team-b"},
        {"provider": "other"},
        {"model": "fake-2"},
        {"model_version": "v2"},
        {"context": ("fact:NetIncome:FY2024",)},
    ],
)
def test_who_and_which_model_change_the_key(change):
    assert key(**change) != key()


@pytest.mark.parametrize(
    "change",
    [
        {"messages": (Message("user", "Hello"),)},  # exact match: case counts
        {"messages": (Message("system", "hello"),)},
        {"max_tokens": 51},
        {"temperature": 0.7},
        {"top_p": 0.9},
        {"seed": 1},
        {"stop": ("x",)},
    ],
)
def test_every_generation_setting_changes_the_key(change):
    assert key(req(**change)) != key()


def test_integer_and_float_temperature_are_the_same_setting():
    assert key(req(temperature=0)) == key(req(temperature=0.0))


def test_allow_reuse_does_not_change_what_is_asked():
    assert key(req(allow_reuse=True)) == key()


def test_message_boundaries_and_order_matter():
    ab_c = (Message("user", "ab"), Message("user", "c"))
    a_bc = (Message("user", "a"), Message("user", "bc"))
    assert key(req(messages=ab_c)) != key(req(messages=a_bc))
    assert key(req(messages=ab_c)) != key(req(messages=ab_c[::-1]))


@pytest.mark.parametrize(
    ("changes", "reusable"),
    [
        ({}, True),
        ({"temperature": 0.7}, False),
        ({"temperature": 0.7, "allow_reuse": True}, True),
    ],
)
def test_only_deterministic_or_opted_in_requests_are_reusable(changes, reusable):
    assert req(**changes).reusable is reusable


@pytest.mark.parametrize(
    "bad",
    [
        {"messages": ()},
        {"messages": (Message("robot", "hi"),)},
        {"max_tokens": 0},
        {"temperature": -0.1},
    ],
)
def test_invalid_requests_are_rejected(bad):
    with pytest.raises(ValueError):
        req(**bad)


async def test_fake_provider_counts_calls_and_tokens():
    provider = FakeProvider("two words")
    completion = await provider.generate(req())
    assert (completion.text, completion.prompt_tokens, completion.completion_tokens) == (
        "two words",
        1,
        2,
    )
    assert provider.calls == 1


async def test_fake_provider_injects_failures():
    provider = FakeProvider()
    provider.failures.append(SourceError("down"))
    with pytest.raises(SourceError):
        await provider.generate(req())