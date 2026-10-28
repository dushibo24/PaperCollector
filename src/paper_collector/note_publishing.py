from __future__ import annotations

import hashlib
import secrets
import shlex
import subprocess
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath

from paper_collector.config import AppConfig
from paper_collector.notes_http import PUBLIC_FILENAME


class NotePublishError(RuntimeError):
    pass


def publish_remote_note(config: AppConfig, path: Path, filename: str) -> None:
    if not config.reading_notes_publish_ssh_host:
        return
    if not PUBLIC_FILENAME.fullmatch(filename):
        raise NotePublishError("拒绝上传非法的阅读笔记文件名")
    directory = PurePosixPath(config.reading_notes_publish_ssh_directory) / "notes"
    destination = directory / filename
    temporary = directory / f".{filename}.{secrets.token_hex(8)}.tmp"
    command = [
        "ssh",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "ClearAllForwardings=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ConnectTimeout=15",
    ]
    if config.reading_notes_publish_ssh_port is not None:
        command.extend(["-p", str(config.reading_notes_publish_ssh_port)])
    quote = shlex.quote
    with path.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    verify_script = (
        "import hashlib, os, sys\n"
        "digest = hashlib.sha256()\n"
        "with open(sys.argv[1], 'rb') as source:\n"
        "    for chunk in iter(lambda: source.read(1024 * 1024), b''):\n"
        "        digest.update(chunk)\n"
        "if digest.hexdigest() != sys.argv[2] or os.path.getsize(sys.argv[1]) != int(sys.argv[3]):\n"
        "    raise SystemExit('Incomplete HTML upload; original publication unchanged')\n"
    )
    remote_command = (
        f"set -eu; umask 077; mkdir -p -- {quote(str(directory))}; "
        f"temporary={quote(str(temporary))}; "
        "trap 'rm -f -- \"$temporary\"' EXIT HUP INT TERM; "
        'cat > "$temporary"; '
        f'python3 -c {quote(verify_script)} "$temporary" {quote(digest)} {path.stat().st_size}; '
        'chmod 600 "$temporary"; '
        f'mv -f -- "$temporary" {quote(str(destination))}'
    )
    command.extend([config.reading_notes_publish_ssh_host, remote_command])
    try:
        with path.open("rb") as source:
            completed = subprocess.run(
                command,
                stdin=source,
                capture_output=True,
                text=True,
                check=False,
                timeout=config.reading_notes_publish_timeout_seconds,
            )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NotePublishError(f"阅读笔记 SSH 上传失败，未发送推送：{exc}") from exc
    if completed.returncode:
        raise NotePublishError(
            f"阅读笔记 SSH 上传失败（exit={completed.returncode}），未发送推送："
            + (completed.stderr or "无错误详情")[-1200:]
        )
    url = f"{config.reading_notes_base_url}/notes/{filename}"
    request = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            content_length = response.headers.get("Content-Length", "")
            accessible = (
                response.status == 200
                and content_type == "text/html"
                and content_length == str(path.stat().st_size)
                and response.geturl() == url
            )
    except OSError as exc:
        raise NotePublishError(
            "笔记已上传，但公开 HTML 地址无法访问；保留缓存，不发送推送"
        ) from exc
    if not accessible:
        raise NotePublishError("笔记已上传，但公开地址未返回匹配的 HTML；不发送错误链接")
