import json
import zipfile

from test_db import make_review

from paper_collector.models import Author, Paper
from paper_collector.pipeline import Pipeline, brief_for_serverchan, render_brief
from paper_collector.publishers import ShareLink


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
