from __future__ import annotations

from http.server import ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

from paper_collector.config import AppConfig
from paper_collector.notes_http import SCRIPT_HASH, NotesHandler, make_static_server, serve_static

__all__ = ["SCRIPT_HASH", "NotesHandler", "make_notes_server", "serve_notes"]


def make_notes_server(config: AppConfig) -> ThreadingHTTPServer:
    prefix = unquote(urlsplit(config.reading_notes_base_url).path).rstrip("/")
    return make_static_server(
        config.reading_notes_dir, config.reading_notes_host, config.reading_notes_port, prefix
    )


def serve_notes(config: AppConfig) -> None:
    if not config.reading_notes_enabled:
        raise RuntimeError("请先启用 reading_notes.enabled 并配置公网可访问的 public_base_url")
    prefix = unquote(urlsplit(config.reading_notes_base_url).path).rstrip("/")
    serve_static(
        config.reading_notes_dir, config.reading_notes_host, config.reading_notes_port, prefix
    )
