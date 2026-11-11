from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path

from pypdf import PdfReader, PdfWriter

from paper_collector.config import AppConfig


class PDFError(RuntimeError):
    pass


LOGGER = logging.getLogger(__name__)


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


def extract_text(pdf_path: Path, max_pages: int = 120, max_chars: int = 350_000) -> str:
    try:
        reader = PdfReader(str(pdf_path))
    except Exception as exc:
        raise PDFError(f"无法解析 PDF {pdf_path}: {exc}") from exc
    if len(reader.pages) > max_pages:
        raise PDFError(f"完整 PDF 有 {len(reader.pages)} 页，超过全文审读上限 {max_pages} 页，拒绝静默截断")
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
            raise PDFError(f"完整论文文本超过 {max_chars} 字符，拒绝静默截断")
        sections.append(section)
        total += len(section)
    result = "".join(sections).strip()
    if len(result) < 1000:
        raise PDFError("PDF 可提取文本过少，可能是扫描版或下载损坏")
    return result


def _translate_single(pdf_path: Path, output_dir: Path, config: AppConfig) -> Path:
    if not config.translation_enabled:
        return pdf_path
    if not shutil.which(config.pdf2zh_bin):
        raise PDFError(f"找不到 pdf2zh：{config.pdf2zh_bin}")
    output_dir.mkdir(parents=True, exist_ok=True)
    before = set(output_dir.glob("*.pdf"))
    environment = os.environ.copy()
    if config.translation_service == "openailiked":
        openailiked = {
            "OPENAILIKED_BASE_URL": config.translation_openailiked_base_url,
            "OPENAILIKED_API_KEY": config.translation_openailiked_api_key,
            "OPENAILIKED_MODEL": config.translation_openailiked_model,
        }
        missing = [name for name, value in openailiked.items() if not value]
        if missing:
            raise PDFError(f"OpenAI-liked 翻译配置缺失：{', '.join(missing)}")
        environment.update({name: value for name, value in openailiked.items() if value})
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
    if config.translation_skip_subset_fonts:
        command.append("--skip-subset-fonts")
    started = time.monotonic()
    while True:
        remaining = config.translation_timeout_seconds - (time.monotonic() - started)
        if remaining <= 0:
            raise PDFError(f"pdf2zh 翻译超时（{config.translation_timeout_seconds}s）")
        try:
            completed = _run_translation_process(command, timeout=remaining, env=environment)
        except subprocess.TimeoutExpired as exc:
            detail = _translation_log(output_dir, exc.stdout, exc.stderr, config)
            raise PDFError(
                f"pdf2zh 翻译超时（{config.translation_timeout_seconds}s），进度保留于 {output_dir}；{detail}"
            ) from exc
        detail = _translation_log(output_dir, completed.stdout, completed.stderr, config)
        if completed.returncode == 0:
            break
        if "subset_fonts" in detail and "--skip-subset-fonts" not in command:
            LOGGER.warning("PDF 字体子集化失败，使用现有译文缓存重试兼容模式：%s", pdf_path.name)
            command.append("--skip-subset-fonts")
            continue
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


def _run_translation_process(command, *, timeout, env):
    """Timeouts terminate the whole translator tree, not only the CLI parent."""
    with subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=os.name == "posix",
    ) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except BaseException:
            if os.name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                process.kill()
            stdout, stderr = process.communicate()
            # Preserve timeout diagnostics without retaining a subprocess or lock.
            import sys

            error = sys.exc_info()[1]
            if isinstance(error, subprocess.TimeoutExpired):
                error.stdout, error.stderr = stdout, stderr
            raise
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _translation_log(directory, stdout, stderr, config):
    def text(value):
        return value.decode(errors="replace") if isinstance(value, bytes) else value or ""

    detail = text(stderr) or text(stdout)
    if config.translation_openailiked_api_key:
        detail = detail.replace(config.translation_openailiked_api_key, "[REDACTED]")
    path = directory / "pdf2zh.log"
    with path.open("a", encoding="utf-8") as handle:
        path.chmod(0o600)
        handle.write(detail[-32000:] + "\n")
    return detail[-2000:]


def _pdf_pages(path):
    try:
        return len(PdfReader(str(path)).pages)
    except Exception as exc:
        raise PDFError(f"PDF 结构无效：{path}") from exc


def translate_dual(pdf_path: Path, output_dir: Path, config: AppConfig) -> Path:
    if not config.translation_enabled:
        return pdf_path
    source_pages = _pdf_pages(pdf_path)
    if source_pages < 1:
        raise PDFError("不能翻译空 PDF")
    output_dir.mkdir(parents=True, exist_ok=True)
    source_hash = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    identity = json.dumps(
        {
            "source": source_hash,
            "service": config.translation_service,
            "model": config.translation_openailiked_model,
            "endpoint": config.translation_openailiked_base_url,
            "chunk_pages": config.translation_chunk_pages,
        },
        sort_keys=True,
    )
    key = hashlib.sha256(identity.encode()).hexdigest()[:24]
    cache = output_dir / "segments" / key
    cache.mkdir(parents=True, exist_ok=True)
    target = output_dir / f"{pdf_path.stem}-dual.pdf"
    manifest = output_dir / "translation-manifest.json"
    if manifest.exists() and target.exists():
        try:
            saved = json.loads(manifest.read_text())
            if (
                saved.get("key") == key
                and saved.get("sha256") == hashlib.sha256(target.read_bytes()).hexdigest()
                and _pdf_pages(target) == source_pages * 2
            ):
                return target
        except (OSError, ValueError, PDFError):
            pass
    budget = min(config.translation_timeout_seconds, config.translation_attempt_timeout_seconds)
    started = time.monotonic()
    reader = PdfReader(str(pdf_path))
    parts = []
    for start in range(0, source_pages, config.translation_chunk_pages):
        stop = min(source_pages, start + config.translation_chunk_pages)
        segment = cache / f"pages-{start + 1:04d}-{stop:04d}"
        segment.mkdir(parents=True, exist_ok=True)
        source = segment / "source.pdf"
        done = segment / "complete.json"
        result = segment / "source-dual.pdf"
        if done.exists() and result.exists():
            try:
                record = json.loads(done.read_text())
                if (
                    record.get("sha256") == hashlib.sha256(result.read_bytes()).hexdigest()
                    and _pdf_pages(result) == (stop - start) * 2
                ):
                    parts.append(result)
                    LOGGER.info(
                        "复用已完成翻译分段：PDF p.%s–%s / %s", start + 1, stop, source_pages
                    )
                    continue
            except (OSError, ValueError, PDFError):
                pass
        remaining = budget - (time.monotonic() - started)
        if remaining <= 0:
            raise PDFError(f"单篇翻译处理预算 {budget}s 已耗尽；已完成分段保留，下次从断点继续")
        if not source.exists():
            writer = PdfWriter()
            for page in reader.pages[start:stop]:
                writer.add_page(page)
            with source.open("wb") as handle:
                writer.write(handle)
        LOGGER.info(
            "开始翻译分段：PDF p.%s–%s / %s，当前处理最多 %.0fs",
            start + 1,
            stop,
            source_pages,
            remaining,
        )
        segment_config = replace(
            config,
            translation_timeout_seconds=min(remaining, config.translation_chunk_timeout_seconds),
        )
        result.unlink(missing_ok=True)
        translated = _translate_single(source, segment, segment_config)
        if _pdf_pages(translated) != (stop - start) * 2:
            raise PDFError(f"翻译分段页数不完整：PDF p.{start + 1}–{stop}，拒绝发布残缺双语 PDF")
        if translated != result:
            shutil.copy2(translated, result)
        temporary = done.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"sha256": hashlib.sha256(result.read_bytes()).hexdigest()})
        )
        temporary.replace(done)
        parts.append(result)
    writer = PdfWriter()
    for part in parts:
        writer.append(str(part))
    temporary = target.with_suffix(".tmp.pdf")
    try:
        with temporary.open("wb") as handle:
            writer.write(handle)
        if _pdf_pages(temporary) != source_pages * 2:
            raise PDFError("合并双语 PDF 页数不完整，拒绝发布")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    temporary = manifest.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(
            {
                "key": key,
                "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                "source_pages": source_pages,
            }
        )
    )
    temporary.replace(manifest)
    return target
