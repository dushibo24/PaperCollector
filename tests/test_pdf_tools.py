from pathlib import Path
from types import SimpleNamespace

from pypdf import PdfReader, PdfWriter

from paper_collector import pdf_tools
from paper_collector.pdf_tools import translate_dual


def make_pdf(path, pages=1):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=612, height=792)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        writer.write(handle)
    return path


def translated_output(command):
    output = Path(command[command.index("--output") + 1]) / "source-dual.pdf"
    return make_pdf(output, len(PdfReader(command[1]).pages) * 2)


def test_font_subsetting_failure_retries_with_cached_translation_compatibility_mode(
    config, tmp_path, monkeypatch
):
    config.translation_enabled = True
    config.translation_service = "google"
    source = tmp_path / "source.pdf"
    make_pdf(source)
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
        translated_output(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(pdf_tools, "_run_translation_process", run)
    assert translate_dual(source, output, config).name == "source-dual.pdf"
    assert len(commands) == 2
    assert "--ignore-cache" not in commands[1]


def test_explicit_font_compatibility_mode_is_used_on_first_attempt(config, tmp_path, monkeypatch):
    config.translation_enabled = True
    config.translation_service = "google"
    config.translation_skip_subset_fonts = True
    source = tmp_path / "source.pdf"
    make_pdf(source)
    output = tmp_path / "translated"
    monkeypatch.setattr(pdf_tools.shutil, "which", lambda _binary: "/usr/bin/pdf2zh")

    def run(command, **_kwargs):
        assert command.count("--skip-subset-fonts") == 1
        translated_output(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(pdf_tools, "_run_translation_process", run)
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
    make_pdf(source)
    output_dir = tmp_path / "translated"

    monkeypatch.setattr(pdf_tools.shutil, "which", lambda _binary: "/usr/bin/pdf2zh")

    def fake_run(command, **kwargs):
        environment = kwargs["env"]
        assert environment["OPENAILIKED_BASE_URL"] == "https://api.example.test/v1"
        assert environment["OPENAILIKED_API_KEY"] == "test-api-key"
        assert environment["OPENAILIKED_MODEL"] == "test-model"
        assert "test-api-key" not in command
        translated_output(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(pdf_tools, "_run_translation_process", fake_run)

    assert translate_dual(source, output_dir, config) == output_dir / "paper-dual.pdf"


def test_chunk_timeout_keeps_completed_pages_and_resume_produces_full_dual(
    config, tmp_path, monkeypatch
):
    import subprocess

    import pytest

    config.translation_enabled = True
    config.translation_service = "google"
    config.translation_chunk_pages = 2
    source = make_pdf(tmp_path / "paper.pdf", 7)
    output = tmp_path / "translated"
    calls = []
    fail = [True]
    monkeypatch.setattr(pdf_tools.shutil, "which", lambda _b: "/usr/bin/pdf2zh")

    def run(command, **_kwargs):
        segment = Path(command[1]).parent.name
        calls.append(segment)
        if segment == "pages-0003-0004" and fail[0]:
            fail[0] = False
            raise subprocess.TimeoutExpired(command, 600, stderr="stalled at page 4")
        translated_output(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(pdf_tools, "_run_translation_process", run)
    with pytest.raises(pdf_tools.PDFError, match="翻译超时"):
        translate_dual(source, output, config)
    assert not (output / "paper-dual.pdf").exists()
    assert len(list(output.glob("segments/*/*/complete.json"))) == 1
    result = translate_dual(source, output, config)
    assert len(PdfReader(result).pages) == 14
    assert calls.count("pages-0001-0002") == 1
    assert calls.count("pages-0003-0004") == 2
    previous = list(calls)
    assert translate_dual(source, output, config) == result
    assert calls == previous


def test_translation_identity_prevents_reusing_another_pdf_or_model(config, tmp_path, monkeypatch):
    config.translation_enabled = True
    config.translation_service = "google"
    source = make_pdf(tmp_path / "paper.pdf", 2)
    output = tmp_path / "translated"
    calls = []
    monkeypatch.setattr(pdf_tools.shutil, "which", lambda _b: "/usr/bin/pdf2zh")

    def run(command, **_kwargs):
        calls.append(command)
        translated_output(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(pdf_tools, "_run_translation_process", run)
    translate_dual(source, output, config)
    config.translation_openailiked_model = "different-model"
    translate_dual(source, output, config)
    assert len(calls) == 2
    make_pdf(source, 3)
    result = translate_dual(source, output, config)
    assert len(PdfReader(result).pages) == 6
    assert len(calls) == 3


def test_corrupt_completed_segment_is_regenerated(config, tmp_path, monkeypatch):
    config.translation_enabled = True
    config.translation_service = "google"
    source = make_pdf(tmp_path / "paper.pdf", 2)
    output = tmp_path / "translated"
    calls = []
    monkeypatch.setattr(pdf_tools.shutil, "which", lambda _b: "/usr/bin/pdf2zh")

    def run(command, **_kwargs):
        calls.append(1)
        translated_output(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(pdf_tools, "_run_translation_process", run)
    result = translate_dual(source, output, config)
    result.unlink()
    segment = next(output.glob("segments/*/*/source-dual.pdf"))
    segment.write_bytes(b"corrupted")
    assert len(PdfReader(translate_dual(source, output, config)).pages) == 4
    assert len(calls) == 2


def test_incomplete_dual_is_never_merged_or_marked_complete(config, tmp_path, monkeypatch):
    import pytest

    config.translation_enabled = True
    config.translation_service = "google"
    source = make_pdf(tmp_path / "paper.pdf", 3)
    output = tmp_path / "translated"
    monkeypatch.setattr(pdf_tools.shutil, "which", lambda _b: "/usr/bin/pdf2zh")

    def run(command, **_kwargs):
        make_pdf(Path(command[command.index("--output") + 1]) / "source-dual.pdf", 2)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(pdf_tools, "_run_translation_process", run)
    with pytest.raises(pdf_tools.PDFError, match="页数不完整"):
        translate_dual(source, output, config)
    assert not (output / "paper-dual.pdf").exists()
    assert not list(output.glob("segments/*/*/complete.json"))


def test_paper_budget_ends_attempt_without_discarding_complete_segments(
    config, tmp_path, monkeypatch
):
    import pytest

    config.translation_enabled = True
    config.translation_service = "google"
    config.translation_chunk_pages = 2
    config.translation_attempt_timeout_seconds = 10
    source = make_pdf(tmp_path / "paper.pdf", 6)
    output = tmp_path / "translated"
    clock = [0]
    calls = []
    monkeypatch.setattr(pdf_tools.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(pdf_tools.shutil, "which", lambda _b: "/usr/bin/pdf2zh")

    def run(command, **_kwargs):
        calls.append(1)
        translated_output(command)
        clock[0] += 11
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(pdf_tools, "_run_translation_process", run)
    with pytest.raises(pdf_tools.PDFError, match="预算 10s 已耗尽"):
        translate_dual(source, output, config)
    assert len(calls) == 1
    assert len(list(output.glob("segments/*/*/complete.json"))) == 1
    assert not (output / "paper-dual.pdf").exists()


def test_translation_timeout_kills_children_and_preserves_diagnostics():
    import os
    import subprocess
    import sys
    import time

    import pytest

    started = time.monotonic()
    code = "import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']); print(p.pid,flush=True); time.sleep(60)"
    with pytest.raises(subprocess.TimeoutExpired) as exc:
        pdf_tools._run_translation_process(
            [sys.executable, "-c", code], timeout=0.4, env=os.environ.copy()
        )
    assert time.monotonic() - started < 5
    assert str(exc.value.stdout).strip().isdigit()


def test_sixty_five_page_paper_is_fully_translated_and_never_truncated(
    config, tmp_path, monkeypatch
):
    config.translation_enabled = True
    config.translation_service = "google"
    source = make_pdf(tmp_path / "long.pdf", 65)
    calls = []
    monkeypatch.setattr(pdf_tools.shutil, "which", lambda _b: "/usr/bin/pdf2zh")

    def run(command, **_kwargs):
        calls.append(len(PdfReader(command[1]).pages))
        translated_output(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(pdf_tools, "_run_translation_process", run)
    result = translate_dual(source, tmp_path / "translated", config)
    assert calls == [5] * 13
    assert len(PdfReader(result).pages) == 130


def test_full_text_limit_fails_explicitly_instead_of_silently_truncating(tmp_path):
    import pytest

    source = make_pdf(tmp_path / "long.pdf", 65)
    with pytest.raises(pdf_tools.PDFError, match="拒绝静默截断"):
        pdf_tools.extract_text(source, max_pages=60)
