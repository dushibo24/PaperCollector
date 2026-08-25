from pathlib import Path

import pytest

from paper_collector.config import ConfigError, load_config


def test_load_config_resolves_data_dir(config_file: Path):
    config = load_config(config_file)
    assert config.data_dir == config_file.parent / "data"
    assert [item.scope for item in config.interests] == ["niche", "broad"]


def test_config_requires_interest(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text('[profile]\nemail="a@b.com"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="至少配置"):
        load_config(path)


def test_dotenv_is_loaded_without_overwrite(config_file: Path, monkeypatch):
    monkeypatch.delenv("WECOM_WEBHOOK_KEY", raising=False)
    (config_file.parent / ".env").write_text("WECOM_WEBHOOK_KEY=secret-test\n")
    assert load_config(config_file).wecom_webhook_key == "secret-test"
