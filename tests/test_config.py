from pathlib import Path

import pytest

from paper_collector.config import ConfigError, load_config


def test_load_config_resolves_data_dir(config_file: Path):
    config = load_config(config_file)
    assert config.data_dir == config_file.parent / "data"
    assert config.knowledge_base_dir == config_file.parent / "data" / "knowledge_base"
    assert [item.scope for item in config.interests] == ["niche", "broad"]
    assert config.translation_service == "openailiked"
    assert config.official_enabled is True
    assert "openreview" in config.official_sources
    assert config.official_lookback_years == 2
    assert config.diversity_history_days == 7
    assert config.diversity_penalty == 1.5


def test_config_requires_interest(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text('[profile]\nemail="a@b.com"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="至少配置"):
        load_config(path)


def test_dotenv_is_loaded_without_overwrite(config_file: Path, monkeypatch):
    monkeypatch.delenv("WECOM_WEBHOOK_KEY", raising=False)
    (config_file.parent / ".env").write_text("WECOM_WEBHOOK_KEY=secret-test\n")
    assert load_config(config_file).wecom_webhook_key == "secret-test"


def test_openailiked_translation_settings_are_loaded_from_config(config_file: Path, monkeypatch):
    monkeypatch.setenv("OPENAILIKED_BASE_URL", "https://api.example.test/v1")
    monkeypatch.setenv("OPENAILIKED_API_KEY", "test-api-key")
    monkeypatch.setenv("OPENAILIKED_MODEL", "test-model")
    content = config_file.read_text(encoding="utf-8").replace(
        "[translation]\nenabled = false",
        """[translation]
enabled = true
service = "OpenAI-liked"
OPENAILIKED_BASE_URL = "${OPENAILIKED_BASE_URL}"
OPENAILIKED_API_KEY = "${OPENAILIKED_API_KEY}"
OPENAILIKED_MODEL = "${OPENAILIKED_MODEL}"
""",
    )
    config_file.write_text(content, encoding="utf-8")

    config = load_config(config_file)

    assert config.translation_service == "openailiked"
    assert config.translation_openailiked_base_url == "https://api.example.test/v1"
    assert config.translation_openailiked_api_key == "test-api-key"
    assert config.translation_openailiked_model == "test-model"


def test_unknown_official_source_is_rejected(config_file: Path):
    content = config_file.read_text(encoding="utf-8").replace(
        "per_interest_limit = 5", 'per_interest_limit = 5\nofficial_sources = ["mystery"]'
    )
    config_file.write_text(content, encoding="utf-8")

    with pytest.raises(ConfigError, match="未知数据源"):
        load_config(config_file)


def test_baidupan_cookie_is_loaded_from_config(config_file: Path):
    with config_file.open("a", encoding="utf-8") as handle:
        handle.write("\n[baidupan]\ncookie = 'BDUSS=x; STOKEN=y'\n")

    config = load_config(config_file)

    assert config.baidupcs_cookie == "BDUSS=x; STOKEN=y"


def test_invalid_toml_has_actionable_cookie_hint(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text('[baidupan]\ncookie = "unterminated\n', encoding="utf-8")

    with pytest.raises(ConfigError, match="baidupan.cookie"):
        load_config(path)


def test_baidupan_remote_dir_must_be_a_root_level_folder(config_file: Path):
    with config_file.open("a", encoding="utf-8") as handle:
        handle.write('\n[baidupan]\nremote_dir = "/nested/PaperCollector"\n')

    with pytest.raises(ConfigError, match="根目录下的单层文件夹"):
        load_config(config_file)
