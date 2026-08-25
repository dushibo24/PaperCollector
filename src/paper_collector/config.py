from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    pass


@dataclass(slots=True)
class Interest:
    name: str
    description: str
    scope: str = "broad"
    keywords: list[str] = field(default_factory=list)
    negative_keywords: list[str] = field(default_factory=list)
    weight: float = 1.0


@dataclass(slots=True)
class AppConfig:
    path: Path
    repo_root: Path
    data_dir: Path
    email: str
    interests: list[Interest]
    recent_days: int
    evergreen_years: int
    per_interest_limit: int
    shortlist_size: int
    max_deep_reviews: int
    min_recommendation_score: float
    top_venues: set[str]
    strong_venues: set[str]
    codex_bin: str
    codex_model: str | None
    codex_timeout_seconds: int
    pdf2zh_bin: str
    translation_enabled: bool
    translation_service: str
    translation_threads: int
    translation_timeout_seconds: int
    notify_channel: str
    serverchan_sendkey: str | None
    wecom_webhook_key: str | None
    wecom_enabled: bool
    baidupcs_bin: str
    baidupcs_remote_dir: str
    baidupcs_share_period_days: int
    baidupcs_share_password: str | None
    baidupcs_timeout_seconds: int
    semantic_scholar_api_key: str | None


def _need(table: dict[str, Any], key: str, context: str) -> Any:
    if key not in table:
        raise ConfigError(f"{context} 缺少必填项 {key!r}")
    return table[key]


def _expand_env(value: str | None) -> str | None:
    if value is None:
        return None
    if value.startswith("${") and value.endswith("}"):
        return os.environ.get(value[2:-1])
    return os.path.expandvars(value)


def _load_dotenv(path: Path) -> None:
    """Load a tiny, dependency-free .env subset without overriding the process environment."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key.replace("_", "").isalnum():
            os.environ.setdefault(key, value)


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path).expanduser().resolve()
    if not config_path.exists():
        raise ConfigError(f"配置文件不存在：{config_path}（可从 config.example.toml 复制）")
    with config_path.open("rb") as handle:
        raw = tomllib.load(handle)
    _load_dotenv(config_path.parent / ".env")

    profile = raw.get("profile", {})
    sources = raw.get("sources", {})
    quality = raw.get("quality", {})
    agent = raw.get("agent", {})
    translation = raw.get("translation", {})
    notify = raw.get("notify", {})
    paths = raw.get("paths", {})
    interests_raw = raw.get("interests", [])
    if not interests_raw:
        raise ConfigError("至少配置一个 [[interests]]")

    interests = [
        Interest(
            name=_need(item, "name", "[[interests]]"),
            description=_need(item, "description", f"interest {item.get('name', '')}"),
            scope=item.get("scope", "broad"),
            keywords=list(item.get("keywords", [])),
            negative_keywords=list(item.get("negative_keywords", [])),
            weight=float(item.get("weight", 1.0)),
        )
        for item in interests_raw
    ]
    invalid_scopes = [i.name for i in interests if i.scope not in {"niche", "broad"}]
    if invalid_scopes:
        raise ConfigError(f"scope 只能是 niche 或 broad：{', '.join(invalid_scopes)}")

    repo_root = config_path.parent
    data_dir = (repo_root / paths.get("data_dir", "data")).resolve()
    webhook_key = _expand_env(notify.get("wecom_webhook_key", "${WECOM_WEBHOOK_KEY}"))
    serverchan_sendkey = _expand_env(notify.get("serverchan_sendkey", "${SERVERCHAN_SENDKEY}"))
    notify_channel = str(notify.get("channel", "serverchan"))
    if notify_channel not in {"serverchan", "wecom", "none"}:
        raise ConfigError("notify.channel 只能是 serverchan、wecom 或 none")
    baidupan = raw.get("baidupan", {})
    share_period_days = int(baidupan.get("share_period_days", 365))
    share_password = baidupan.get("share_password") or None
    if share_period_days < 0:
        raise ConfigError("baidupan.share_period_days 不能小于 0")
    if share_password and (
        len(share_password) != 4
        or not str(share_password).isascii()
        or not str(share_password).isalnum()
    ):
        raise ConfigError("baidupan.share_password 必须留空或填写四位 ASCII 字母/数字")
    scholar_key = _expand_env(
        sources.get("semantic_scholar_api_key", "${SEMANTIC_SCHOLAR_API_KEY}")
    )
    codex_model = agent.get("model") or None

    return AppConfig(
        path=config_path,
        repo_root=repo_root,
        data_dir=data_dir,
        email=_need(profile, "email", "[profile]"),
        interests=interests,
        recent_days=int(sources.get("recent_days", 45)),
        evergreen_years=int(sources.get("evergreen_years", 8)),
        per_interest_limit=int(sources.get("per_interest_limit", 20)),
        shortlist_size=int(agent.get("shortlist_size", 5)),
        max_deep_reviews=int(agent.get("max_deep_reviews", 3)),
        min_recommendation_score=float(quality.get("min_recommendation_score", 7.0)),
        top_venues={str(v).casefold() for v in quality.get("top_venues", [])},
        strong_venues={str(v).casefold() for v in quality.get("strong_venues", [])},
        codex_bin=str(agent.get("codex_bin", "codex")),
        codex_model=codex_model,
        codex_timeout_seconds=int(agent.get("timeout_seconds", 900)),
        pdf2zh_bin=str(translation.get("pdf2zh_bin", "pdf2zh")),
        translation_enabled=bool(translation.get("enabled", True)),
        translation_service=str(translation.get("service", "google")),
        translation_threads=int(translation.get("threads", 4)),
        translation_timeout_seconds=int(translation.get("timeout_seconds", 3600)),
        notify_channel=notify_channel,
        serverchan_sendkey=serverchan_sendkey,
        wecom_webhook_key=webhook_key,
        wecom_enabled=bool(notify.get("wecom_enabled", True)),
        baidupcs_bin=str(baidupan.get("bin", "BaiduPCS-Go")),
        baidupcs_remote_dir=str(baidupan.get("remote_dir", "/PaperCollector")),
        baidupcs_share_period_days=share_period_days,
        baidupcs_share_password=share_password,
        baidupcs_timeout_seconds=int(baidupan.get("timeout_seconds", 1800)),
        semantic_scholar_api_key=scholar_key,
    )
