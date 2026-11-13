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

    monkeypatch.setattr("paper_collector.codex_agent.run_isolated", run)
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

    monkeypatch.setattr("paper_collector.codex_agent.run_isolated", run)
    monkeypatch.setattr("paper_collector.codex_agent.time.sleep", sleeps.append)
    with pytest.raises(AgentUnavailableError, match="temporarily unavailable"):
        CodexAgent(config)._run("paper", tmp_path / "schema.json", tmp_path / "out.json")
    assert len(calls) == 4
    assert sleeps == [30, 90, 180]


def test_unsupported_model_fails_without_pointless_capacity_retries(config, tmp_path, monkeypatch):
    sleeps = []
    monkeypatch.setattr(
        "paper_collector.codex_agent.run_isolated",
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

    monkeypatch.setattr("paper_collector.codex_agent.run_isolated", timeout)
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
    from test_db import make_paper

    assert agent.rank([make_paper("date-test")], tmp_path) == []
    assert datetime.now(UTC).date().isoformat() in prompts[0]


def test_untrusted_prompt_echo_does_not_turn_an_unsupported_model_into_transient_error(
    config, tmp_path, monkeypatch
):
    sleeps = []
    monkeypatch.setattr(
        "paper_collector.codex_agent.run_isolated",
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


def test_note_model_override_does_not_modify_main_agent_configuration(
    config, tmp_path, monkeypatch
):
    config.codex_model = "main-review-model"
    commands = []
    output = tmp_path / "out.json"

    def run(command, **_kwargs):
        commands.append(command)
        output.write_text('{"ok":true}')
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("paper_collector.codex_agent.run_isolated", run)
    agent = CodexAgent(config)
    agent._run("note", tmp_path / "schema", output, model_override="note-model")
    agent._run("review", tmp_path / "schema", output)
    assert commands[0][commands[0].index("--model") + 1] == "note-model"
    assert commands[1][commands[1].index("--model") + 1] == "main-review-model"
    assert config.codex_model == "main-review-model"


def test_invalid_model_unicode_is_a_model_error_and_never_escapes_json_boundary(
    config, tmp_path, monkeypatch
):
    output = tmp_path / "out.json"

    def run(*_args, **_kwargs):
        output.write_text('{"text":"\\ud835"}')
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("paper_collector.codex_agent.run_isolated", run)
    with pytest.raises(AgentError, match="合法 JSON"):
        CodexAgent(config)._run("prompt", tmp_path / "schema", output)


def test_invalid_prompt_is_rejected_before_a_model_request(config, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "paper_collector.codex_agent.run_isolated",
        lambda *_a, **_k: pytest.fail("Do not send invalid text"),
    )
    with pytest.raises(AgentError, match="无效 Unicode"):
        CodexAgent(config)._run("matrix \ud835", tmp_path / "schema", tmp_path / "out.json")


def test_rank_skips_only_invalid_candidate_and_filters_selection_to_actual_payload(
    config, tmp_path, monkeypatch
):
    from pathlib import Path

    from test_db import make_paper

    config.repo_root = Path(__file__).parents[1]
    invalid, valid = make_paper("bad"), make_paper("good")
    invalid.abstract = "bad \ud835"
    prompts = []

    def run(prompt, *_args):
        prompts.append(prompt)
        return {"selected": [{"paper_id": "bad", "rank": 1}, {"paper_id": "good", "rank": 2}]}

    agent = CodexAgent(config)
    monkeypatch.setattr(agent, "_run", run)
    assert [item["paper_id"] for item in agent.rank([invalid, valid], tmp_path)] == ["good"]
    assert '"id": "bad"' not in prompts[0]
    prompts[0].encode("utf-8")


def test_non_utf8_subprocess_output_is_a_typed_agent_error(config, tmp_path, monkeypatch):
    def invalid_output(*_args, **_kwargs):
        b"\xff".decode("utf-8")

    monkeypatch.setattr("paper_collector.codex_agent.run_isolated", invalid_output)
    with pytest.raises(AgentError, match="有效的 UTF-8"):
        CodexAgent(config)._run("prompt", tmp_path / "schema", tmp_path / "out.json")
