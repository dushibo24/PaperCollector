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
    assert config.openreview_username is None
    assert config.openreview_password is None
    assert config.openreview_access_token is None
    assert config.diversity_history_days == 7
    assert config.diversity_penalty == 1.5
    assert config.reading_notes_enabled is False
    assert config.reading_notes_min_chars == 30000
    assert config.reading_notes_dir == config.data_dir / "public_notes"


def test_reading_note_settings_and_public_url_environment(config_file: Path, monkeypatch):
    monkeypatch.setenv("READING_NOTES_BASE_URL", "http://198.51.100.12:8765/reading/")
    with config_file.open("a") as handle:
        handle.write('\n[reading_notes]\nenabled=true\nhost="0.0.0.0"\nport=8765\n')
    config = load_config(config_file)
    assert config.reading_notes_enabled is True
    assert config.reading_notes_base_url == "http://198.51.100.12:8765/reading"
    assert config.reading_notes_host == "0.0.0.0"


def test_enabled_reading_notes_require_a_public_url(config_file: Path, monkeypatch):
    monkeypatch.delenv("READING_NOTES_BASE_URL", raising=False)
    with config_file.open("a") as handle:
        handle.write('\n[reading_notes]\nenabled=true\n')
    with pytest.raises(ConfigError, match="public_base_url"):
        load_config(config_file)


def test_vps_publication_is_configured_on_a100_without_changing_local_paths(config_file):
    settings = (Path(__file__).parents[1] / "deploy" / "reading-notes-vps.example.toml").read_text()
    with config_file.open("a") as handle:
        handle.write("\n" + settings)
    config = load_config(config_file)
    assert config.reading_notes_publish_ssh_host == "ubuntu@82.156.88.52"
    assert config.reading_notes_publish_ssh_directory == "/home/ubuntu/paper-collector-notes/public"
    assert config.reading_notes_publish_ssh_port == 22
    assert config.reading_notes_base_url == "https://82.156.88.52/paper-notes"
    assert config.data_dir == config_file.parent / "data"
    assert config.reading_notes_host == "127.0.0.1"


@pytest.mark.parametrize("settings", [
    'publish_ssh_host="root@82.156.88.52"',
    'publish_ssh_directory="/root/public"',
    'publish_ssh_host="-oProxyCommand=bad"\npublish_ssh_directory="/root/public"',
    'publish_ssh_host="root@host;whoami"\npublish_ssh_directory="/root/public"',
    'publish_ssh_host="root@host"\npublish_ssh_directory="relative"',
    'publish_ssh_host="root@host"\npublish_ssh_directory="/root/../etc"',
    'publish_ssh_host="root@host"\npublish_ssh_directory="///"',
    'publish_ssh_port=65536',
    'publish_timeout_seconds=0',
])
def test_invalid_vps_publication_settings_are_rejected(config_file, settings):
    with config_file.open("a") as handle:
        handle.write('\n[reading_notes]\n' + settings + '\n')
    with pytest.raises(ConfigError, match="publish_"):
        load_config(config_file)


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "javascript:alert(1)", "http://user:secret@example.test",
    "http://example.test/?secret=x", "http://example.test/#fragment", "http://example.test/../private",
    "http://example.test/%2e%2e/private", "http://example.test:99999", "http://example.test/bad path",
])
def test_invalid_reading_note_base_urls_are_rejected(config_file: Path, url):
    with config_file.open("a") as handle:
        handle.write(f'\n[reading_notes]\nenabled=true\npublic_base_url="{url}"\n')
    with pytest.raises(ConfigError, match="public_base_url"):
        load_config(config_file)


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


def test_openreview_credentials_are_loaded_from_environment(config_file: Path, monkeypatch):
    monkeypatch.setenv("OPENREVIEW_USERNAME", "researcher@example.com")
    monkeypatch.setenv("OPENREVIEW_PASSWORD", "test-password")
    monkeypatch.setenv("OPENREVIEW_ACCESS_TOKEN", "test-token")

    config = load_config(config_file)

    assert config.openreview_username == "researcher@example.com"
    assert config.openreview_password == "test-password"
    assert config.openreview_access_token == "test-token"


def test_openreview_username_and_password_must_be_paired(config_file: Path, monkeypatch):
    monkeypatch.setenv("OPENREVIEW_USERNAME", "researcher@example.com")
    monkeypatch.delenv("OPENREVIEW_PASSWORD", raising=False)

    with pytest.raises(ConfigError, match="必须成对配置"):
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
