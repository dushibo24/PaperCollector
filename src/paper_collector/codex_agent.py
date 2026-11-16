from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from paper_collector.agent_common import (
    AgentDataError,
    AgentError,
    BaseAgent,
    _TransientFailure,
)
from paper_collector.unicode_tools import utf8_value


class CodexAgent(BaseAgent):
    provider_name = "Codex"

    def _binary(self) -> str:
        return self.config.codex_bin

    def _command(
        self,
        prompt: str,
        schema: Path,
        output: Path,
        *,
        images: list[Path] | None,
        model: str | None,
        cwd: Path | None,
    ) -> list[str]:
        command = [
            self.config.codex_bin,
            "exec",
            "--ephemeral",
            "--color",
            "never",
            "--sandbox",
            "read-only",
            "--config",
            "shell_environment_policy.inherit=none",
            "--cd",
            str(output.parent),
            "--skip-git-repo-check",
            "--output-schema",
            str(schema),
            "--output-last-message",
            str(output),
        ]
        if model:
            command.extend(["--model", model])
        for image in images or []:
            command.extend(["--image", str(image.resolve())])
        command.append("-")
        return command

    def _extract_result(
        self, completed: subprocess.CompletedProcess, output: Path
    ) -> dict[str, Any]:
        if completed.returncode == 0:
            try:
                return utf8_value(json.loads(output.read_text(encoding="utf-8")))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise AgentDataError(f"Codex 没有生成合法 JSON：{output}") from exc
        raw = completed.stderr or completed.stdout or ""
        error_lines = [
            line for line in raw.splitlines() if line.startswith(("ERROR:", "Error:"))
        ]
        detail = ("\n".join(error_lines) or raw)[-1600:]
        message = f"Codex 执行失败（exit={completed.returncode}）：\n{detail}"
        if self._is_transient(detail):
            raise _TransientFailure(message)
        raise AgentError(message)
