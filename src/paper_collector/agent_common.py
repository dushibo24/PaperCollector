from __future__ import annotations

import json
import logging
import subprocess
import time
from contextlib import nullcontext
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


class _TransientFailure(Exception):
    """Internal signal: the same request may succeed after backoff."""


LOGGER = logging.getLogger(__name__)

RETRY_DELAYS = (30, 90, 180)
TRANSIENT_MARKERS = (
    "high demand",
    "overloaded",
    "overloaded_error",
    "temporarily unavailable",
    "server_error",
    "api_error",
    "rate_limit_exceeded",
    "rate_limit_error",
    "rate limit reached",
    "too many requests",
    "connection reset",
    "stream disconnected",
    "internal server error",
    "502 bad gateway",
    "status code 500",
    "status code 502",
    "status code 503",
    "status code 504",
    "status code 529",
)


class BaseAgent:
    provider_name = "Agent"
    encoding: str | None = None

    def __init__(self, config: AppConfig):
        self.config = config

    def _binary(self) -> str:
        raise NotImplementedError

    def _execution_context(self, output: Path, images: list[Path] | None):
        return nullcontext((None, None))

    def _prepare_prompt(self, prompt: str, images: list[Path] | None) -> str:
        return prompt

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
        raise NotImplementedError

    def _extract_result(self, completed: subprocess.CompletedProcess, output: Path) -> dict[str, Any]:
        raise NotImplementedError

    def _clear_outputs(self, output: Path) -> None:
        output.unlink(missing_ok=True)

    def _is_transient(self, detail: str) -> bool:
        folded = detail.casefold()
        return any(marker in folded for marker in TRANSIENT_MARKERS)

    def _write_output(self, output: Path, result: dict[str, Any]) -> None:
        output.write_text(
            json.dumps(utf8_value(result), ensure_ascii=False), encoding="utf-8"
        )

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
        timeout = timeout_seconds or self.config.agent_timeout_seconds
        deadline = time.monotonic() + timeout
        prepared = self._prepare_prompt(prompt, images)
        model = model_override or self.config.agent_model
        with self._execution_context(output, images) as (env, cwd):
            command = self._command(
                prepared, schema, output, images=images, model=model, cwd=cwd
            )
            for attempt in range(len(RETRY_DELAYS) + 1):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AgentUnavailableError(
                        f"{self.provider_name} 超时（总预算 {timeout}s）"
                    )
                self._clear_outputs(output)
                try:
                    completed = run_isolated(
                        command,
                        input=prepared,
                        timeout=remaining,
                        env=env,
                        cwd=cwd,
                        encoding=self.encoding,
                    )
                except FileNotFoundError as exc:
                    raise AgentError(
                        f"找不到 {self.provider_name} CLI：{self._binary()}"
                    ) from exc
                except UnicodeError as exc:
                    raise AgentDataError(
                        f"{self.provider_name} 输出不是有效的 UTF-8 文本"
                    ) from exc
                except subprocess.TimeoutExpired as exc:
                    raise AgentUnavailableError(
                        f"{self.provider_name} 超时（总预算 {timeout}s）"
                    ) from exc
                try:
                    return self._extract_result(completed, output)
                except _TransientFailure as exc:
                    if (
                        attempt == len(RETRY_DELAYS)
                        or deadline - time.monotonic() <= RETRY_DELAYS[attempt]
                    ):
                        raise AgentUnavailableError(str(exc)) from exc
                    delay = RETRY_DELAYS[attempt]
                    LOGGER.warning(
                        "模型服务暂时不可用，%s 秒后重试（%s/%s），保留当前论文进度",
                        delay,
                        attempt + 1,
                        len(RETRY_DELAYS),
                    )
                    time.sleep(delay)
        raise AgentUnavailableError(f"{self.provider_name} 暂时不可用")

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
                f"{self.provider_name} review paper_id 不匹配："
                f"{result.get('paper_id')} != {paper.id}"
            )
        return Review.from_dict(result)


def make_agent(config: AppConfig) -> BaseAgent:
    if config.agent_provider == "claude":
        from paper_collector.claude_agent import ClaudeAgent

        return ClaudeAgent(config)
    from paper_collector.codex_agent import CodexAgent

    return CodexAgent(config)
