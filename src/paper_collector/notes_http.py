from __future__ import annotations

import argparse
import base64
import hashlib
import logging
import os
import re
import stat
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

LOGGER = logging.getLogger(__name__)
PUBLIC_FILENAME = re.compile(r"[0-9a-f]{32}\.html\Z")
MAX_NOTE_BYTES = 64 * 1024 * 1024
PROGRESS_SCRIPT = "function updateProgress(){const distance=document.documentElement.scrollHeight-innerHeight;document.getElementById('reading-progress').value=distance>0?100*scrollY/distance:100;}addEventListener('scroll',updateProgress,{passive:true});addEventListener('resize',updateProgress);updateProgress();"
SCRIPT_HASH = base64.b64encode(hashlib.sha256(PROGRESS_SCRIPT.encode()).digest()).decode()


class NotesHandler(BaseHTTPRequestHandler):
    server_version = "PaperCollectorNotes/1.0"
    sys_version = ""

    def __init__(self, *args, directory: Path, prefix: str = "", **kwargs):
        self.directory = directory.resolve()
        self.prefix = prefix
        super().__init__(*args, **kwargs)

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, format, *args):
        LOGGER.info("阅读笔记 HTTP 请求：%s", self.client_address[0])

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cache-Control", "private, no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; img-src data:; style-src 'unsafe-inline'; "
            f"script-src 'sha256-{SCRIPT_HASH}'; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
        )
        super().end_headers()

    def do_GET(self):
        self._serve(send_body=True)

    def do_HEAD(self):
        self._serve(send_body=False)

    def _serve(self, *, send_body: bool):
        try:
            path = unquote(urlsplit(self.path).path, errors="strict")
        except (ValueError, UnicodeError):
            self.send_error(400)
            return
        if path == self.prefix + "/healthz":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", "3")
            self.end_headers()
            if send_body:
                self.wfile.write(b"ok\n")
            return
        route = self.prefix + "/notes/"
        filename = path[len(route) :] if path.startswith(route) else ""
        if not PUBLIC_FILENAME.fullmatch(filename):
            self.send_error(404)
            return
        notes_dir = self.directory / "notes"
        target = notes_dir / filename
        if notes_dir.is_symlink() or target.is_symlink():
            self.send_error(404)
            return
        try:
            descriptor = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as handle:
                metadata = os.fstat(handle.fileno())
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_NOTE_BYTES:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(metadata.st_size))
                self.end_headers()
                if send_body:
                    while chunk := handle.read(64 * 1024):
                        self.wfile.write(chunk)
        except FileNotFoundError:
            self.send_error(404)
        except PermissionError:
            self.send_error(403)


def make_static_server(
    directory: Path, host: str, port: int, prefix: str = ""
) -> ThreadingHTTPServer:
    handler = partial(NotesHandler, directory=directory, prefix=prefix)
    return ThreadingHTTPServer((host, port), handler)


def serve_static(directory: Path, host: str, port: int, prefix: str = "") -> None:
    with make_static_server(directory, host, port, prefix) as server:
        LOGGER.info("阅读笔记 HTTP 服务监听 %s:%s", host, server.server_address[1])
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="只公开已发布 HTML 的独立阅读笔记服务")
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--prefix", default="")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    if args.prefix and (
        not args.prefix.startswith("/")
        or any(part in {".", ".."} for part in args.prefix.split("/"))
    ):
        parser.error("prefix must be an absolute URL path without parent traversal")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    serve_static(args.directory, args.host, args.port, args.prefix.rstrip("/"))


if __name__ == "__main__":
    main()
