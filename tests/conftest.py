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
