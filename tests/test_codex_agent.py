import subprocess
from types import SimpleNamespace

import pytest

from paper_collector.codex_agent import AgentError, AgentUnavailableError, CodexAgent


def test_transient_provider_error_retries_current_request_without_stale_json(
    config, tmp_path, monkeypatch
):
    output = tmp_path / "result.json"
    output.write_text('{"stale":true}')
    calls = []
    sleeps = []

    def run(*_args, **kwargs):
        assert not output.exists()
        assert 0 < kwargs["timeout"] <= config.codex_timeout_seconds
        calls.append(kwargs["input"])
        if len(calls) == 1:
            return SimpleNamespace(
                returncode=1, stderr="ERROR: We’re currently experiencing high demand", stdout=""
            )
        output.write_text('{"status":"ok"}')
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr("paper_collector.codex_agent.subprocess.run", run)
    monkeypatch.setattr("paper_collector.codex_agent.time.sleep", sleeps.append)
    assert CodexAgent(config)._run("same paper", tmp_path / "schema.json", output) == {
        "status": "ok"
    }
    assert calls == ["same paper", "same paper"]
    assert sleeps == [30]


def test_provider_overload_is_bounded_and_remains_distinct_from_paper_failure(
    config, tmp_path, monkeypatch
):
    calls = []
    sleeps = []

    def run(*args, **_kwargs):
        calls.append(args)
        return SimpleNamespace(
            returncode=1, stderr="ERROR: service temporarily unavailable", stdout=""
        )

    monkeypatch.setattr("paper_collector.codex_agent.subprocess.run", run)
    monkeypatch.setattr("paper_collector.codex_agent.time.sleep", sleeps.append)
    with pytest.raises(AgentUnavailableError, match="temporarily unavailable"):
        CodexAgent(config)._run("paper", tmp_path / "schema.json", tmp_path / "out.json")
    assert len(calls) == 4
    assert sleeps == [30, 90, 180]


def test_unsupported_model_fails_without_pointless_capacity_retries(config, tmp_path, monkeypatch):
    sleeps = []
    monkeypatch.setattr(
        "paper_collector.codex_agent.subprocess.run",
        lambda *_a, **_k: SimpleNamespace(
            returncode=1, stderr="ERROR: model is not supported with a ChatGPT account", stdout=""
        ),
    )
    monkeypatch.setattr("paper_collector.codex_agent.time.sleep", sleeps.append)
    with pytest.raises(AgentError, match="not supported") as error:
        CodexAgent(config)._run("paper", tmp_path / "schema.json", tmp_path / "out.json")
    assert not isinstance(error.value, AgentUnavailableError)
    assert not sleeps


def test_total_request_timeout_is_classified_as_service_failure(config, tmp_path, monkeypatch):
    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("codex", 900)

    monkeypatch.setattr("paper_collector.codex_agent.subprocess.run", timeout)
    with pytest.raises(AgentUnavailableError, match="总预算"):
        CodexAgent(config)._run("paper", tmp_path / "schema.json", tmp_path / "out.json")


def test_ranking_prompt_contains_actual_date(config, tmp_path, monkeypatch):
    from datetime import UTC, datetime
    from pathlib import Path

    config.repo_root = Path(__file__).parents[1]
    agent = CodexAgent(config)
    prompts = []

    def run(prompt, *_args):
        prompts.append(prompt)
        return {"selected": []}

    monkeypatch.setattr(agent, "_run", run)
    assert agent.rank([], tmp_path) == []
    assert datetime.now(UTC).date().isoformat() in prompts[0]


def test_untrusted_prompt_echo_does_not_turn_an_unsupported_model_into_transient_error(
    config, tmp_path, monkeypatch
):
    sleeps = []
    monkeypatch.setattr(
        "paper_collector.codex_agent.subprocess.run",
        lambda *_a, **_k: SimpleNamespace(
            returncode=1,
            stderr="Paper contents: high demand temporarily unavailable\nERROR: model is not supported",
            stdout="",
        ),
    )
    monkeypatch.setattr("paper_collector.codex_agent.time.sleep", sleeps.append)
    with pytest.raises(AgentError) as error:
        CodexAgent(config)._run("paper", tmp_path / "schema.json", tmp_path / "out.json")
    assert not isinstance(error.value, AgentUnavailableError)
    assert not sleeps
