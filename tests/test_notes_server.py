import io
from pathlib import Path

import pytest

from paper_collector.notes_server import SCRIPT_HASH, NotesHandler

FILENAME = "a" * 32 + ".html"


class MemorySocket:
    def __init__(self, request):
        self.input = io.BytesIO(request)
        self.output = io.BytesIO()

    def makefile(self, *_args):
        return self.input

    def sendall(self, data):
        self.output.write(data)

    def settimeout(self, _timeout):
        pass


def request(directory: Path, path: str, method="GET", prefix=""):
    connection = MemorySocket(f"{method} {path} HTTP/1.0\r\nHost: example.test\r\n\r\n".encode())
    NotesHandler(connection, ("127.0.0.1", 10000), object(), directory=directory, prefix=prefix)
    header, _, body = connection.output.getvalue().partition(b"\r\n\r\n")
    return int(header.split(b" ", 2)[1]), header.decode(), body


def test_only_generated_html_is_served_with_security_headers(tmp_path):
    directory = tmp_path / "public"
    notes = directory / "notes"
    notes.mkdir(parents=True)
    (notes / FILENAME).write_text("<!doctype html><h1>阅读笔记</h1>")
    status, headers, body = request(directory, "/notes/" + FILENAME)
    assert status == 200
    assert "阅读笔记" in body.decode()
    assert "Content-Type: text/html; charset=utf-8" in headers
    assert f"script-src 'sha256-{SCRIPT_HASH}'" in headers
    assert "frame-ancestors 'none'" in headers
    assert "Referrer-Policy: no-referrer" in headers
    assert "X-Content-Type-Options: nosniff" in headers
    assert "private, no-store" in headers
    status, headers, body = request(directory, "/notes/" + FILENAME, method="HEAD")
    assert status == 200 and body == b""


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/index.html",
        "/index.json",
        "/config.toml",
        "/.env",
        "/state.sqlite3",
        "/notes/",
        "/notes/../../.env",
        "/notes/%2e%2e/%2e%2e/.env",
        "/notes/" + FILENAME + "/../config.toml",
        "/notes/arbitrary.html",
    ],
)
def test_private_files_directory_listing_and_traversal_are_denied(tmp_path, path):
    (tmp_path / ".env").write_text("API_KEY=private")
    status, _, body = request(tmp_path, path)
    assert status == 404
    assert b"API_KEY=private" not in body


def test_symlinked_note_cannot_expose_a_private_file(tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("private")
    directory = tmp_path / "public"
    (directory / "notes").mkdir(parents=True)
    (directory / "notes" / FILENAME).symlink_to(secret)
    assert request(directory, "/notes/" + FILENAME)[0] == 404


def test_health_and_reverse_proxy_prefix(tmp_path):
    assert request(tmp_path, "/reading/healthz", prefix="/reading")[0] == 200
    assert request(tmp_path, "/healthz", prefix="/reading")[0] == 404
    assert request(tmp_path, "/reading/healthz", method="POST", prefix="/reading")[0] == 501
