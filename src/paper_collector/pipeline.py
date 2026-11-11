from __future__ import annotations

import fcntl
import json
import logging
import re
import shutil
import signal
import sqlite3
import threading
import time
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import fields, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from paper_collector.codex_agent import AgentError, AgentUnavailableError, CodexAgent
from paper_collector.config import AppConfig
from paper_collector.db import StateDB, normalized_title
from paper_collector.models import Paper, Review
from paper_collector.notifiers import NotifyError, ServerChanNotifier, WeComNotifier
from paper_collector.official_sources import OfficialSources
from paper_collector.pdf_tools import (
    PDFError,
    download_pdf,
    extract_text,
    safe_stem,
    translate_dual,
)
from paper_collector.publishers import BaiduPCSGoPublisher
from paper_collector.reading_notes import PUBLIC_FILENAME, ReadingNoteError, ReadingNotes
from paper_collector.scoring import score_paper
from paper_collector.sources import (
    ArxivSource,
    OpenAlexSource,
    SemanticScholarEnricher,
    SourceError,
)

LOGGER = logging.getLogger("paper_collector")


class PipelineBusyError(RuntimeError):
    pass


class RunBudgetExceeded(RuntimeError):
    pass


@contextmanager
def execution_budget(seconds):
    """The CLI's whole run cannot keep the recovery lock indefinitely."""
    if (
        not hasattr(signal, "setitimer")
        or threading.current_thread() is not threading.main_thread()
    ):
        yield
        return

    def exceeded(_signal, _frame):
        raise RunBudgetExceeded(f"任务总运行预算 {seconds}s 已耗尽；保留缓存，等待定时恢复")

    previous_handler = signal.signal(signal.SIGALRM, exceeded)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, seconds)
    started = time.monotonic()
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            remaining = max(0.001, previous_timer[0] - (time.monotonic() - started))
            signal.setitimer(signal.ITIMER_REAL, remaining, previous_timer[1])


@contextmanager
def single_instance(lock_path: Path) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PipelineBusyError("已有一个 Paper Collector 实例正在运行") from exc
        yield


def _review_dict(review: Review) -> dict:
    return {field.name: getattr(review, field.name) for field in fields(review)}


def render_brief(paper: Paper, review: Review) -> str:
    authors = "、".join(author.name for author in paper.authors[:5])
    if len(paper.authors) > 5:
        authors += " 等"
    venue = paper.venue or "预印本"
    contributions = "\n".join(f"- {item}" for item in review.contributions)
    evidence = "\n".join(f"- {item}" for item in review.evidence)
    limitations = "\n".join(f"- {item}" for item in review.limitations)
    tags = " ".join(f"#{item.replace(' ', '_')}" for item in review.tags)
    return f"""# 今日论文｜{review.title_zh}

> {review.hook}

**原题**：{paper.title}  
**作者**：{authors}  
**发表**：{venue} · {paper.publication_date}  
**推荐分**：{review.overall_score:.1f}/10（置信度 {review.confidence:.0%}）  
**链接**：{paper.landing_url or paper.pdf_url or ""}

## 为什么今天值得看

{review.why_now}

## 论文讲了什么

{review.summary}

## 核心贡献

{contributions}

## 关键实验依据

{evidence}

## 冷静看：限制与疑点

{limitations}

**适合谁读**：{review.audience}

{tags}
"""


def brief_for_wecom(paper: Paper, review: Review, note_url: str = "") -> str:
    contributions = "\n".join(f"> • {item}" for item in review.contributions[:3])
    limitations = "；".join(review.limitations[:2])
    content = f"""## 今日论文｜{review.title_zh}
> **{review.hook}**

{review.summary}

**核心贡献**
{contributions}

**冷静看**：{limitations}
<font color=\"info\">推荐 {review.overall_score:.1f}/10 · {paper.venue or "预印本"} · {paper.publication_date}</font>"""
    suffix = note_link(note_url)
    if suffix:
        budget = 4096 - len(suffix.encode("utf-8")) - len("…".encode())
        if budget < 0:
            raise NotifyError("阅读笔记链接超过企业微信消息长度限制")
        if len((content + suffix).encode("utf-8")) > 4096:
            content = content.encode("utf-8")[:budget].decode("utf-8", errors="ignore") + "…"
    return content + suffix


def note_link(url: str) -> str:
    return f"\n\n---\n\n📖 [阅读完整笔记（HTML）]({url})\n" if url else ""


def brief_for_serverchan(
    paper: Paper, review: Review, share_url: str, share_password: str, note_url: str = ""
) -> str:
    contributions = "\n".join(f"- {item}" for item in review.contributions[:4])
    evidence = "\n".join(f"- {item}" for item in review.evidence[:4])
    limitations = "\n".join(f"- {item}" for item in review.limitations[:3])
    return f"""# 今日论文｜{review.title_zh}

> {review.hook}

{review.summary}

## 核心贡献

{contributions}

## 关键证据

{evidence}

## 冷静看

{limitations}

## 下载论文 ZIP

[打开百度网盘分享]({share_url})  
提取码：`{share_password}`  
ZIP 内含英文原文、中英双语 PDF 和中文导读。
链接有效期以百度网盘页面显示为准。

---

原题：{paper.title}  
发表：{paper.venue or "预印本"} · {paper.publication_date}  
推荐分：{review.overall_score:.1f}/10  
[论文原始页面]({paper.landing_url or paper.pdf_url or share_url})
""" + note_link(note_url)


class Pipeline:
    def __init__(self, config: AppConfig):
        self.config = config
        self.config.data_dir.mkdir(parents=True, exist_ok=True)
        self.config.knowledge_base_dir.mkdir(parents=True, exist_ok=True)
        self.db = StateDB(self.config.data_dir / "state.sqlite3")
        self.openalex = OpenAlexSource(config)
        self.arxiv = ArxivSource(config)
        self.official = OfficialSources(config)
        self.s2 = SemanticScholarEnricher(
            config.semantic_scholar_api_key,
            reserve_request=lambda: self.db.reserve_api_request(
                "semantic_scholar", SemanticScholarEnricher.default_daily_request_limit
            ),
        )
        self.agent = CodexAgent(config)

    def close(self) -> None:
        self.db.close()

    def discover(self, run_id: int | None = None, *, expansion: int = 0) -> list[Paper]:
        merged: dict[str, Paper] = {}
        ids_by_title: dict[str, str] = {}
        interests = self.config.interests
        if expansion:
            expanded = replace(
                self.config,
                recent_days=min(365, self.config.recent_days * (expansion + 1)),
                per_interest_limit=min(100, self.config.per_interest_limit * (expansion + 1)),
            )
            # Re-query recent-work APIs with a larger window, more results and a
            # different keyword slice. Do not refetch slow conference archives.
            sources = [OpenAlexSource(expanded), ArxivSource(expanded)]
            interests = []
            for interest in self.config.interests:
                keywords = interest.keywords
                shift = (
                    (expansion * 6 if len(keywords) > 6 else expansion) % len(keywords)
                    if keywords
                    else 0
                )
                interests.append(replace(interest, keywords=keywords[shift:] + keywords[:shift]))
            self.db.event("search_expanded", run_id=run_id, detail=f"level={expansion}")
            LOGGER.info(
                "扩大论文搜索：第 %s 级，时间窗 %s 天，每方向 %s 条",
                expansion,
                expanded.recent_days,
                expanded.per_interest_limit,
            )
        else:
            sources = [self.openalex, self.arxiv]
            if self.config.official_enabled:
                sources.append(self.official)
        for interest in interests:
            for source in sources:
                try:
                    found = source.discover(interest)
                except SourceError as exc:
                    LOGGER.warning(
                        "%s / %s 抓取失败：%s", source.__class__.__name__, interest.name, exc
                    )
                    self.db.event("source_error", run_id=run_id, detail=str(exc))
                    continue
                for paper in found:
                    title_key = normalized_title(paper.title)
                    merge_id = paper.id if paper.id in merged else ids_by_title.get(title_key)
                    if merge_id:
                        current = merged[merge_id]
                        current.matched_interests = sorted(
                            set(current.matched_interests + paper.matched_interests)
                        )
                        incoming_is_official = paper.source.startswith("official:")
                        if paper.abstract and len(paper.abstract) > len(current.abstract):
                            current.abstract = paper.abstract
                        if paper.doi:
                            current.doi = current.doi or paper.doi
                        if paper.authors and (
                            not current.authors
                            or (
                                incoming_is_official
                                and not any(author.openalex_id for author in current.authors)
                            )
                        ):
                            current.authors = paper.authors
                        if paper.venue and (incoming_is_official or not current.venue):
                            current.venue = paper.venue
                            current.venue_type = paper.venue_type or current.venue_type
                        if paper.publication_date and (
                            incoming_is_official or not current.publication_date
                        ):
                            current.publication_date = paper.publication_date
                            current.year = paper.year
                        if paper.pdf_url and (incoming_is_official or not current.pdf_url):
                            current.pdf_url = paper.pdf_url
                        if paper.landing_url and (incoming_is_official or not current.landing_url):
                            current.landing_url = paper.landing_url
                        if incoming_is_official:
                            current.source = paper.source
                        current.cited_by_count = max(current.cited_by_count, paper.cited_by_count)
                        current.influential_citation_count = max(
                            current.influential_citation_count,
                            paper.influential_citation_count,
                        )
                        current.concepts = list(dict.fromkeys(current.concepts + paper.concepts))
                    else:
                        merged[paper.id] = paper
                        ids_by_title[title_key] = paper.id
        papers = [score_paper(paper, self.config) for paper in merged.values()]
        for paper in papers:
            self.db.upsert_paper(paper)
        self.db.event("discovered", run_id=run_id, detail=f"{len(papers)} unique works")
        return papers

    @staticmethod
    def _topic_signature(paper: Paper, review: Review) -> set[str]:
        signature = {
            f"interest:{item.casefold().strip()}"
            for item in paper.matched_interests
            if item.strip()
        }
        signature.update(f"tag:{item.casefold().strip()}" for item in review.tags if item.strip())
        generic = {
            "with",
            "using",
            "based",
            "from",
            "model",
            "models",
            "learning",
            "system",
            "systems",
            "network",
            "networks",
            "paper",
        }
        signature.update(
            f"title:{token}"
            for token in re.findall(r"[a-z0-9][a-z0-9+.-]{2,}", paper.title.casefold())
            if token not in generic
        )
        return signature

    def _select_queued_delivery(self):
        queued = self.db.queued_deliveries()
        if not queued:
            return None
        recent_signatures: list[set[str]] = []
        for row in self.db.recent_deliveries(self.config.diversity_history_days):
            paper = Paper.from_dict(json.loads(row["paper_json"]))
            review = Review.from_dict(json.loads(row["review_json"]))
            recent_signatures.append(self._topic_signature(paper, review))

        def selection_score(row) -> tuple[float, float]:
            paper = Paper.from_dict(json.loads(row["paper_json"]))
            review = Review.from_dict(json.loads(row["review_json"]))
            signature = self._topic_signature(paper, review)
            max_overlap = 0.0
            for recent in recent_signatures:
                union = signature | recent
                if union:
                    max_overlap = max(max_overlap, len(signature & recent) / len(union))
            quality = review.overall_score + review.timeliness_score * 0.05
            return quality - self.config.diversity_penalty * max_overlap, review.overall_score

        return max(queued, key=selection_score)

    def _prepare_candidates(self, *, exclude_ids: set[str] | None = None) -> list[Paper]:
        # Discovery order reflects source/interest iteration, not paper merit.
        # Score the full local pool before imposing the expensive enrichment budget.
        excluded = exclude_ids or set()
        papers = [
            score_paper(paper, self.config)
            for paper in self.db.eligible_papers(limit=None)
            if paper.id not in excluded
        ]
        papers.sort(key=lambda item: item.deterministic_score, reverse=True)
        budget = max(20, self.config.shortlist_size * 4)
        cutoff = datetime.now(UTC).date() - timedelta(days=max(365, self.config.recent_days))

        def recent(paper):
            try:
                return date.fromisoformat(paper.publication_date) >= cutoff
            except (TypeError, ValueError):
                return False

        # Reserve most slots for relevant recent work, without discarding classics.
        selected = [paper for paper in papers if recent(paper) and paper.matched_interests][
            : budget * 3 // 4
        ]
        selected_ids = {paper.id for paper in selected}
        selected += [paper for paper in papers if paper.id not in selected_ids][
            : budget - len(selected)
        ]
        enriched: list[Paper] = []
        for paper in selected:
            paper = self.s2.enrich(paper)
            if any(author.openalex_id for author in paper.authors):
                paper = self.openalex.enrich_authors(paper)
            paper = score_paper(paper, self.config)
            self.db.upsert_paper(paper)
            enriched.append(paper)
        enriched.sort(key=lambda item: item.deterministic_score, reverse=True)
        return enriched

    def _artifact_dir(self, paper: Paper) -> Path:
        return self.config.knowledge_base_dir / "papers" / safe_stem(paper.id)

    def _cache_review_summary(self, paper: Paper, review: Review) -> Path:
        artifact_dir = self._artifact_dir(paper)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        brief = artifact_dir / "导读.md"
        brief.write_text(render_brief(paper, review), encoding="utf-8")
        self.db.save_artifacts(paper.id, brief_path=brief)
        return brief

    def _create_delivery_zip(
        self,
        paper: Paper,
        review: Review,
        original: Path,
        dual: Path,
        brief: Path,
        reading_note: Path | None = None,
    ) -> Path:
        artifact_dir = self._artifact_dir(paper)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        package = artifact_dir / (
            f"{safe_stem(review.title_zh, 60)}-{safe_stem(paper.id, 18)}-论文包.zip"
        )
        temporary = package.with_suffix(".zip.tmp")
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(original, "论文原文.pdf")
            archive.write(dual, "中英双语.pdf")
            archive.write(brief, "中文导读.md")
            if reading_note:
                archive.write(reading_note, "阅读笔记.html")
        temporary.replace(package)
        self.db.save_artifacts(paper.id, package_path=package)
        return package

    def _ensure_reading_note(self, paper: Paper, review: Review, original: Path) -> Path:
        path, url = ReadingNotes(self.config, self.agent).ensure(
            paper, review, original, self._artifact_dir(paper)
        )
        self.db.save_reading_note(paper.id, path, url)
        self.db.event("reading_note_published", work_id=paper.id, detail=url)
        return path

    def write_reading_note(self, paper_id: str) -> str:
        if not self.config.reading_notes_enabled:
            raise RuntimeError("请先启用 reading_notes.enabled 并配置 public_base_url")
        with single_instance(self.config.data_dir / "paper-collector.lock"):
            row = self.db.work_row(paper_id)
            if row is None or not row["review_json"]:
                raise RuntimeError("未找到这篇论文的全文审读记录，请先运行论文筛选")
            paper = Paper.from_dict(json.loads(row["paper_json"]))
            review = Review.from_dict(json.loads(row["review_json"]))
            original = Path(row["original_pdf"]) if row["original_pdf"] else None
            if not original or not original.exists():
                if not paper.pdf_url:
                    raise PDFError("没有可公开下载的 PDF URL")
                original = download_pdf(paper.pdf_url, self._artifact_dir(paper) / "original.pdf")
                self.db.save_artifacts(paper.id, original_pdf=original)
            self._ensure_reading_note(paper, review, original)
            return self.db.work_row(paper.id)["reading_note_url"]

    def _ensure_artifacts(
        self, paper: Paper, review: Review, row=None
    ) -> tuple[Path, Path, Path, Path]:
        artifact_dir = self._artifact_dir(paper)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        original = Path(row["original_pdf"]) if row is not None and row["original_pdf"] else None
        if not original or not original.exists():
            if not paper.pdf_url:
                raise PDFError("没有可公开下载的 PDF URL")
            original = download_pdf(paper.pdf_url, artifact_dir / "original.pdf")
            self.db.save_artifacts(paper.id, original_pdf=original)
        brief = Path(row["brief_path"]) if row is not None and row["brief_path"] else None
        if not brief or not brief.exists():
            brief = artifact_dir / "导读.md"
        brief.write_text(render_brief(paper, review), encoding="utf-8")
        self.db.save_artifacts(paper.id, brief_path=brief)
        dual = Path(row["dual_pdf"]) if row is not None and row["dual_pdf"] else None
        if not dual or not dual.exists():
            LOGGER.info("开始准备双语 PDF：%s", paper.title)
            translated = translate_dual(original, artifact_dir / "translated", self.config)
            suffix = "中英双语" if self.config.translation_enabled else "英文原文"
            friendly = artifact_dir / (
                f"{safe_stem(paper.id, 18)}-{safe_stem(review.title_zh, 60)}-{suffix}.pdf"
            )
            if translated != friendly:
                shutil.copy2(translated, friendly)
            dual = friendly
            self.db.save_artifacts(paper.id, dual_pdf=dual)
            LOGGER.info("双语 PDF 已就绪：%s", paper.title)
        if self.config.reading_notes_enabled:
            LOGGER.info("开始生成、验收并发布完整 HTML 阅读笔记：%s", paper.title)
        reading_note = (
            self._ensure_reading_note(paper, review, original)
            if self.config.reading_notes_enabled
            else None
        )
        package = Path(row["package_path"]) if row is not None and row["package_path"] else None
        if not package or not package.exists() or reading_note:
            package = self._create_delivery_zip(paper, review, original, dual, brief, reading_note)
        self.db.set_status(paper.id, "translated")
        return original, dual, brief, package

    def _deliver(self, paper: Paper, review: Review, package: Path, row=None) -> None:
        channel = self.config.notify_channel
        row = self.db.work_row(paper.id)
        note_url = ""
        if self.config.reading_notes_enabled:
            note_url = row["reading_note_url"]
            filename = Path(urlsplit(note_url or "").path).name
            if (
                not note_url
                or not PUBLIC_FILENAME.fullmatch(filename)
                or note_url != f"{self.config.reading_notes_base_url}/notes/{filename}"
                or not row["reading_note_path"]
                or not Path(row["reading_note_path"]).is_file()
                or not (self.config.reading_notes_dir / "notes" / filename).is_file()
            ):
                raise NotifyError("阅读笔记尚未成功发布，保留状态等待重试，不发送缺失笔记的推送")
        if channel == "serverchan":
            if not self.config.serverchan_sendkey:
                raise NotifyError("缺少 SERVERCHAN_SENDKEY；产物已生成但未推送")
            share_url = row["share_url"]
            share_password = row["share_password"]
            if not share_url:
                share = BaiduPCSGoPublisher(self.config).publish(package)
                self.db.save_share(paper.id, share.remote_path, share.url, share.password)
                share_url = share.url
                share_password = share.password
            ServerChanNotifier(self.config.serverchan_sendkey).send(
                f"今日论文｜{review.title_zh}",
                brief_for_serverchan(paper, review, share_url, share_password or "", note_url),
            )
            self.db.mark_link_delivered(paper.id)
            LOGGER.info("Server酱已接受论文推送任务：%s", paper.title)
            return

        if channel == "wecom":
            if not self.config.wecom_enabled:
                raise NotifyError("notify.wecom_enabled=false；产物已生成但未推送")
            if not self.config.wecom_webhook_key:
                raise NotifyError("缺少 WECOM_WEBHOOK_KEY；产物已生成但未推送")
            notifier = WeComNotifier(self.config.wecom_webhook_key)
            summary_done = bool(row["summary_delivered_at"])
            if not summary_done:
                notifier.send_markdown(brief_for_wecom(paper, review, note_url))
                self.db.mark_summary_delivered(paper.id)
            notifier.send_file(package)
            self.db.mark_file_delivered(paper.id)
            return

        raise NotifyError("notify.channel=none；产物已生成但未推送")

    def _resume_pending(self, row, *, no_notify: bool) -> str:
        paper = Paper.from_dict(json.loads(row["paper_json"]))
        review = Review.from_dict(json.loads(row["review_json"]))
        _, _, _, package = self._ensure_artifacts(paper, review, row=row)
        if no_notify:
            LOGGER.info("待推送产物已准备：%s", package)
            return paper.id
        self._deliver(paper, review, package, row=row)
        return paper.id

    def _deliver_ready(self, run_id: int, no_notify: bool, errors: list[Exception]) -> str | None:
        """An individual paper's build failure must not terminate the daily run."""
        attempted = set()
        while True:
            row = self.db.inflight_delivery() or self._select_queued_delivery()
            if row is None or row["id"] in attempted:
                return None
            paper_id = row["id"]
            attempted.add(paper_id)
            self.db.select_run_paper(run_id, paper_id)
            try:
                result = self._resume_pending(row, no_notify=no_notify)
            except (PDFError, ReadingNoteError) as exc:
                # Never change papers after a partial notification has been sent.
                if row["summary_delivered_at"]:
                    raise
                errors.append(exc)
                self.db.defer_delivery(paper_id, str(exc))
                self.db.event("delivery_deferred", run_id, paper_id, str(exc))
                LOGGER.warning(
                    "论文产物准备失败，保留分段及缓存并换下一篇：%s：%s", row["title"], exc
                )
                continue
            self.db.event("prepared" if no_notify else "delivered", run_id, result, "")
            self.db.finish_run(run_id, "prepared" if no_notify else "delivered", result)
            return result

    def check_daily_delivery(self) -> bool:
        """Independent audit: a running/hung worker is not proof of delivery."""
        if self.db.delivered_on(datetime.now(ZoneInfo("Asia/Shanghai")).date()):
            return True
        row = self.db.connection.execute("SELECT id FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        self._notify_run_outcome(row[0] if row else None, "overdue")
        return False

    def _notify_run_outcome(self, run_id: int | None, status: str) -> None:
        """Report an empty/failed run without pretending a paper was delivered.

        Never include raw provider/HTTP errors: they may contain private prompts or
        credential-bearing URLs. A notification failure must not hide the run error.
        """
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        day_start = datetime.combine(
            today, datetime.min.time(), ZoneInfo("Asia/Shanghai")
        ).astimezone(UTC)
        prior = self.db.connection.execute(
            "SELECT 1 FROM events WHERE event='run_notice_sent' AND detail=? AND julianday(created_at)>=julianday(?) LIMIT 1",
            (status, day_start.isoformat()),
        ).fetchone()
        if prior:
            return
        title = "论文推送｜任务失败" if status == "failed" else "论文推送｜今日暂无合格论文"
        content = (
            "今日任务已启动，但处理或推送失败，尚未确认本次完整投递。请检查服务器运行日志；修复后可从保存的状态重试。"
            if status == "failed"
            else "今日任务经过多轮搜索与审读，仍未选出达到门槛且产物齐备的论文，因此没有发送论文。"
            "这不代表没有新论文，也不会降低质量门槛强行推荐。"
        )
        if status == "overdue":
            title = "论文推送｜今日投递仍未完成"
            content = "截止今日投递核查，数据库仍没有完整投递成功记录。任务启动、审读通过或部分产物完成均不算成功。请检查服务状态及运行日志。"
        try:
            if self.config.notify_channel == "serverchan" and self.config.serverchan_sendkey:
                ServerChanNotifier(self.config.serverchan_sendkey).send(title, content)
            elif (
                self.config.notify_channel == "wecom"
                and self.config.wecom_enabled
                and self.config.wecom_webhook_key
            ):
                WeComNotifier(self.config.wecom_webhook_key).send_markdown(
                    f"**{title}**\n\n{content}"
                )
            else:
                return
            self.db.event("run_notice_sent", run_id=run_id, detail=status)
        except (NotifyError, OSError, sqlite3.Error) as exc:
            LOGGER.warning("任务状态通知失败（%s）；不覆盖原始任务状态", type(exc).__name__)

    def run(
        self,
        *,
        dry_run: bool = False,
        no_notify: bool = False,
        skip_discovery: bool = False,
        scheduled: bool = False,
    ) -> str | None:
        with (
            single_instance(self.config.data_dir / "paper-collector.lock"),
            execution_budget(self.config.run_timeout_seconds),
        ):
            if (
                scheduled
                and not dry_run
                and self.db.delivered_on(datetime.now(ZoneInfo("Asia/Shanghai")).date())
            ):
                LOGGER.info("北京时间今日已经完成论文投递，跳过重复定时任务")
                return None
            run_id = self.db.start_run()
            selected_id: str | None = None
            try:
                delivery_errors: list[Exception] = []
                if not dry_run:
                    ready = self._deliver_ready(run_id, no_notify, delivery_errors)
                    if ready:
                        return ready

                if not skip_discovery:
                    self.discover(run_id)
                stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
                run_dir = self.config.data_dir / "runs" / f"{run_id}-{stamp}"
                run_dir.mkdir(parents=True, exist_ok=True)
                attempted: set[str] = set()
                completed_reviews = 0
                review_errors = 0
                had_candidates = False
                for search_round in range(1, self.config.max_search_rounds + 1):
                    # A later round changes both the API search and the local batch.
                    if not skip_discovery and search_round > 1 and search_round % 2 == 0:
                        self.discover(run_id, expansion=search_round // 2)
                    candidates = (
                        self._prepare_candidates()
                        if not attempted
                        else self._prepare_candidates(exclude_ids=attempted)
                    )
                    candidates = [paper for paper in candidates if paper.id not in attempted]
                    had_candidates = had_candidates or bool(candidates)
                    round_dir = run_dir if search_round == 1 else run_dir / f"search-{search_round}"
                    round_dir.mkdir(parents=True, exist_ok=True)
                    LOGGER.info(
                        "论文搜索第 %s/%s 轮：%s 篇候选，已尝试 %s 篇",
                        search_round,
                        self.config.max_search_rounds,
                        len(candidates),
                        len(attempted),
                    )
                    self.db.event(
                        "search_round",
                        run_id=run_id,
                        detail=f"round={search_round}, candidates={len(candidates)}",
                    )
                    if not candidates and skip_discovery and not dry_run:
                        break
                    ranked = self.agent.rank(candidates[:40], round_dir) if candidates else []
                    self.db.event(
                        "ranked", run_id=run_id, detail=json.dumps(ranked, ensure_ascii=False)
                    )
                    if dry_run:
                        selected_id = ranked[0]["paper_id"] if ranked else None
                        self.db.finish_run(run_id, "dry-run", selected_id)
                        return selected_id
                    if not ranked:
                        attempted.update(paper.id for paper in candidates)
                        continue
                    by_id = {paper.id: paper for paper in candidates}
                    deep_review_attempts = 0
                    for rank_context in ranked:
                        if deep_review_attempts >= self.config.max_deep_reviews:
                            break
                        paper = by_id[rank_context["paper_id"]]
                        if paper.id in attempted:
                            continue
                        attempted.add(paper.id)
                        selected_id = paper.id
                        self.db.set_status(paper.id, "ranked")
                        if not paper.pdf_url:
                            self.db.reject(paper.id, "没有公开 PDF，无法全文审读")
                            continue
                        artifact_dir = self._artifact_dir(paper)
                        try:
                            original = download_pdf(paper.pdf_url, artifact_dir / "original.pdf")
                            self.db.save_artifacts(paper.id, original_pdf=original)
                            text = extract_text(original)
                            (artifact_dir / "extracted.txt").write_text(text, encoding="utf-8")
                            deep_review_attempts += 1
                            review = self.agent.review(paper, text, rank_context, round_dir)
                        except AgentUnavailableError:
                            raise
                        except (PDFError, AgentError) as exc:
                            review_errors += 1
                            if isinstance(exc, PDFError):
                                self.db.defer_delivery(paper.id, str(exc))
                            else:
                                self.db.set_status(paper.id, "failed", str(exc))
                            self.db.event("review_error", run_id, paper.id, str(exc))
                            LOGGER.warning("审读 %s 失败：%s", paper.title, exc)
                            continue
                        completed_reviews += 1
                        self.db.save_review(paper.id, review)
                        self._cache_review_summary(paper, review)
                        if (
                            not review.recommend
                            or review.overall_score < self.config.min_recommendation_score
                        ):
                            reason = (
                                review.rejection_reason
                                or f"深读得分 {review.overall_score:.1f} 未达阈值"
                            )
                            self.db.reject(paper.id, reason)
                            self.db.event("rejected", run_id, paper.id, reason)
                            continue
                        self.db.event(
                            "knowledge_cached",
                            run_id,
                            paper.id,
                            str(self._artifact_dir(paper)),
                        )

                    ready = self._deliver_ready(run_id, no_notify, delivery_errors)
                    if ready:
                        return ready
                if delivery_errors:
                    raise PDFError(
                        f"本轮可用论文未完成产物准备，已保留缓存并安排重试：{delivery_errors[-1]}"
                    )
                if review_errors and not completed_reviews:
                    raise RuntimeError(
                        "多轮候选均未能完成全文审读，请检查 PDF 下载或模型服务；不能视为论文未达质量门槛"
                    )
                status = "no-recommendation" if had_candidates else "empty"
                self.db.finish_run(run_id, status, selected_id)
                if not no_notify:
                    self._notify_run_outcome(run_id, status)
                return None
            except Exception as exc:
                active = self.db.connection.execute(
                    "SELECT selected_work_id FROM runs WHERE id=?", (run_id,)
                ).fetchone()
                selected_id = active[0] or selected_id
                if selected_id:
                    self.db.set_status(selected_id, "failed", str(exc))
                self.db.finish_run(run_id, "failed", selected_id, str(exc))
                if not dry_run and not no_notify:
                    self._notify_run_outcome(run_id, "failed")
                raise
