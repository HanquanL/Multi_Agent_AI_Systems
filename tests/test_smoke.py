import pytest

import multi_agent_ai_systems
from multi_agent_ai_systems.config import ConfigError, sec_user_agent


def test_package_has_version():
    assert multi_agent_ai_systems.__version__


def test_sec_user_agent_reads_env(monkeypatch):
    monkeypatch.setenv("SEC_USER_AGENT", "Test User test@example.com")
    assert sec_user_agent() == "Test User test@example.com"


def test_sec_user_agent_missing_raises(monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    with pytest.raises(ConfigError):
        sec_user_agent()
