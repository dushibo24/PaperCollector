from __future__ import annotations

import base64
import copy
import hashlib
import html
import json
import math
import re
import secrets
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from paper_collector.codex_agent import AgentError, CodexAgent
from paper_collector.config import AppConfig
from paper_collector.models import Paper, Review
from paper_collector.note_publishing import publish_remote_note
from paper_collector.notes_http import MAX_NOTE_BYTES, PROGRESS_SCRIPT, PUBLIC_FILENAME
from paper_collector.reading_note_schema import (
    NOTE_SCHEMA,
    PLAN_SCHEMA,
    SECTION_IDS,
    TEXT_REPAIR_SCHEMA,
)

CAPTION = re.compile(r"^\s*(Figure|Fig\.?|Table)\s*([0-9]+[A-Za-z]?|[IVXLC]+)\b", re.IGNORECASE)
BANNED = ("而是", "而非", "而不是", "并非")


class ReadingNoteError(RuntimeError):
    pass


def banned_passages(value: Any, path: str = "") -> list[dict[str, str]]:
    if isinstance(value, str):
        return [{"path": path, "original": value}] if any(word in value for word in BANNED) else []
    if isinstance(value, dict):
        result = []
        for key, item in value.items():
            escaped = key.replace("~", "~0").replace("/", "~1")
            result.extend(banned_passages(item, path + "/" + escaped))
        return result
    if isinstance(value, list):
        result = []
        for index, item in enumerate(value):
            result.extend(banned_passages(item, path + "/" + str(index)))
        return result
    return []


def apply_text_repairs(note: dict[str, Any], edits: list[dict[str, str]]) -> dict[str, Any]:
    expected = {item["path"]: item["original"] for item in banned_passages(note)}
    if len(edits) != len(expected) or {item.get("path") for item in edits} != set(expected):
        raise ReadingNoteError("局部修订必须逐一对应待修订文本，不能漏改或修改其它字段")
    result = copy.deepcopy(note)
    for edit in edits:
        original = expected[edit["path"]]
        replacement = edit.get("replacement", "")
        if (
            edit.get("original") != original
            or not isinstance(replacement, str)
            or not replacement.strip()
            or any(word in replacement for word in BANNED)
            or re.findall(r"\d+(?:\.\d+)?", original) != re.findall(r"\d+(?:\.\d+)?", replacement)
        ):
            raise ReadingNoteError("局部修订改变了原文/数值，或仍包含禁用措辞")
        parts = [part.replace("~1", "/").replace("~0", "~") for part in edit["path"].split("/")[1:]]
        parent = result
        for part in parts[:-1]:
            parent = parent[int(part)] if isinstance(parent, list) else parent[part]
        parent[int(parts[-1]) if isinstance(parent, list) else parts[-1]] = replacement
    return result


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            temporary.chmod(0o600)
            handle.write(content)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def normalized_label(text: str) -> str:
    match = CAPTION.match(text)
    if not match:
        raise ReadingNoteError(f"图表标签无效：{text}")
    kind = "Table" if match.group(1).lower() == "table" else "Figure"
    return f"{kind} {match.group(2)}"


def pdf_inventory(pdf: Path, workspace: Path) -> tuple[dict[str, Any], list[Path]]:
    try:
        import pymupdf
    except ImportError as exc:
        raise ReadingNoteError("阅读笔记需要 PyMuPDF，请运行 uv sync --extra notes") from exc
    workspace.mkdir(parents=True, exist_ok=True)
    inventory: dict[str, Any] = {"pages": [], "page_count": 0}
    previews = []
    character_count = 0
    with pymupdf.open(pdf) as document:
        if not 1 <= len(document) <= 120:
            raise ReadingNoteError("阅读笔记支持 1–120 页的完整论文，不会静默截断 PDF")
        inventory["page_count"] = len(document)
        for number, page in enumerate(document, start=1):
            if page.rect.width > 3000 or page.rect.height > 5000:
                raise ReadingNoteError(f"PDF p.{number} 的页面尺寸异常")
            blocks = []
            captions = []
            for block in page.get_text("blocks"):
                if len(block) > 6 and block[6] != 0:
                    continue
                text = block[4].strip()
                character_count += len(text)
                rectangle = [round(coordinate, 2) for coordinate in block[:4]]
                blocks.append({"bbox": rectangle, "text": text})
                caption_match = CAPTION.match(text)
                remainder = text[caption_match.end() :] if caption_match else ""
                if caption_match and (
                    not remainder.strip()
                    or remainder.lstrip().startswith((":", "."))
                    or "\n" in remainder[: len(remainder) - len(remainder.lstrip())]
                    or remainder.lstrip()[0].isupper()
                ):
                    captions.append(
                        {"label": normalized_label(text), "bbox": rectangle, "text": text}
                    )
            if character_count > 350000:
                raise ReadingNoteError("论文全文超过笔记上下文上限，不会用截断文本生成笔记")
            drawings = [
                list(rectangle)
                for rectangle in page.cluster_drawings()
                if rectangle.width >= 8 and rectangle.height >= 8
            ]
            images = [list(image["bbox"]) for image in page.get_image_info()]
            item = {
                "page": number,
                "width": page.rect.width,
                "height": page.rect.height,
                "blocks": blocks,
                "captions": captions,
                "images": images,
                "drawing_clusters": drawings,
            }
            if captions or images or drawings:
                preview = workspace / f"page-{number:03d}.png"
                page.get_pixmap(matrix=pymupdf.Matrix(1.3, 1.3), alpha=False).save(preview)
                item["preview"] = str(preview.resolve())
                previews.append(preview)
            inventory["pages"].append(item)
    return inventory, previews


def validate_plan(plan: dict[str, Any], inventory: dict[str, Any]) -> None:
    try:
        planned = set()
        for artifact in plan["artifacts"]:
            page_number = artifact["page"]
            if type(page_number) is not int or not 1 <= page_number <= inventory["page_count"]:
                raise ReadingNoteError("裁剪计划引用了无效的 PDF 页码")
            page = inventory["pages"][page_number - 1]
            label = normalized_label(artifact["label"])
            identity = (label, page_number)
            if identity in planned:
                raise ReadingNoteError(f"重复的图表裁剪：{identity}")
            planned.add(identity)
            coordinates = artifact["bbox"]
            if len(coordinates) != 4 or not all(
                type(value) in (int, float) and math.isfinite(value) for value in coordinates
            ):
                raise ReadingNoteError(f"图表裁剪坐标无效：{identity}")
            left, top, right, bottom = coordinates
            if not (0 <= left < right <= page["width"] and 0 <= top < bottom <= page["height"]):
                raise ReadingNoteError(f"图表裁剪越界：{identity}")
            area = (right - left) * (bottom - top) / (page["width"] * page["height"])
            if right - left < 35 or bottom - top < 15 or area >= 0.92:
                raise ReadingNoteError(f"图表过小或接近整页，拒绝发布：{identity}")
            caption = " ".join(artifact["caption"].split())
            page_text = " ".join(block["text"] for block in page["blocks"])
            if not caption or caption not in " ".join(page_text.split()):
                raise ReadingNoteError(f"原 caption 无法在 PDF 对应页核对：{identity}")
            if normalized_label(caption) != label:
                raise ReadingNoteError(f"caption 与图表标签不一致：{identity}")
            if artifact["role"] not in {"method", "data", "result", "analysis"}:
                raise ReadingNoteError(f"未知图表角色：{identity}")
            for detected in page["captions"]:
                if detected["label"] == label:
                    caption_left, caption_top, caption_right, caption_bottom = detected["bbox"]
                    if not (
                        left <= caption_left + 2
                        and top <= caption_top + 2
                        and right >= caption_right - 2
                        and bottom >= caption_bottom - 2
                    ):
                        raise ReadingNoteError(f"裁剪遗漏了 caption：{identity}")
        expected = {
            (caption["label"], page["page"])
            for page in inventory["pages"]
            for caption in page["captions"]
        }
        if expected - planned:
            raise ReadingNoteError(f"图表覆盖不完整：{sorted(expected - planned)}")
        if not planned and not plan["no_artifacts_reason"].strip():
            raise ReadingNoteError("无图表的论文也必须说明核对结果")
    except (KeyError, TypeError, ValueError) as exc:
        raise ReadingNoteError(f"裁剪计划格式错误：{exc}") from exc


def crop_artifacts(pdf: Path, plan: dict[str, Any], workspace: Path) -> list[Path]:
    import pymupdf

    workspace.mkdir(parents=True, exist_ok=True)
    crops = []
    with pymupdf.open(pdf) as document:
        for number, artifact in enumerate(plan["artifacts"], start=1):
            path = workspace / f"crop-{number:03d}.png"
            page = document[artifact["page"] - 1]
            page.get_pixmap(
                matrix=pymupdf.Matrix(3.2, 3.2),
                clip=pymupdf.Rect(artifact["bbox"]),
                alpha=False,
            ).save(path)
            crops.append(path)
    return crops


def contact_sheets(crops: list[Path], workspace: Path) -> list[Path]:
    import pymupdf

    sheets = []
    for offset in range(0, len(crops), 12):
        batch = crops[offset : offset + 12]
        with pymupdf.open() as document:
            page = document.new_page(width=1410, height=340 * math.ceil(len(batch) / 3))
            for position, crop in enumerate(batch):
                left = position % 3 * 470
                top = position // 3 * 340
                page.insert_text((left + 8, top + 18), f"Crop {offset + position + 1}")
                page.insert_image(
                    pymupdf.Rect(left + 8, top + 28, left + 462, top + 332), filename=crop
                )
            path = workspace / f"contact-sheet-{offset // 12 + 1:02d}.png"
            page.get_pixmap(alpha=False).save(path)
            sheets.append(path)
    return sheets


def validate_note(
    note: dict[str, Any], plan: dict[str, Any], paper_id: str, page_count: int, min_chars: int
) -> None:
    text_parts = []

    def check_pages(pages):
        if not pages or any(type(page) is not int or not 1 <= page <= page_count for page in pages):
            raise ReadingNoteError("笔记证据必须引用有效的物理 PDF 页码")

    def check_paragraph(paragraph):
        text = paragraph["text"]
        if (
            not isinstance(text, str)
            or not text.strip()
            or paragraph["kind"] not in {"claim", "analysis"}
        ):
            raise ReadingNoteError("笔记存在空段落或未区分论文主张和笔记分析")
        check_pages(paragraph["pages"])
        text_parts.append(text)

    try:
        if note["paper_id"] != paper_id:
            raise ReadingNoteError("阅读笔记 paper_id 不匹配")
        if not note["title_zh"].strip() or not note["subtitle"].strip():
            raise ReadingNoteError("阅读笔记缺少直译标题或副标题")
        if tuple(section["id"] for section in note["sections"]) != SECTION_IDS:
            raise ReadingNoteError("阅读笔记必须包含固定顺序的八个章节")
        check_paragraph(note["brief"])
        for section in note["sections"]:
            if not section["paragraphs"] or not section["title"].strip():
                raise ReadingNoteError("阅读笔记章节内容为空")
            for paragraph in section["paragraphs"]:
                check_paragraph(paragraph)
        identities = []
        for artifact in note["artifacts"]:
            if type(artifact["page"]) is not int:
                raise ReadingNoteError("图表页码必须为整数")
            identities.append((normalized_label(artifact["label"]), artifact["page"]))
            if artifact["crop_verified"] is not True:
                raise ReadingNoteError(
                    f"裁剪未通过视觉验收：{artifact['label']} {artifact['limit']}"
                )
            if artifact["section"] not in {"technical", "evaluation-protocol", "experiments"}:
                raise ReadingNoteError("图表归属章节无效")
            for field in ("paper_claim", "analysis", "axes", "interpretation", "limit"):
                check_paragraph(artifact[field])
            if (
                artifact["paper_claim"]["kind"] != "claim"
                or artifact["analysis"]["kind"] != "analysis"
            ):
                raise ReadingNoteError("图表的论文主张与笔记分析必须分开标记")
        expected = {(normalized_label(item["label"]), item["page"]) for item in plan["artifacts"]}
        if set(identities) != expected or len(identities) != len(expected):
            raise ReadingNoteError("图表解读必须逐一覆盖全部裁剪，不能重复或遗漏")
        record = note["data_record"]
        record_value = json.loads(record["json"])
        if not isinstance(record_value, dict) or not record_value:
            raise ReadingNoteError("data-record 必须是非空 JSON 对象")
        if type(record["reconstructed"]) is not bool:
            raise ReadingNoteError("数据记录必须明确标记是否为重构示例")
        check_pages(record["pages"])
        names = [field["name"] for field in record["fields"]]
        if set(names) != set(record_value) or len(names) != len(record_value):
            raise ReadingNoteError("数据记录必须逐字段解释")
        for field in record["fields"]:
            check_paragraph(field["description"])
        if not note["evidence_synthesis"] or not note["discrepancies"]:
            raise ReadingNoteError("缺少证据综合表或来源核对表")
        for row in note["evidence_synthesis"]:
            for key in ("claim", "evidence", "key_values", "boundary"):
                check_paragraph(row[key])
        for row in note["discrepancies"]:
            if not row["item"].strip():
                raise ReadingNoteError("来源核对项目为空")
            check_paragraph(row["claim"])
            check_paragraph(row["verification"])
        serialized = json.dumps(note, ensure_ascii=False)
        if any(word in serialized for word in BANNED):
            raise ReadingNoteError("阅读笔记含 skill 禁用连接词")
        character_count = sum(len(text) for text in text_parts)
        if not min_chars <= character_count <= 80000:
            raise ReadingNoteError(
                f"笔记正文长度 {character_count} 不在 {min_chars}–80000 字符范围内"
            )
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ReadingNoteError(f"阅读笔记数据格式错误：{exc}") from exc


def source_ref(pages: list[int], extra: str = "") -> str:
    label = " · ".join(f"PDF p.{page}" for page in pages)
    return f'<p class="source-ref">{html.escape(label + extra)}</p>'


def paragraph_html(paragraph: dict[str, Any]) -> str:
    label = "论文主张" if paragraph["kind"] == "claim" else "笔记分析"
    return (
        f'<div class="paragraph {paragraph["kind"]}"><p><span class="kind">{label}</span> '
        f"{html.escape(paragraph['text'])}</p>{source_ref(paragraph['pages'])}</div>"
    )


def safe_paper_url(paper: Paper) -> str:
    for value in (paper.landing_url, paper.pdf_url):
        if value and urlsplit(value).scheme in {"http", "https"}:
            return value
    return ""


def render_note(note: dict[str, Any], plan: dict[str, Any], crops: list[Path], paper: Paper) -> str:
    figures = {}
    artifact_lookup = {
        (normalized_label(item["label"]), item["page"]): item for item in note["artifacts"]
    }
    for artifact, crop in zip(plan["artifacts"], crops, strict=True):
        label = normalized_label(artifact["label"])
        explanation = artifact_lookup[(label, artifact["page"])]
        image = base64.b64encode(crop.read_bytes()).decode("ascii")
        body = (
            f'<figure class="artifact evidence-figure" data-label="{html.escape(label)}" '
            f'data-role="{artifact["role"]}"><h3>{html.escape(label)} · PDF p.{artifact["page"]}</h3>'
            f'<img loading="lazy" src="data:image/png;base64,{image}" alt="{html.escape(label)}">'
        )
        for field, title in (
            ("paper_claim", "论文主张"),
            ("analysis", "笔记分析"),
            ("axes", "设置、变量与坐标/行列"),
            ("interpretation", "结果与解释"),
            ("limit", "边界与反例"),
        ):
            body += (
                f'<div class="{field}"><h4>{title}</h4>{paragraph_html(explanation[field])}</div>'
            )
        body += (
            '<details class="original-caption"><summary>原始图注 / 表题</summary>'
            f"<p>{html.escape(artifact['caption'])}</p>{source_ref([artifact['page']])}</details></figure>"
        )
        figures.setdefault(explanation["section"], []).append(body)
    record = note["data_record"]
    reconstructed = (
        " · 字段/记录为解释性重构，不代表真实观测样本" if record["reconstructed"] else ""
    )
    record_html = (
        '<h3>可重建的数据记录 / 输入输出实例</h3><pre class="data-record">'
        + html.escape(json.dumps(json.loads(record["json"]), ensure_ascii=False, indent=2))
        + "</pre>"
        + source_ref(record["pages"], reconstructed)
    )
    for field in record["fields"]:
        record_html += (
            f"<h4>{html.escape(field['name'])}</h4>{paragraph_html(field['description'])}"
        )
    sections = []
    navigation = []
    for section in note["sections"]:
        identity = section["id"]
        title = html.escape(section["title"])
        navigation.append(f'<a href="#{identity}">{title}</a>')
        body = "".join(paragraph_html(paragraph) for paragraph in section["paragraphs"])
        if identity == "story":
            steps = "".join(
                f"<span>{html.escape(paragraph['text'].split('。')[0][:100])}</span>"
                for paragraph in section["paragraphs"][:5]
            )
            body = f'<div class="story-chain">{steps}</div>' + body
        if identity == "technical":
            body = "".join(
                f'<div class="tech-step">{paragraph_html(paragraph)}</div>'
                for paragraph in section["paragraphs"]
            )
            body += record_html
        body += "".join(figures.get(identity, []))
        if identity == "experiments":
            body += '<div class="table-scroll"><table class="evidence-synthesis"><thead><tr>'
            body += "<th>主张</th><th>证据</th><th>关键数值</th><th>边界</th></tr></thead><tbody>"
            for row in note["evidence_synthesis"]:
                body += (
                    "<tr>"
                    + "".join(
                        f"<td>{paragraph_html(row[field])}</td>"
                        for field in ("claim", "evidence", "key_values", "boundary")
                    )
                    + "</tr>"
                )
            body += "</tbody></table></div>"
        if identity == "source-discrepancies":
            body += '<div class="table-scroll"><table class="source-discrepancies"><thead><tr>'
            body += "<th>项目</th><th>论文表述</th><th>核对结果与边界</th></tr></thead><tbody>"
            for row in note["discrepancies"]:
                body += (
                    f"<tr><th>{html.escape(row['item'])}</th>"
                    f"<td>{paragraph_html(row['claim'])}</td>"
                    f"<td>{paragraph_html(row['verification'])}</td></tr>"
                )
            body += "</tbody></table></div>"
        sections.append(f'<section id="{identity}"><h2>{title}</h2>{body}</section>')
    template = (Path(__file__).with_name("note-template.html")).read_text(encoding="utf-8")
    original = safe_paper_url(paper)
    original_link = (
        f'<a href="{html.escape(original, quote=True)}" rel="noreferrer">论文原始页面</a>'
        if original
        else "论文未提供公开链接"
    )
    replacements = {
        "TITLE": html.escape(note["title_zh"]),
        "SUBTITLE": html.escape(note["subtitle"]),
        "ORIGINAL_TITLE": html.escape(paper.title),
        "BRIEF": paragraph_html(note["brief"]),
        "NAVIGATION": "".join(navigation),
        "SECTIONS": "".join(sections),
        "ORIGINAL_LINK": original_link,
        "PROGRESS_SCRIPT": PROGRESS_SCRIPT,
    }
    return re.sub(r"\{\{([A-Z_]+)\}\}", lambda match: replacements[match.group(1)], template)


class ReadingNotes:
    def __init__(self, config: AppConfig, agent: CodexAgent):
        self.config = config
        self.agent = agent

    def _ask(self, template: str, schema: dict[str, Any], workspace: Path, images, **values):
        prompt = (self.config.repo_root / "prompts" / template).read_text(encoding="utf-8")
        prompt = prompt.format(**values)
        stem = Path(template).stem
        schema_path = workspace / f"{stem}.schema.json"
        atomic_write(schema_path, json.dumps(schema, ensure_ascii=False))
        atomic_write(workspace / f"{stem}.prompt.txt", prompt)
        return self.agent._run(
            prompt,
            schema_path,
            workspace / f"{stem}.result.json",
            images=images,
            timeout_seconds=self.config.reading_notes_timeout_seconds,
        )

    def repair_connectives(self, note: dict[str, Any], workspace: Path) -> dict[str, Any]:
        passages = banned_passages(note)
        if not passages:
            return note
        result = self._ask(
            "reading-note-repair.md",
            TEXT_REPAIR_SCHEMA,
            workspace,
            [],
            passages_json=json.dumps(passages, ensure_ascii=False),
        )
        return apply_text_repairs(note, result.get("edits", []))

    def ensure(
        self, paper: Paper, review: Review, pdf: Path, artifact_dir: Path
    ) -> tuple[Path, str]:
        workspace = artifact_dir / "reading-note"
        workspace.mkdir(parents=True, exist_ok=True)
        cache_path = workspace / "validated.json"
        with pdf.open("rb") as handle:
            fingerprint = hashlib.file_digest(handle, "sha256").hexdigest()
        inventory, previews = pdf_inventory(pdf, workspace)
        cache = None
        if cache_path.exists():
            try:
                candidate = json.loads(cache_path.read_text(encoding="utf-8"))
                if candidate.get("pdf_sha256") == fingerprint and candidate.get("version") == 1:
                    validate_plan(candidate["plan"], inventory)
                    validate_note(
                        candidate["note"],
                        candidate["plan"],
                        paper.id,
                        inventory["page_count"],
                        self.config.reading_notes_min_chars,
                    )
                    if PUBLIC_FILENAME.fullmatch(candidate["public_filename"]):
                        cache = candidate
            except (ValueError, TypeError, KeyError, ReadingNoteError):
                cache = None
        if cache is None:
            feedback = ""
            for attempt in range(1, 3):
                attempt_dir = workspace / f"attempt-{attempt}"
                attempt_dir.mkdir(parents=True, exist_ok=True)
                try:
                    plan = self._ask(
                        "reading-note-plan.md",
                        PLAN_SCHEMA,
                        attempt_dir,
                        previews,
                        paper_json=json.dumps(paper.to_dict(), ensure_ascii=False),
                        inventory_json=json.dumps(inventory, ensure_ascii=False),
                        feedback=feedback,
                    )
                    validate_plan(plan, inventory)
                    crops = crop_artifacts(pdf, plan, workspace)
                    sheets = contact_sheets(crops, workspace)
                    note = self._ask(
                        "reading-note.md",
                        NOTE_SCHEMA,
                        attempt_dir,
                        sheets + crops,
                        paper_json=json.dumps(paper.to_dict(), ensure_ascii=False),
                        review_json=json.dumps(asdict(review), ensure_ascii=False),
                        inventory_json=json.dumps(inventory, ensure_ascii=False),
                        plan_json=json.dumps(plan, ensure_ascii=False),
                        min_chars=self.config.reading_notes_min_chars,
                    )
                    note = self.repair_connectives(note, attempt_dir)
                    validate_note(
                        note,
                        plan,
                        paper.id,
                        inventory["page_count"],
                        self.config.reading_notes_min_chars,
                    )
                    cache = {
                        "version": 1,
                        "pdf_sha256": fingerprint,
                        "public_filename": secrets.token_hex(16) + ".html",
                        "plan": plan,
                        "note": note,
                    }
                    break
                except (ReadingNoteError, AgentError) as exc:
                    feedback = str(exc)
            if cache is None:
                raise ReadingNoteError(
                    f"完整阅读笔记两次生成/验收失败，保留状态等待重试：{feedback}"
                )
        atomic_write(cache_path, json.dumps(cache, ensure_ascii=False, indent=2))
        crops = crop_artifacts(pdf, cache["plan"], workspace)
        document = render_note(cache["note"], cache["plan"], crops, paper)
        if len(document.encode("utf-8")) > MAX_NOTE_BYTES:
            raise ReadingNoteError("内嵌图表的笔记超过 64 MiB，拒绝发布无法提供访问的页面")
        local_path = artifact_dir / "阅读笔记.html"
        public_path = self.config.reading_notes_dir / "notes" / cache["public_filename"]
        atomic_write(local_path, document)
        atomic_write(public_path, document)
        publish_remote_note(self.config, public_path, cache["public_filename"])
        self._index(paper, cache)
        return local_path, f"{self.config.reading_notes_base_url}/notes/{cache['public_filename']}"

    def _index(self, paper: Paper, cache: dict[str, Any]) -> None:
        path = self.config.reading_notes_dir / "index.json"
        entries = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        entries[paper.id] = {
            "title": cache["note"]["title_zh"],
            "original_title": paper.title,
            "href": "notes/" + cache["public_filename"],
            "artifact_count": len(cache["plan"]["artifacts"]),
        }
        cards = []
        for entry in entries.values():
            filename = Path(entry["href"]).name
            if not PUBLIC_FILENAME.fullmatch(filename):
                raise ReadingNoteError("阅读索引包含无效文件名")
            if not (path.parent / "notes" / filename).is_file():
                raise ReadingNoteError("阅读索引指向了不存在的笔记")
            cards.append(
                f'<article class="paper-card"><a href="notes/{filename}">'
                f"{html.escape(entry['title'])}</a><p>{html.escape(entry['original_title'])}</p></article>"
            )
        atomic_write(path, json.dumps(entries, ensure_ascii=False, indent=2))
        atomic_write(
            path.with_name("index.html"),
            '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>论文阅读笔记</title>'
            f"<h1>论文阅读笔记 · {len(entries)} 篇</h1>" + "".join(cards) + "</html>",
        )
