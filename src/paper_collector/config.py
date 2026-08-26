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
    knowledge_base_dir: Path
    email: str
    interests: list[Interest]
    recent_days: int
    evergreen_years: int
    per_interest_limit: int
    shortlist_size: int
    max_deep_reviews: int
    diversity_history_days: int
    diversity_penalty: float
    min_recommendation_score: float
    top_venues: set[str]
    strong_venues: set[str]
    codex_bin: str
    codex_model: str | None
    codex_timeout_seconds: int
    pdf2zh_bin: str
    translation_enabled: bool
    translation_service: str
    translation_openailiked_base_url: str | None
    translation_openailiked_api_key: str | None
    translation_openailiked_model: str | None
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
    baidupcs_cookie: str | None
    semantic_scholar_api_key: str | None
    official_enabled: bool
    official_lookback_years: int
    official_per_source_limit: int
    official_sources: list[str]
    ieee_xplore_api_key: str | None
    elsevier_api_key: str | None


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
    try:
        with config_path.open("rb") as handle:
            raw = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(
            f"配置文件 TOML 格式错误：{exc}。baidupan.cookie 必须是单行字符串；"
            "Cookie 含双引号时可改用 TOML 单引号字符串。"
        ) from exc
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
    knowledge_base_dir = (
        repo_root / paths.get("knowledge_base_dir", "data/knowledge_base")
    ).resolve()
    webhook_key = _expand_env(notify.get("wecom_webhook_key", "${WECOM_WEBHOOK_KEY}"))
    serverchan_sendkey = _expand_env(notify.get("serverchan_sendkey", "${SERVERCHAN_SENDKEY}"))
    notify_channel = str(notify.get("channel", "serverchan"))
    if notify_channel not in {"serverchan", "wecom", "none"}:
        raise ConfigError("notify.channel 只能是 serverchan、wecom 或 none")
    baidupan = raw.get("baidupan", {})
    baidupcs_remote_dir = "/" + str(baidupan.get("remote_dir", "/PaperCollector")).strip("/")
    if not baidupcs_remote_dir.strip("/") or "/" in baidupcs_remote_dir.strip("/"):
        raise ConfigError("baidupan.remote_dir 必须是网盘根目录下的单层文件夹，如 /PaperCollector")
    share_period_days = int(baidupan.get("share_period_days", 365))
    share_password = baidupan.get("share_password") or None
    baidupcs_cookie = _expand_env(baidupan.get("cookie", "${BAIDUPAN_COOKIE}"))
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
    official_sources = [
        str(item).casefold()
        for item in sources.get(
            "official_sources",
            [
                "openreview",
                "pmlr",
                "acl",
                "cvf",
                "neurips",
                "usenix",
                "ndss",
                "jmlr",
                "pvldb",
                "ieee",
                "elsevier",
            ],
        )
    ]
    known_official_sources = {
        "openreview",
        "pmlr",
        "acl",
        "cvf",
        "neurips",
        "usenix",
        "ndss",
        "jmlr",
        "pvldb",
        "ieee",
        "elsevier",
    }
    unknown_official_sources = sorted(set(official_sources) - known_official_sources)
    if unknown_official_sources:
        raise ConfigError(
            "sources.official_sources 含未知数据源：" + ", ".join(unknown_official_sources)
        )
    official_lookback_years = int(sources.get("official_lookback_years", 2))
    official_per_source_limit = int(sources.get("official_per_source_limit", 20))
    if official_lookback_years < 1:
        raise ConfigError("sources.official_lookback_years 必须至少为 1")
    if official_per_source_limit < 1:
        raise ConfigError("sources.official_per_source_limit 必须至少为 1")
    diversity_history_days = int(agent.get("diversity_history_days", 7))
    diversity_penalty = float(agent.get("diversity_penalty", 1.5))
    if diversity_history_days < 0:
        raise ConfigError("agent.diversity_history_days 不能小于 0")
    if diversity_penalty < 0:
        raise ConfigError("agent.diversity_penalty 不能小于 0")
    codex_model = agent.get("model") or None
    translation_service_raw = str(translation.get("service", "openailiked"))
    translation_service = (
        "openailiked"
        if translation_service_raw.casefold().replace("-", "") == "openailiked"
        else translation_service_raw
    )
    openailiked_base_url = _expand_env(
        translation.get("OPENAILIKED_BASE_URL", "${OPENAILIKED_BASE_URL}")
    )
    openailiked_api_key = _expand_env(
        translation.get("OPENAILIKED_API_KEY", "${OPENAILIKED_API_KEY}")
    )
    openailiked_model = _expand_env(translation.get("OPENAILIKED_MODEL", "${OPENAILIKED_MODEL}"))

    return AppConfig(
        path=config_path,
        repo_root=repo_root,
        data_dir=data_dir,
        knowledge_base_dir=knowledge_base_dir,
        email=_need(profile, "email", "[profile]"),
        interests=interests,
        recent_days=int(sources.get("recent_days", 45)),
        evergreen_years=int(sources.get("evergreen_years", 8)),
        per_interest_limit=int(sources.get("per_interest_limit", 20)),
        shortlist_size=int(agent.get("shortlist_size", 5)),
        max_deep_reviews=int(agent.get("max_deep_reviews", 3)),
        diversity_history_days=diversity_history_days,
        diversity_penalty=diversity_penalty,
        min_recommendation_score=float(quality.get("min_recommendation_score", 7.0)),
        top_venues={str(v).casefold() for v in quality.get("top_venues", [])},
        strong_venues={str(v).casefold() for v in quality.get("strong_venues", [])},
        codex_bin=str(agent.get("codex_bin", "codex")),
        codex_model=codex_model,
        codex_timeout_seconds=int(agent.get("timeout_seconds", 900)),
        pdf2zh_bin=str(translation.get("pdf2zh_bin", "pdf2zh")),
        translation_enabled=bool(translation.get("enabled", True)),
        translation_service=translation_service,
        translation_openailiked_base_url=openailiked_base_url or None,
        translation_openailiked_api_key=openailiked_api_key or None,
        translation_openailiked_model=openailiked_model or None,
        translation_threads=int(translation.get("threads", 4)),
        translation_timeout_seconds=int(translation.get("timeout_seconds", 3600)),
        notify_channel=notify_channel,
        serverchan_sendkey=serverchan_sendkey,
        wecom_webhook_key=webhook_key,
        wecom_enabled=bool(notify.get("wecom_enabled", True)),
        baidupcs_bin=str(baidupan.get("bin", "BaiduPCS-Go")),
        baidupcs_remote_dir=baidupcs_remote_dir,
        baidupcs_share_period_days=share_period_days,
        baidupcs_share_password=share_password,
        baidupcs_timeout_seconds=int(baidupan.get("timeout_seconds", 1800)),
        baidupcs_cookie=baidupcs_cookie.strip() if baidupcs_cookie else None,
        semantic_scholar_api_key=scholar_key,
        official_enabled=bool(sources.get("official_enabled", True)),
        official_lookback_years=official_lookback_years,
        official_per_source_limit=official_per_source_limit,
        official_sources=official_sources,
        ieee_xplore_api_key=_expand_env(
            sources.get("ieee_xplore_api_key", "${IEEE_XPLORE_API_KEY}")
        ),
        elsevier_api_key=_expand_env(sources.get("elsevier_api_key", "${ELSEVIER_API_KEY}")),
    )
