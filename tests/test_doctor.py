from dataclasses import replace

import pytest

from paper_collector import doctor
from paper_collector.doctor import Check


@pytest.fixture
def claude_config(config):
    return replace(
        config,
        agent_provider="claude",
        agent_model="deepseek-flash",
        claude_base_url="https://api.deepseek.com/anthropic",
        claude_auth_token="super-secret-token",
    )


@pytest.fixture
def patched(monkeypatch):
    commands = []

    def command_check(name, executable, args, needle=None):
        commands.append((name, executable, args, needle))
        return Check(True, name, executable)

    monkeypatch.setattr(doctor, "_command_check", command_check)
    monkeypatch.setattr(doctor, "_network_check", lambda name, url: Check(True, name, url))
    return commands


def by_name(checks, name):
    return next(check for check in checks if check.name == name)


def test_claude_provider_checks_cli_credentials_and_model(claude_config, patched):
    checks = doctor.run_checks(claude_config)
    names = [check.name for check in checks]
    assert "Claude CLI" in names
    assert "Codex CLI" not in names
    credentials = by_name(checks, "Claude 凭据")
    assert credentials.ok
    assert "super-secret-token" not in credentials.detail
    assert by_name(checks, "Claude 模型").detail == "deepseek-flash"
    assert patched[0][1] == claude_config.claude_bin


def test_codex_provider_keeps_codex_check(config, patched):
    checks = doctor.run_checks(config)
    names = [check.name for check in checks]
    assert "Codex CLI" in names
    assert "Claude CLI" not in names


def test_missing_claude_token_is_reported_without_leaking(claude_config, patched):
    checks = doctor.run_checks(replace(claude_config, claude_auth_token=None))
    credentials = by_name(checks, "Claude 凭据")
    assert not credentials.ok
    assert "ANTHROPIC_AUTH_TOKEN" in credentials.detail
    assert "super-secret-token" not in credentials.detail


def test_unconfigured_model_is_informational(claude_config, patched):
    checks = doctor.run_checks(replace(claude_config, agent_model=None))
    model = by_name(checks, "Claude 模型")
    assert model.ok
    assert "默认" in model.detail
