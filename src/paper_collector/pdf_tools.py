from __future__ import annotations

import re
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from pypdf import PdfReader

from paper_collector.config import AppConfig


class PDFError(RuntimeError):
    pass


def safe_stem(value: str, limit: int = 90) -> str:
    normalized = re.sub(r"[^\w\-.]+", "-", value, flags=re.UNICODE).strip("-.")
    return (normalized or "paper")[:limit]


def download_pdf(
    url: str, target: Path, timeout: int = 90, max_bytes: int = 100 * 1024 * 1024
) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 PaperCollector/0.1",
            "Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.1",
        },
    )
    temporary = target.with_suffix(".part")
    try:
        with (
            urllib.request.urlopen(request, timeout=timeout) as response,
            temporary.open("wb") as output,
        ):
            total = 0
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise PDFError(f"PDF 超过 {max_bytes // 1024 // 1024}MB 上限")
                output.write(chunk)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        temporary.unlink(missing_ok=True)
        raise PDFError(f"PDF 下载失败 {url}: {exc}") from exc
    with temporary.open("rb") as handle:
        if handle.read(5) != b"%PDF-":
            temporary.unlink(missing_ok=True)
            raise PDFError(f"下载内容不是 PDF：{url}")
    temporary.replace(target)
    return target


def extract_text(pdf_path: Path, max_pages: int = 60, max_chars: int = 150_000) -> str:
    try:
        reader = PdfReader(str(pdf_path))
    except Exception as exc:
        raise PDFError(f"无法解析 PDF {pdf_path}: {exc}") from exc
    sections: list[str] = []
    total = 0
    for page_number, page in enumerate(reader.pages[:max_pages], start=1):
        try:
            text = page.extract_text() or ""
        # Individual malformed pages should not make an otherwise readable paper fail.
        except Exception:  # noqa: BLE001
            text = ""
        section = f"\n\n===== PAGE {page_number} / [p.{page_number}] =====\n{text.strip()}"
        if total + len(section) > max_chars:
            sections.append(section[: max_chars - total])
            break
        sections.append(section)
        total += len(section)
    result = "".join(sections).strip()
    if len(result) < 1000:
        raise PDFError("PDF 可提取文本过少，可能是扫描版或下载损坏")
    return result


def translate_dual(pdf_path: Path, output_dir: Path, config: AppConfig) -> Path:
    if not config.translation_enabled:
        return pdf_path
    if not shutil.which(config.pdf2zh_bin):
        raise PDFError(f"找不到 pdf2zh：{config.pdf2zh_bin}")
    output_dir.mkdir(parents=True, exist_ok=True)
    before = set(output_dir.glob("*.pdf"))
    command = [
        config.pdf2zh_bin,
        str(pdf_path),
        "--lang-in",
        "en",
        "--lang-out",
        "zh",
        "--service",
        config.translation_service,
        "--thread",
        str(config.translation_threads),
        "--output",
        str(output_dir),
    ]
    try:
        completed = subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
            timeout=config.translation_timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise PDFError(f"pdf2zh 翻译超时（{config.translation_timeout_seconds}s）") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout)[-4000:]
        raise PDFError(f"pdf2zh 翻译失败（exit={completed.returncode}）：\n{detail}")
    after = set(output_dir.glob("*.pdf"))
    new_files = after - before
    duals = sorted(
        [path for path in new_files if "dual" in path.stem.casefold()]
        or [path for path in after if "dual" in path.stem.casefold()],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not duals:
        raise PDFError(f"pdf2zh 执行成功但未在 {output_dir} 发现 dual PDF")
    return duals[0]
