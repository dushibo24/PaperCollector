import json
import subprocess
from dataclasses import replace

import pytest

from paper_collector.agent_common import (
    AgentDataError,
    AgentError,
    AgentUnavailableError,
    make_agent,
)
from paper_collector.claude_agent import ClaudeAgent


@pytest.fixture
def claude_config(config):
    return replace(
        config,
        agent_provider="claude",
        agent_model="deepseek-flash",
        claude_base_url="https://api.deepseek.com/anthropic",
        claude_auth_token="test-token",
    )


def envelope(**values):
    payload = {"type": "result", "subtype": "success", "is_error": False}
    payload.update(values)
    return json.dumps(payload)


def completed(stdout, returncode=0, stderr=""):
    return subprocess.CompletedProcess(["claude"], returncode, stdout, stderr)


def capture(monkeypatch, config, responds, *, images=None, output=None, **run_kwargs):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return responds(len(calls), command, kwargs)

    monkeypatch.setattr("paper_collector.agent_common.run_isolated", run)
    output = output or config.path.parent / "out.json"
    schema = output.parent / "schema.json"
    if not schema.exists():
        schema.write_text("{}", encoding="utf-8")
    result = ClaudeAgent(config)._run(
        "prompt", schema, output, images=images, **run_kwargs
    )
    return result, calls


def test_text_command_uses_inline_schema_and_disables_tools(claude_config, tmp_path, monkeypatch):
    schema = tmp_path / "schema.json"
    schema.write_text(
        '{"$schema": "https://json-schema.org/draft/2020-12/schema",'
        ' "type": "object", "properties": {"ok": {"type": "boolean"}}}'
    )
    output = tmp_path / "out.json"

    def respond(_attempt, _command, _kwargs):
        return completed(envelope(structured_output={"ok": True}))

    result, calls = capture(
        monkeypatch, claude_config, respond, output=output
    )

    command, kwargs = calls[0]
    assert result == {"ok": True}
    assert command[0] == claude_config.claude_bin
    assert command[command.index("--json-schema") + 1] == (
        '{"type":"object","properties":{"ok":{"type":"boolean"}}}'
    )
    assert str(schema) not in command
    assert command[command.index("--tools") + 1] == ""
    assert "--add-dir" not in command
    assert command[command.index("--model") + 1] == "deepseek-flash"
    assert kwargs["input"] == "prompt"
    assert kwargs["cwd"] == tmp_path
    assert output.read_text() == '{"ok": true}'


def test_envelope_is_persisted_with_stale_outputs_cleared(
    claude_config, tmp_path, monkeypatch
):
    output = tmp_path / "out.json"
    sidecar = tmp_path / "out.envelope.json"
    output.write_text('{"stale": true}')
    sidecar.write_text('{"stale": true}')

    def respond(_attempt, _command, _kwargs):
        assert not output.exists()
        assert not sidecar.exists()
        return completed(envelope(structured_output={"fresh": True}))

    capture(monkeypatch, claude_config, respond, output=output)
    assert output.read_text() == '{"fresh": true}'
    assert json.loads(sidecar.read_text())["subtype"] == "success"


def test_result_string_is_parsed_when_structured_output_is_absent(
    claude_config, tmp_path, monkeypatch
):
    result, _ = capture(
        monkeypatch,
        claude_config,
        lambda *_args: completed(envelope(result='{"fallback": true}')),
        output=tmp_path / "out.json",
    )
    assert result == {"fallback": True}


def test_image_command_reads_only_listed_paths(claude_config, tmp_path, monkeypatch):
    images = tmp_path / "images"
    images.mkdir()
    first, second = images / "a.png", images / "b.png"
    first.write_bytes(b"png")
    second.write_bytes(b"png")

    def respond(_attempt, _command, kwargs):
        assert str(first.resolve()) in kwargs["input"]
        assert str(second.resolve()) in kwargs["input"]
        return completed(envelope(structured_output={"ok": True}))

    _, calls = capture(
        monkeypatch, claude_config, respond, images=[first, second], output=tmp_path / "out.json"
    )
    command, kwargs = calls[0]
    assert command[command.index("--tools") + 1] == "Read"
    assert command[command.index("--allowedTools") + 1] == "Read"
    assert command[command.index("--add-dir") + 1] == str(images.resolve())
    assert kwargs["cwd"] == images.resolve()
    assert command.count("--add-dir") == 1


def test_environment_is_whitelisted_and_home_is_temporary(
    claude_config, tmp_path, monkeypatch
):
    for name in (
        "SERVERCHAN_SENDKEY",
        "BAIDUPAN_COOKIE",
        "ANTHROPIC_API_KEY",
        "OPENREVIEW_PASSWORD",
        "IEEE_XPLORE_API_KEY",
    ):
        monkeypatch.setenv(name, "secret-value")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://ignored.example")
    seen_home = []

    def respond(_attempt, _command, kwargs):
        env = kwargs["env"]
        assert set(env) <= {
            "PATH",
            "HOME",
            "XDG_CONFIG_HOME",
            "TMPDIR",
            "LANG",
            "LC_ALL",
            "ANTHROPIC_BASE_URL",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_MODEL",
        }
        assert "secret-value" not in json.dumps(env)
        assert env["ANTHROPIC_BASE_URL"] == claude_config.claude_base_url
        assert env["ANTHROPIC_AUTH_TOKEN"] == claude_config.claude_auth_token
        seen_home.append(env["HOME"])
        return completed(envelope(structured_output={"ok": True}))

    capture(monkeypatch, claude_config, respond, output=tmp_path / "out.json")
    home = seen_home[0]
    assert home != claude_config.path.parent
    assert not __import__("pathlib").Path(home).exists()


def test_transient_envelope_failure_retries_same_request(claude_config, tmp_path, monkeypatch):
    sleeps = []
    outputs = []
    monkeypatch.setattr("paper_collector.agent_common.time.sleep", sleeps.append)

    def respond(attempt, _command, _kwargs):
        body = "overloaded_error"
        outputs.append(attempt)
        if attempt == 1:
            return completed(envelope(subtype="error", is_error=True, result=body))
        return completed(envelope(structured_output={"ok": True}))

    result, calls = capture(
        monkeypatch, claude_config, respond, output=tmp_path / "out.json"
    )
    assert result == {"ok": True}
    assert outputs == [1, 2]
    assert sleeps == [30]
    assert calls[0][1]["input"] == calls[1][1]["input"]


def test_persistent_transient_envelope_failure_is_bounded(claude_config, tmp_path, monkeypatch):
    sleeps = []
    monkeypatch.setattr("paper_collector.agent_common.time.sleep", sleeps.append)

    def respond(*_args):
        return completed(envelope(subtype="error", is_error=True, result="api_error 529"))

    with pytest.raises(AgentUnavailableError, match="api_error"):
        capture(monkeypatch, claude_config, respond, output=tmp_path / "out.json")
    assert sleeps == [30, 90, 180]


def test_non_transient_envelope_failure_is_not_retried(claude_config, tmp_path, monkeypatch):
    sleeps = []
    monkeypatch.setattr("paper_collector.agent_common.time.sleep", sleeps.append)

    def respond(*_args):
        return completed(envelope(subtype="error", is_error=True, result="invalid request"))

    with pytest.raises(AgentError, match="invalid request"):
        capture(monkeypatch, claude_config, respond, output=tmp_path / "out.json")
    assert sleeps == []


def test_nonzero_exit_is_typed(claude_config, tmp_path, monkeypatch):
    with pytest.raises(AgentError, match="exit=2"):
        capture(
            monkeypatch,
            claude_config,
            lambda *_args: completed("", returncode=2, stderr="bad args"),
            output=tmp_path / "out.json",
        )


def test_missing_cli_is_a_clear_error(claude_config, tmp_path, monkeypatch):
    def missing(*_args, **_kwargs):
        raise FileNotFoundError("claude")

    monkeypatch.setattr("paper_collector.agent_common.run_isolated", missing)
    schema = tmp_path / "schema.json"
    schema.write_text("{}", encoding="utf-8")
    with pytest.raises(AgentError, match="找不到 Claude CLI"):
        ClaudeAgent(claude_config)._run("prompt", schema, tmp_path / "out.json")


def test_invalid_envelope_and_invalid_result_are_data_errors(claude_config, tmp_path, monkeypatch):
    with pytest.raises(AgentDataError, match="envelope"):
        capture(
            monkeypatch,
            claude_config,
            lambda *_args: completed("not json"),
            output=tmp_path / "out.json",
        )

    with pytest.raises(AgentDataError, match="结构化结果"):
        capture(
            monkeypatch,
            claude_config,
            lambda *_args: completed(envelope()),
            output=tmp_path / "out.json",
        )


def test_factory_selects_provider(claude_config, config):
    assert isinstance(make_agent(claude_config), ClaudeAgent)
    assert make_agent(config).__class__.__name__ == "CodexAgent"


def test_agentic_run_wraps_landlock_and_keeps_tools(claude_config, tmp_path, monkeypatch):
    config = replace(
        claude_config,
        agent_model="deepseek-v4-pro",
        reading_notes_sandbox_user="",
    )
    workspace = tmp_path / "ws"
    workspace.mkdir()
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return completed(envelope(result="done"))

    monkeypatch.setattr("paper_collector.agent_common.run_isolated", run)
    monkeypatch.setattr("paper_collector.claude_agent.sys.platform", "linux")

    result = ClaudeAgent(config).run_agentic(
        "work", workspace, timeout_seconds=60, model_override="deepseek-v4-pro"
    )

    assert result == "done"
    command, kwargs = calls[0]
    assert command[0] == __import__("sys").executable
    assert "sandbox_exec.py" in command[1]
    assert "--workspace" in command
    assert str(workspace.resolve()) in command
    assert "--json-schema" not in command
    claude_at = command.index(config.claude_bin)
    tail = command[claude_at:]
    assert tail[tail.index("--tools") + 1] == "Read,Write,Edit,Bash"
    assert tail[tail.index("--allowedTools") + 1] == "Read,Write,Edit,Bash"
    assert tail[tail.index("--permission-mode") + 1] == "dontAsk"
    assert tail[tail.index("--permission-prompts") + 1] == "none"
    assert tail[tail.index("--add-dir") + 1] == str(workspace.resolve())
    assert "--restricted" in tail
    assert '"sandbox":{"enabled":false}' in tail[tail.index("--settings") + 1]
    assert kwargs["cwd"] == workspace.resolve()
    assert kwargs["input"] == "work"
    assert kwargs["env"]["HOME"] == str(workspace / ".agent-home")
    assert kwargs["env"]["TMPDIR"] == str(workspace / ".agent-tmp")
    assert kwargs["env"]["PATH"].startswith(
        str(__import__("pathlib").Path(__import__("sys").executable).parent)
    )
    assert (workspace / "agent.envelope.json").is_file()
