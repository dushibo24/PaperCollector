from __future__ import annotations

import json
import logging
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from paper_collector.config import AppConfig
from paper_collector.models import Paper, Review
from paper_collector.process_tools import run_isolated
from paper_collector.unicode_tools import utf8_text, utf8_value


class AgentError(RuntimeError):
    pass


class AgentDataError(AgentError):
    """Invalid input/output data; changing papers can resolve it."""


class AgentUnavailableError(AgentError):
    """Temporary provider failure after bounded retries; changing papers won't fix it."""


LOGGER = logging.getLogger(__name__)


class CodexAgent:
    def __init__(self, config: AppConfig):
        self.config = config

    def _run(
        self,
        prompt: str,
        schema: Path,
        output: Path,
        *,
        images: list[Path] | None = None,
        timeout_seconds: int | None = None,
        model_override: str | None = None,
    ) -> dict[str, Any]:
        try:
            prompt = utf8_text(prompt)
        except UnicodeError as exc:
            raise AgentDataError(f"模型输入包含无效 Unicode：{exc}") from exc
        output.parent.mkdir(parents=True, exist_ok=True)
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
        model = model_override or self.config.codex_model
        if model:
            command.extend(["--model", model])
        for image in images or []:
            command.extend(["--image", str(image.resolve())])
        command.append("-")
        timeout = timeout_seconds or self.config.codex_timeout_seconds
        deadline = time.monotonic() + timeout
        delays = (30, 90, 180)
        transient_markers = (
            "high demand",
            "overloaded",
            "temporarily unavailable",
            "server_error",
            "rate_limit_exceeded",
            "rate limit reached",
            "too many requests",
            "connection reset",
            "stream disconnected",
            "502 bad gateway",
            "status code 502",
            "status code 503",
            "status code 504",
        )
        for attempt in range(len(delays) + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AgentUnavailableError(f"Codex 超时（总预算 {timeout}s）")
            # Never accept a previous attempt's output as the current result.
            output.unlink(missing_ok=True)
            try:
                completed = run_isolated(command, input=prompt, timeout=remaining)
            except FileNotFoundError as exc:
                raise AgentError(f"找不到 Codex CLI：{self.config.codex_bin}") from exc
            except UnicodeError as exc:
                raise AgentDataError("Codex 输出不是有效的 UTF-8 文本") from exc
            except subprocess.TimeoutExpired as exc:
                raise AgentUnavailableError(f"Codex 超时（总预算 {timeout}s）") from exc
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
            if not any(marker in detail.casefold() for marker in transient_markers):
                raise AgentError(message)
            if attempt == len(delays) or deadline - time.monotonic() <= delays[attempt]:
                raise AgentUnavailableError(message)
            delay = delays[attempt]
            LOGGER.warning(
                "模型服务暂时不可用，%s 秒后重试（%s/%s），保留当前论文进度",
                delay,
                attempt + 1,
                len(delays),
            )
            time.sleep(delay)
        raise AgentUnavailableError("Codex 暂时不可用")

    def _interests_json(self) -> str:
        payload = [
            {
                "name": item.name,
                "description": item.description,
                "scope": item.scope,
                "keywords": item.keywords,
                "negative_keywords": item.negative_keywords,
                "weight": item.weight,
            }
            for item in self.config.interests
        ]
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def rank(self, papers: list[Paper], run_dir: Path) -> list[dict[str, Any]]:
        template = (self.config.repo_root / "prompts/rank.md").read_text(encoding="utf-8")
        candidate_payload = []
        for paper in papers:
            try:
                value = utf8_value(paper.to_dict())
            except UnicodeError as exc:
                LOGGER.warning("跳过文本编码无效的精排候选 %r：%s", paper.id, exc)
                continue
            value["abstract"] = value["abstract"][:3500]
            candidate_payload.append(value)
        if not candidate_payload:
            return []
        prompt = template.format(
            today=datetime.now(UTC).date().isoformat(),
            shortlist_size=self.config.shortlist_size,
            interests_json=self._interests_json(),
            candidates_json=json.dumps(candidate_payload, ensure_ascii=False, indent=2),
        )
        try:
            prompt = utf8_text(prompt)
            (run_dir / "rank.prompt.txt").write_text(prompt, encoding="utf-8")
        except UnicodeError as exc:
            raise AgentDataError(f"精排输入文本编码无效：{exc}") from exc
        result = self._run(
            prompt,
            self.config.repo_root / "schemas/rank.schema.json",
            run_dir / "rank.result.json",
        )
        known = {value["id"] for value in candidate_payload}
        selected = [item for item in result.get("selected", []) if item.get("paper_id") in known]
        return sorted(selected, key=lambda item: item["rank"])[: self.config.shortlist_size]

    def review(
        self,
        paper: Paper,
        paper_text: str,
        rank_context: dict[str, Any],
        run_dir: Path,
    ) -> Review:
        template = (self.config.repo_root / "prompts/review.md").read_text(encoding="utf-8")
        try:
            prompt = template.format(
                interests_json=self._interests_json(),
                paper_json=json.dumps(utf8_value(paper.to_dict()), ensure_ascii=False, indent=2),
                rank_context_json=json.dumps(
                    utf8_value(rank_context), ensure_ascii=False, indent=2
                ),
                paper_text=utf8_text(paper_text),
            )
            prompt = utf8_text(prompt)
            safe_id = paper.id.replace(":", "_").replace("/", "_")
            (run_dir / f"review-{safe_id}.prompt.txt").write_text(prompt, encoding="utf-8")
        except UnicodeError as exc:
            raise AgentDataError(f"审读输入文本编码无效：{exc}") from exc
        result = self._run(
            prompt,
            self.config.repo_root / "schemas/review.schema.json",
            run_dir / f"review-{safe_id}.result.json",
        )
        if result.get("paper_id") != paper.id:
            raise AgentError(
                f"Codex review paper_id 不匹配：{result.get('paper_id')} != {paper.id}"
            )
        return Review.from_dict(result)
