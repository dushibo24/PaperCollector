from __future__ import annotations

import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass

from paper_collector.config import AppConfig
from paper_collector.publishers import BaiduPCSGoPublisher, PublishError


@dataclass(slots=True)
class Check:
    ok: bool
    name: str
    detail: str


def _command_check(name: str, executable: str, args: list[str], needle: str | None = None) -> Check:
    resolved = shutil.which(executable)
    if not resolved:
        return Check(False, name, f"找不到 {executable}")
    try:
        result = subprocess.run(
            [resolved, *args], text=True, capture_output=True, timeout=15, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Check(False, name, str(exc))
    output = (result.stdout + result.stderr).strip()
    ok = result.returncode == 0 and (needle is None or needle in output)
    detail = (
        str(resolved) if ok else (output.splitlines()[0] if output else f"exit={result.returncode}")
    )
    return Check(ok, name, detail)


def _network_check(name: str, url: str) -> Check:
    try:
        request = urllib.request.Request(
            url, method="HEAD", headers={"User-Agent": "PaperCollector/0.1"}
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return Check(response.status < 400, name, f"HTTP {response.status}")
    except (urllib.error.URLError, TimeoutError) as exc:
        return Check(False, name, str(exc))


def run_checks(config: AppConfig) -> list[Check]:
    checks = [
        _command_check("Codex CLI", config.codex_bin, ["exec", "--help"], "--output-schema"),
        _command_check("pdf2zh", config.pdf2zh_bin, ["--help"], "--lang-out"),
        _network_check("OpenAlex", "https://api.openalex.org/works?per-page=1"),
        _network_check(
            "arXiv",
            "https://export.arxiv.org/api/query?search_query=all%3Acomputer&start=0&max_results=1",
        ),
        Check(
            "@" in config.email and config.email != "you@example.com", "OpenAlex 邮箱", config.email
        ),
        Check(bool(config.interests), "兴趣配置", f"{len(config.interests)} 个方向"),
    ]
    if config.notify_channel == "serverchan":
        checks.append(
            Check(
                bool(config.serverchan_sendkey),
                "Server酱",
                "已配置 SendKey" if config.serverchan_sendkey else "缺少 SERVERCHAN_SENDKEY",
            )
        )
        try:
            publisher = BaiduPCSGoPublisher(config)
            logged_in, detail = publisher.check_login()
            checks.append(Check(logged_in, "百度网盘", detail))
        except PublishError as exc:
            checks.append(Check(False, "百度网盘", str(exc)))
    elif config.notify_channel == "wecom":
        checks.append(
            Check(
                bool(config.wecom_webhook_key) and config.wecom_enabled,
                "企业微信",
                "已配置 key" if config.wecom_webhook_key else "缺少 WECOM_WEBHOOK_KEY",
            )
        )
    else:
        checks.append(Check(True, "通知通道", "none（仅生成本地产物）"))
    if config.interests and all(item.name.startswith("示例") for item in config.interests):
        checks.append(Check(False, "兴趣个性化", "仍是示例方向，请编辑 config.toml"))
    return checks
