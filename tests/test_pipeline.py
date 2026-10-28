import json
import zipfile

import pytest
from test_db import make_review

from paper_collector.models import Author, Paper
from paper_collector.notifiers import NotifyError
from paper_collector.pipeline import Pipeline, brief_for_serverchan, render_brief
from paper_collector.publishers import ShareLink
from paper_collector.reading_notes import ReadingNoteError


def test_note_link_is_at_end_of_both_notification_formats():
    from paper_collector.pipeline import brief_for_wecom

    paper = Paper(id="notes", title="Title", abstract="x", authors=[], publication_date="2026-10-26", year=2026)
    url = "http://198.51.100.25:8765/notes/" + "a" * 32 + ".html"
    serverchan = brief_for_serverchan(paper, make_review(paper.id), "https://pan.baidu.com/s/test", "1234", url)
    wecom = brief_for_wecom(paper, make_review(paper.id), url)
    assert serverchan.rstrip().endswith(f"({url})")
    assert wecom.rstrip().endswith(f"({url})")
    assert "https://pan.baidu.com/s/test" in serverchan


def test_wecom_long_summary_cannot_truncate_the_note_link():
    from paper_collector.pipeline import brief_for_wecom

    paper = Paper(id="long", title="Title", abstract="x", authors=[], publication_date="2026-10-26", year=2026)
    review = make_review(paper.id)
    review.summary = "完整研究背景与实验结论。" * 1000
    url = "http://198.51.100.25:8765/notes/" + "d" * 32 + ".html"
    content = brief_for_wecom(paper, review, url)
    assert len(content.encode("utf-8")) <= 4096
    assert content.rstrip().endswith(f"({url})")


def test_notes_are_generated_before_delivery_and_included_in_new_zip(config, tmp_path, monkeypatch):
    config.reading_notes_enabled = True
    config.reading_notes_base_url = "http://198.51.100.25:8765"
    paper = Paper(id="notes", title="Title", abstract="x", authors=[], publication_date="2026-10-26", year=2026)
    original = tmp_path / "original.pdf"
    original.write_bytes(b"%PDF-test")
    note_path = tmp_path / "reading-note.html"
    note_path.write_text("<h1>Full reading note</h1>")
    filename = "b" * 32 + ".html"
    public = config.reading_notes_dir / "notes" / filename
    public.parent.mkdir(parents=True)
    public.write_bytes(note_path.read_bytes())
    note_url = config.reading_notes_base_url + "/notes/" + filename
    calls = []

    def fake_note(_self, _paper, _review, _original, _directory):
        calls.append(_paper.id)
        return note_path, note_url

    monkeypatch.setattr("paper_collector.pipeline.ReadingNotes.ensure", fake_note)
    pipeline = Pipeline(config)
    try:
        pipeline.db.upsert_paper(paper)
        review = make_review(paper.id)
        pipeline.db.save_review(paper.id, review)
        pipeline.db.save_artifacts(paper.id, original_pdf=original, dual_pdf=original)
        row = pipeline.db.work_row(paper.id)
        _, _, _, package = pipeline._ensure_artifacts(paper, review, row)
        assert calls == [paper.id]
        assert pipeline.db.work_row(paper.id)["reading_note_url"] == note_url
        with zipfile.ZipFile(package) as archive:
            assert archive.read("阅读笔记.html") == note_path.read_bytes()
        assert pipeline.db.work_row(paper.id)["file_delivered_at"] is None
    finally:
        pipeline.close()


def test_missing_note_blocks_notification_before_upload(config, monkeypatch):
    config.reading_notes_enabled = True
    config.notify_channel = "serverchan"
    config.serverchan_sendkey = "test-sendkey"
    paper = Paper(id="missing", title="Title", abstract="x", authors=[], publication_date="2026-10-26", year=2026)

    def must_not_publish(*_args):
        raise AssertionError("No upload or notification is allowed without a note")

    monkeypatch.setattr("paper_collector.pipeline.BaiduPCSGoPublisher", must_not_publish)
    monkeypatch.setattr("paper_collector.pipeline.ServerChanNotifier", must_not_publish)
    pipeline = Pipeline(config)
    try:
        pipeline.db.upsert_paper(paper)
        pipeline.db.save_review(paper.id, make_review(paper.id))
        with pytest.raises(NotifyError, match="阅读笔记"):
            pipeline._deliver(paper, make_review(paper.id), config.data_dir / "not-needed.zip")
        assert pipeline.db.work_row(paper.id)["file_delivered_at"] is None
    finally:
        pipeline.close()


def test_note_failure_preserves_original_and_does_not_mark_delivered(config, tmp_path, monkeypatch):
    config.reading_notes_enabled = True
    paper = Paper(id="note-failed", title="Title", abstract="x", authors=[], publication_date="2026-10-26", year=2026)
    original = tmp_path / "original.pdf"
    original.write_bytes(b"%PDF-test")

    def fail_note(*_args):
        raise ReadingNoteError("visual QA failed")

    monkeypatch.setattr("paper_collector.pipeline.ReadingNotes.ensure", fail_note)
    pipeline = Pipeline(config)
    try:
        pipeline.db.upsert_paper(paper)
        pipeline.db.save_review(paper.id, make_review(paper.id))
        pipeline.db.save_artifacts(paper.id, original_pdf=original, dual_pdf=original)
        with pytest.raises(ReadingNoteError, match="QA"):
            pipeline._ensure_artifacts(paper, make_review(paper.id), pipeline.db.work_row(paper.id))
        row = pipeline.db.work_row(paper.id)
        assert row["original_pdf"] == str(original)
        assert row["reading_note_url"] is None
        assert row["file_delivered_at"] is None
    finally:
        pipeline.close()


@pytest.mark.parametrize("no_notify", [True, False])
def test_queued_run_publishes_note_before_notification(config, tmp_path, monkeypatch, no_notify):
    config.reading_notes_enabled = True
    config.reading_notes_base_url = "http://198.51.100.25:8765"
    config.notify_channel = "serverchan"
    config.serverchan_sendkey = "test-sendkey"
    paper = Paper(id="queued-note", title="Queued paper", abstract="x", authors=[], publication_date="2026-10-26", year=2026)
    original = tmp_path / "original.pdf"
    original.write_bytes(b"%PDF-test")
    filename = "c" * 32 + ".html"
    url = config.reading_notes_base_url + "/notes/" + filename
    calls = []

    def fake_note(_self, _paper, _review, _original, directory):
        calls.append("note")
        note_path = directory / "阅读笔记.html"
        note_path.write_text("<h1>Complete note</h1>")
        public = config.reading_notes_dir / "notes" / filename
        public.parent.mkdir(parents=True, exist_ok=True)
        public.write_bytes(note_path.read_bytes())
        return note_path, url

    class FakePublisher:
        def __init__(self, _config):
            pass

        def publish(self, _package):
            calls.append("upload")
            return ShareLink("/PaperCollector/test.zip", "https://pan.baidu.com/s/test", "1234")

    class FakeNotifier:
        def __init__(self, _key):
            pass

        def send(self, _title, content):
            calls.append("notify")
            assert content.rstrip().endswith(f"({url})")

    monkeypatch.setattr("paper_collector.pipeline.ReadingNotes.ensure", fake_note)
    monkeypatch.setattr("paper_collector.pipeline.BaiduPCSGoPublisher", FakePublisher)
    monkeypatch.setattr("paper_collector.pipeline.ServerChanNotifier", FakeNotifier)
    pipeline = Pipeline(config)
    try:
        pipeline.db.upsert_paper(paper)
        pipeline.db.save_review(paper.id, make_review(paper.id))
        pipeline.db.save_artifacts(paper.id, original_pdf=original, dual_pdf=original)
        monkeypatch.setattr(pipeline, "discover", lambda _run_id: [])
        monkeypatch.setattr(pipeline, "_prepare_candidates", list)
        assert pipeline.run(no_notify=no_notify) == paper.id
        assert calls == (["note"] if no_notify else ["note", "upload", "notify"])
        assert pipeline.db.work_row(paper.id)["reading_note_url"] == url
        assert bool(pipeline.db.work_row(paper.id)["file_delivered_at"]) is (not no_notify)
    finally:
        pipeline.close()


def test_brief_contains_evidence_and_limitations():
    paper = Paper(
        id="x",
        title="Original title",
        abstract="x",
        authors=[Author("Ada"), Author("Lin")],
        publication_date="2026-08-21",
        year=2026,
        venue="NeurIPS",
        landing_url="https://example.test/paper",
    )
    brief = render_brief(paper, make_review("x"))
    assert "关键实验依据" in brief
    assert "证据 [p.1]" in brief
    assert "冷静看" in brief
    assert "https://example.test/paper" in brief


def test_serverchan_brief_contains_baidupan_link_and_password():
    paper = Paper(
        id="x",
        title="Original title",
        abstract="x",
        authors=[Author("Ada")],
        publication_date="2026-08-21",
        year=2026,
        venue="NeurIPS",
    )
    brief = brief_for_serverchan(
        paper,
        make_review("x"),
        "https://pan.baidu.com/s/abc?pwd=k9m2",
        "k9m2",
    )
    assert "下载论文 ZIP" in brief
    assert "英文原文、中英双语 PDF 和中文导读" in brief
    assert "pan.baidu.com" in brief
    assert "k9m2" in brief


def test_serverchan_delivery_persists_share_before_marking_delivered(config, tmp_path, monkeypatch):
    config.notify_channel = "serverchan"
    config.serverchan_sendkey = "SCT123test"
    paper = Paper(
        id="doi:10.test/deliver",
        doi="10.test/deliver",
        title="Original title",
        abstract="x",
        authors=[Author("Ada")],
        publication_date="2026-08-21",
        year=2026,
        venue="NeurIPS",
    )
    sent = []

    class FakePublisher:
        def __init__(self, _config):
            pass

        def publish(self, path):
            assert path.name == "paper.zip"
            return ShareLink(
                "/PaperCollector/paper.zip",
                "https://pan.baidu.com/s/abc?pwd=k9m2",
                "k9m2",
            )

    class FakeNotifier:
        def __init__(self, sendkey):
            assert sendkey == "SCT123test"

        def send(self, title, content):
            sent.append((title, content))

    monkeypatch.setattr("paper_collector.pipeline.BaiduPCSGoPublisher", FakePublisher)
    monkeypatch.setattr("paper_collector.pipeline.ServerChanNotifier", FakeNotifier)
    package = tmp_path / "paper.zip"
    package.write_bytes(b"PK-test")
    pipeline = Pipeline(config)
    try:
        pipeline.db.upsert_paper(paper)
        review = make_review(paper.id)
        pipeline.db.save_review(paper.id, review)
        pipeline._deliver(paper, review, package)
        row = pipeline.db.work_row(paper.id)
        assert row["share_url"].startswith("https://pan.baidu.com/")
        assert row["status"] == "delivered"
        assert len(sent) == 1
    finally:
        pipeline.close()


def test_serverchan_retry_reuses_existing_share(config, tmp_path, monkeypatch):
    config.notify_channel = "serverchan"
    config.serverchan_sendkey = "SCT123test"
    paper = Paper(
        id="doi:10.test/retry",
        doi="10.test/retry",
        title="Retry paper",
        abstract="x",
        authors=[Author("Ada")],
        publication_date="2026-08-21",
        year=2026,
    )

    class MustNotPublish:
        def __init__(self, _config):
            raise AssertionError("existing share must be reused")

    class FakeNotifier:
        def __init__(self, _sendkey):
            pass

        def send(self, _title, content):
            assert "existing" in content

    monkeypatch.setattr("paper_collector.pipeline.BaiduPCSGoPublisher", MustNotPublish)
    monkeypatch.setattr("paper_collector.pipeline.ServerChanNotifier", FakeNotifier)
    pipeline = Pipeline(config)
    try:
        pipeline.db.upsert_paper(paper)
        review = make_review(paper.id)
        pipeline.db.save_review(paper.id, review)
        pipeline.db.save_share(
            paper.id,
            "/PaperCollector/existing.pdf",
            "https://pan.baidu.com/s/existing?pwd=1234",
            "1234",
        )
        pipeline._deliver(paper, review, tmp_path / "not-needed.pdf")
        assert pipeline.db.work_row(paper.id)["status"] == "delivered"
    finally:
        pipeline.close()


def test_delivery_zip_contains_original_dual_and_brief(config, tmp_path):
    paper = Paper(
        id="doi:10.test/package",
        title="Packaged Paper",
        abstract="x",
        authors=[Author("Ada")],
        publication_date="2026-08-21",
        year=2026,
    )
    original = tmp_path / "original.pdf"
    dual = tmp_path / "dual.pdf"
    brief = tmp_path / "brief.md"
    original.write_bytes(b"%PDF-original")
    dual.write_bytes(b"%PDF-dual")
    brief.write_text("summary", encoding="utf-8")
    pipeline = Pipeline(config)
    try:
        pipeline.db.upsert_paper(paper)
        package = pipeline._create_delivery_zip(paper, make_review(paper.id), original, dual, brief)
        with zipfile.ZipFile(package) as archive:
            assert set(archive.namelist()) == {"论文原文.pdf", "中英双语.pdf", "中文导读.md"}
        assert package.parent.parent.parent == config.knowledge_base_dir
        assert pipeline.db.work_row(paper.id)["package_path"] == str(package)
    finally:
        pipeline.close()


def test_queue_selection_penalizes_recently_repeated_topic(config):
    recent = Paper(
        id="recent",
        title="DDoS Testing Framework",
        abstract="x",
        authors=[],
        publication_date="2026-08-20",
        year=2026,
        matched_interests=["DDoS"],
    )
    repeated = Paper(
        id="repeated",
        title="Another DDoS Testing Framework",
        abstract="x",
        authors=[],
        publication_date="2026-08-21",
        year=2026,
        matched_interests=["DDoS"],
    )
    different = Paper(
        id="different",
        title="Tool Using Language Agents",
        abstract="x",
        authors=[],
        publication_date="2026-08-21",
        year=2026,
        matched_interests=["Agent"],
    )
    pipeline = Pipeline(config)
    try:
        for paper in (recent, repeated, different):
            pipeline.db.upsert_paper(paper)
            review = make_review(paper.id)
            review.tags = ["DDoS testing"] if paper.id != "different" else ["LLM agent"]
            pipeline.db.save_review(paper.id, review)
        pipeline.db.mark_link_delivered(recent.id)

        selected = pipeline._select_queued_delivery()

        assert selected is not None
        assert json.loads(selected["paper_json"])["id"] == different.id
    finally:
        pipeline.close()
