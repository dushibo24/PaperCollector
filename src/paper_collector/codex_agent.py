from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from paper_collector.config import AppConfig
from paper_collector.models import Paper, Review


class AgentError(RuntimeError):
    pass


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
    ) -> dict[str, Any]:
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
        if self.config.codex_model:
            command.extend(["--model", self.config.codex_model])
        for image in images or []:
            command.extend(["--image", str(image.resolve())])
        command.append("-")
        timeout = timeout_seconds or self.config.codex_timeout_seconds
        try:
            completed = subprocess.run(
                command,
                input=prompt,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise AgentError(f"找不到 Codex CLI：{self.config.codex_bin}") from exc
        except subprocess.TimeoutExpired as exc:
            raise AgentError(f"Codex 超时（{timeout}s）") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout)[-4000:]
            raise AgentError(f"Codex 执行失败（exit={completed.returncode}）：\n{detail}")
        try:
            return json.loads(output.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AgentError(f"Codex 没有生成合法 JSON：{output}") from exc

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
            value = paper.to_dict()
            value["abstract"] = paper.abstract[:3500]
            candidate_payload.append(value)
        prompt = template.format(
            shortlist_size=self.config.shortlist_size,
            interests_json=self._interests_json(),
            candidates_json=json.dumps(candidate_payload, ensure_ascii=False, indent=2),
        )
        (run_dir / "rank.prompt.txt").write_text(prompt, encoding="utf-8")
        result = self._run(
            prompt,
            self.config.repo_root / "schemas/rank.schema.json",
            run_dir / "rank.result.json",
        )
        known = {paper.id for paper in papers}
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
        prompt = template.format(
            interests_json=self._interests_json(),
            paper_json=json.dumps(paper.to_dict(), ensure_ascii=False, indent=2),
            rank_context_json=json.dumps(rank_context, ensure_ascii=False, indent=2),
            paper_text=paper_text,
        )
        safe_id = paper.id.replace(":", "_").replace("/", "_")
        (run_dir / f"review-{safe_id}.prompt.txt").write_text(prompt, encoding="utf-8")
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
