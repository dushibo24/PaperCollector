from types import SimpleNamespace

from paper_collector import pdf_tools
from paper_collector.pdf_tools import translate_dual


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
