from pathlib import Path

import pytest

from paper_collector.config import load_config


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[profile]
email = "test@example.com"

[paths]
data_dir = "data"

[sources]
recent_days = 30
evergreen_years = 5
per_interest_limit = 5

[agent]
shortlist_size = 3
max_deep_reviews = 2
max_search_rounds = 1

[translation]
enabled = false

[notify]
channel = "none"
wecom_enabled = false

[quality]
min_recommendation_score = 7.0
top_venues = ["NeurIPS"]
strong_venues = ["AAAI"]

[[interests]]
name = "RAG"
description = "retrieval augmented generation"
scope = "niche"
keywords = ["retrieval augmented generation", "RAG"]
negative_keywords = ["survey"]
weight = 1.2

[[interests]]
name = "LLM"
description = "large language models"
scope = "broad"
keywords = ["large language model", "reasoning"]
weight = 1.0
""",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def config(config_file: Path):
    return load_config(config_file)


@pytest.fixture(autouse=True)
def no_unmocked_external_http(monkeypatch):
    """Regression tests must not silently fall back to live paper/model APIs."""
    import urllib.parse
    import urllib.request

    original = urllib.request.urlopen

    def local_only(url, *args, **kwargs):
        value = url.full_url if isinstance(url, urllib.request.Request) else url
        if urllib.parse.urlsplit(value).hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise AssertionError(f"External HTTP must be mocked in tests: {value}")
        return original(url, *args, **kwargs)

    monkeypatch.setattr(urllib.request, "urlopen", local_only)
