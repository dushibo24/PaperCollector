from types import SimpleNamespace

from paper_collector import pdf_tools
from paper_collector.pdf_tools import translate_dual


def test_font_subsetting_failure_retries_with_cached_translation_compatibility_mode(
    config, tmp_path, monkeypatch
):
    config.translation_enabled = True
    config.translation_service = "google"
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-source")
    output = tmp_path / "translated"
    commands = []
    monkeypatch.setattr(pdf_tools.shutil, "which", lambda _binary: "/usr/bin/pdf2zh")

    def run(command, **kwargs):
        commands.append(list(command))
        assert kwargs["timeout"] <= config.translation_timeout_seconds
        if len(commands) == 1:
            return SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="doc_zh.subset_fonts(fallback=True)\nValueError: bad 'value'",
            )
        assert "--skip-subset-fonts" in command
        (output / "source-dual.pdf").write_bytes(b"%PDF-dual")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(pdf_tools.subprocess, "run", run)
    assert translate_dual(source, output, config).name == "source-dual.pdf"
    assert len(commands) == 2
    assert "--ignore-cache" not in commands[1]


def test_explicit_font_compatibility_mode_is_used_on_first_attempt(config, tmp_path, monkeypatch):
    config.translation_enabled = True
    config.translation_service = "google"
    config.translation_skip_subset_fonts = True
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-source")
    output = tmp_path / "translated"
    monkeypatch.setattr(pdf_tools.shutil, "which", lambda _binary: "/usr/bin/pdf2zh")

    def run(command, **_kwargs):
        assert command.count("--skip-subset-fonts") == 1
        (output / "source-dual.pdf").write_bytes(b"%PDF-dual")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(pdf_tools.subprocess, "run", run)
    assert translate_dual(source, output, config).exists()


def test_openailiked_settings_are_passed_only_through_subprocess_environment(
    config, tmp_path, monkeypatch
):
    config.translation_enabled = True
    config.translation_service = "openailiked"
    config.translation_openailiked_base_url = "https://api.example.test/v1"
    config.translation_openailiked_api_key = "test-api-key"
    config.translation_openailiked_model = "test-model"
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-test")
    output_dir = tmp_path / "translated"

    monkeypatch.setattr(pdf_tools.shutil, "which", lambda _binary: "/usr/bin/pdf2zh")

    def fake_run(command, **kwargs):
        environment = kwargs["env"]
        assert environment["OPENAILIKED_BASE_URL"] == "https://api.example.test/v1"
        assert environment["OPENAILIKED_API_KEY"] == "test-api-key"
        assert environment["OPENAILIKED_MODEL"] == "test-model"
        assert "test-api-key" not in command
        (output_dir / "paper-dual.pdf").write_bytes(b"%PDF-dual")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(pdf_tools.subprocess, "run", fake_run)

    assert translate_dual(source, output_dir, config) == output_dir / "paper-dual.pdf"
