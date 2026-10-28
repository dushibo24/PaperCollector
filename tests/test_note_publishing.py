import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from paper_collector.note_publishing import NotePublishError, publish_remote_note

FILENAME = "e" * 32 + ".html"


@pytest.fixture
def remote_note(config, tmp_path):
    config.reading_notes_base_url = "http://82.156.88.52:8765"
    config.reading_notes_publish_ssh_host = "root@82.156.88.52"
    config.reading_notes_publish_ssh_directory = "/root/paper-collector-notes/public"
    path = tmp_path / FILENAME
    path.write_text("<!doctype html><h1>只有已发布的阅读笔记</h1>", encoding="utf-8")
    return config, path


class HeadResponse:
    status = 200

    def __init__(self, config, path):
        self.url = config.reading_notes_base_url + "/notes/" + FILENAME
        self.headers = {
            "Content-Type": "text/html; charset=utf-8",
            "Content-Length": str(path.stat().st_size),
        }

    def geturl(self):
        return self.url

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass


def test_a100_upload_sends_only_one_html_then_checks_public_url(remote_note, monkeypatch):
    config, path = remote_note
    config.reading_notes_publish_ssh_port = 2222
    calls = []

    def run(command, **kwargs):
        calls.append("ssh")
        assert command[0] == "ssh"
        assert "StrictHostKeyChecking=yes" in command
        assert "BatchMode=yes" in command
        assert command[-2] == "root@82.156.88.52"
        assert ["-p", "2222"] == command[command.index("-p") : command.index("-p") + 2]
        assert "mv -f" in command[-1] and ".tmp" in command[-1]
        assert command[-1].index("hashlib.sha256()") < command[-1].index("mv -f")
        assert kwargs["stdin"].read() == path.read_bytes()
        assert kwargs["timeout"] == 180
        return SimpleNamespace(returncode=0, stderr="")

    def urlopen(request, timeout):
        calls.append("http")
        assert request.method == "HEAD"
        assert request.full_url.endswith("/notes/" + FILENAME)
        assert timeout == 15
        return HeadResponse(config, path)

    monkeypatch.setattr("paper_collector.note_publishing.subprocess.run", run)
    monkeypatch.setattr("paper_collector.note_publishing.urllib.request.urlopen", urlopen)
    publish_remote_note(config, path, FILENAME)
    assert calls == ["ssh", "http"]


def test_local_hosting_does_not_use_ssh_or_http(config, tmp_path, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Local publication must not upload to VPS")

    monkeypatch.setattr("paper_collector.note_publishing.subprocess.run", forbidden)
    monkeypatch.setattr("paper_collector.note_publishing.urllib.request.urlopen", forbidden)
    publish_remote_note(config, tmp_path / "not-required", FILENAME)


def test_failed_upload_does_not_claim_the_vps_link_is_published(remote_note, monkeypatch):
    config, path = remote_note
    monkeypatch.setattr(
        "paper_collector.note_publishing.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=255, stderr="Permission denied"),
    )
    with pytest.raises(NotePublishError, match="SSH 上传失败"):
        publish_remote_note(config, path, FILENAME)


@pytest.mark.parametrize("defect", ["size", "content_type", "status", "redirect"])
def test_inaccessible_or_wrong_public_content_blocks_delivery(remote_note, monkeypatch, defect):
    config, path = remote_note
    monkeypatch.setattr(
        "paper_collector.note_publishing.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stderr=""),
    )
    response = HeadResponse(config, path)
    if defect == "size":
        response.headers["Content-Length"] = "1"
    elif defect == "content_type":
        response.headers["Content-Type"] = "text/plain"
    elif defect == "status":
        response.status = 404
    else:
        response.url = "http://example.test/login"
    monkeypatch.setattr(
        "paper_collector.note_publishing.urllib.request.urlopen", lambda *_args, **_kwargs: response
    )
    with pytest.raises(NotePublishError, match="未返回匹配"):
        publish_remote_note(config, path, FILENAME)


def test_invalid_remote_filename_is_rejected_before_ssh(remote_note):
    config, path = remote_note
    with pytest.raises(NotePublishError, match="非法"):
        publish_remote_note(config, path, "../../config.toml")


def test_interrupted_upload_cannot_replace_an_existing_published_note(
    remote_note, monkeypatch, tmp_path
):
    config, path = remote_note
    destination = tmp_path / "remote-public" / "notes" / FILENAME
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"previous complete note")
    config.reading_notes_publish_ssh_directory = str(destination.parent.parent)
    real_run = subprocess.run

    def interrupted_transfer(command, **_kwargs):
        return real_run(
            ["bash", "-c", command[-1]], input="truncated upload", capture_output=True, text=True
        )

    monkeypatch.setattr("paper_collector.note_publishing.subprocess.run", interrupted_transfer)
    with pytest.raises(NotePublishError):
        publish_remote_note(config, path, FILENAME)
    assert destination.read_bytes() == b"previous complete note"
    assert list(destination.parent.iterdir()) == [destination]


def test_remote_atomic_upload_handles_complete_html_and_quoted_directories(
    remote_note, monkeypatch, tmp_path
):
    config, path = remote_note
    directory = tmp_path / "remote's public directory"
    config.reading_notes_publish_ssh_directory = str(directory)
    real_run = subprocess.run

    def transfer(command, **kwargs):
        return real_run(
            ["bash", "-c", command[-1]], stdin=kwargs["stdin"], capture_output=True, text=True
        )

    monkeypatch.setattr("paper_collector.note_publishing.subprocess.run", transfer)
    monkeypatch.setattr(
        "paper_collector.note_publishing.urllib.request.urlopen",
        lambda *_args, **_kwargs: HeadResponse(config, path),
    )
    publish_remote_note(config, path, FILENAME)
    destination = directory / "notes" / FILENAME
    assert destination.read_bytes() == path.read_bytes()
    assert destination.stat().st_mode & 0o777 == 0o600
    assert list(destination.parent.iterdir()) == [destination]


def test_vps_static_server_is_a_standalone_standard_library_program(tmp_path):
    source = Path(__file__).parents[1] / "src" / "paper_collector" / "notes_http.py"
    target = tmp_path / "server.py"
    shutil.copy2(source, target)
    result = subprocess.run(
        [sys.executable, "-I", str(target), "--help"], capture_output=True, text=True, check=True
    )
    assert "--directory" in result.stdout
    assert "--port" in result.stdout
