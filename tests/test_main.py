"""Startup wiring: the gateway runs without SEC settings unless EDGAR is asked for."""

import pytest
from mcp import Client

from multi_agent_ai_systems.__main__ import build, build_semantic, parse_args
from multi_agent_ai_systems.config import ConfigError
from multi_agent_ai_systems.semantic import ReuseMode


async def tool_names(server):
    async with Client(server) as client:
        return {tool.name for tool in (await client.list_tools()).tools}


async def test_llm_only_start_needs_no_sec_settings(monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    server = build(parse_args(["--llm-model", "small"]))
    assert await tool_names(server) == {"generate", "gateway_stats"}


async def test_edgar_flag_adds_data_and_fact_tools(monkeypatch):
    monkeypatch.setenv("SEC_USER_AGENT", "Test Client test@example.com")
    server = build(parse_args(["--edgar", "--llm-model", "small"]))
    assert await tool_names(server) == {"get_record", "get_fact", "generate", "gateway_stats"}


def test_edgar_without_sec_settings_fails_clearly(monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    with pytest.raises(ConfigError):
        build(parse_args(["--edgar"]))


def test_nothing_to_serve_is_refused():
    with pytest.raises(SystemExit):
        build(parse_args([]))

async def test_semantic_flag_configures_reuse_modes(monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    args = parse_args(["--llm-model", "small", "--semantic", "team-a=active"])
    assert build_semantic(args).modes == {"team-a": ReuseMode.ACTIVE}
    assert await tool_names(build(args)) == {"generate", "gateway_stats"}


def test_bad_semantic_mode_is_refused():
    with pytest.raises(SystemExit):
        parse_args(["--llm-model", "small", "--semantic", "team-a=always"])