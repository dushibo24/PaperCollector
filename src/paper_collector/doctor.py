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
    if config.official_enabled:
        checks.extend(
            [
                Check(
                    bool(config.official_sources),
                    "会议/期刊官网",
                    f"已启用 {len(config.official_sources)} 个适配器："
                    + ", ".join(config.official_sources),
                ),
                Check(
                    True,
                    "IEEE Xplore API",
                    "已配置" if config.ieee_xplore_api_key else "未配置（可选，运行时自动跳过）",
                ),
                Check(
                    True,
                    "Elsevier API",
                    "已配置" if config.elsevier_api_key else "未配置（可选，运行时自动跳过）",
                ),
            ]
        )
    else:
        checks.append(Check(True, "会议/期刊官网", "已禁用，仅使用聚合源"))
    if config.translation_enabled and config.translation_service == "openailiked":
        configured = all(
            (
                config.translation_openailiked_base_url,
                config.translation_openailiked_api_key,
                config.translation_openailiked_model,
            )
        )
        checks.append(
            Check(
                configured,
                "OpenAI-liked 翻译",
                "Base URL、API Key、模型均已配置"
                if configured
                else "缺少 OPENAILIKED_BASE_URL / OPENAILIKED_API_KEY / OPENAILIKED_MODEL",
            )
        )
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
            detail = publisher.ensure_login()
            checks.append(Check(True, "百度网盘", detail))
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
