from dataclasses import fields

from paper_collector.db import StateDB
from paper_collector.models import Author, Paper, Review


def make_paper(paper_id: str = "doi:10.test/x") -> Paper:
    return Paper(
        id=paper_id,
        doi="10.test/x",
        title="A paper",
        abstract="An abstract",
        authors=[Author("Ada")],
        publication_date="2026-08-21",
        year=2026,
    )


def make_review(paper_id: str) -> Review:
    values = {
        "paper_id": paper_id,
        "recommend": True,
        "confidence": 0.9,
        "overall_score": 8.0,
        "relevance_score": 8.0,
        "content_quality_score": 8.0,
        "novelty_score": 8.0,
        "impact_score": 7.0,
        "timeliness_score": 9.0,
        "evidence_quality_score": 8.0,
        "verdict": "值得推荐",
        "rejection_reason": "",
        "title_zh": "一篇论文",
        "hook": "值得看",
        "summary": "摘要",
        "contributions": ["贡献"],
        "evidence": ["证据 [p.1]"],
        "limitations": ["限制"],
        "audience": "研究者",
        "why_now": "很新",
        "tags": ["RAG"],
    }
    assert set(values) == {field.name for field in fields(Review)}
    return Review(**values)


def test_doi_upsert_deduplicates_and_preserves_primary_id(tmp_path):
    db = StateDB(tmp_path / "state.sqlite3")
    try:
        assert db.upsert_paper(make_paper("arxiv:123")) is True
        duplicate = make_paper("doi:10.test/x")
        assert db.upsert_paper(duplicate) is False
        assert duplicate.id == "arxiv:123"
        assert len(db.eligible_papers()) == 1
    finally:
        db.close()


def test_reading_note_fields_survive_reopening_database_without_changing_delivery_state(tmp_path):
    path = tmp_path / "state.sqlite3"
    db = StateDB(path)
    paper = make_paper()
    db.upsert_paper(paper)
    db.save_review(paper.id, make_review(paper.id))
    db.mark_link_delivered(paper.id)
    note_path = tmp_path / "reading-note.html"
    db.save_reading_note(paper.id, note_path, "http://198.51.100.25:8765/notes/test.html")
    db.close()
    db = StateDB(path)
    try:
        row = db.work_row(paper.id)
        assert row["reading_note_path"] == str(note_path)
        assert row["reading_note_url"].endswith("test.html")
        assert row["status"] == "delivered"
        assert row["file_delivered_at"] is not None
    finally:
        db.close()


def test_completed_translation_is_resumed_after_note_generation_failure(tmp_path):
    db = StateDB(tmp_path / "state.sqlite3")
    paper = make_paper()
    try:
        db.upsert_paper(paper)
        db.save_review(paper.id, make_review(paper.id))
        db.save_artifacts(paper.id, dual_pdf=tmp_path / "dual.pdf")
        db.set_status(paper.id, "failed", "note JSON invalid")
        assert db.inflight_delivery()["id"] == paper.id
        db.mark_link_delivered(paper.id)
        assert db.inflight_delivery() is None
    finally:
        db.close()


def test_active_run_records_selected_paper_before_delivery_finishes(tmp_path):
    db = StateDB(tmp_path / "state.sqlite3")
    paper = make_paper()
    try:
        db.upsert_paper(paper)
        run_id = db.start_run()
        db.select_run_paper(run_id, paper.id)
        row = db.connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        assert row["selected_work_id"] == paper.id
        assert row["status"] == "running"
        assert row["finished_at"] is None
    finally:
        db.close()


def test_same_title_is_deduplicated_across_sources(tmp_path):
    db = StateDB(tmp_path / "state.sqlite3")
    try:
        first = make_paper("arxiv:123")
        first.doi = None
        db.upsert_paper(first)
        duplicate = make_paper("title:other")
        duplicate.doi = None
        duplicate.title = "A PAPER!!!"
        assert db.upsert_paper(duplicate) is False
        assert duplicate.id == "arxiv:123"
        assert len(db.eligible_papers()) == 1
    finally:
        db.close()


def test_api_daily_quota_is_persisted_and_resets_on_a_new_date(tmp_path):
    path = tmp_path / "state.sqlite3"
    db = StateDB(path)
    try:
        assert db.reserve_api_request("semantic_scholar", 2, "2026-08-25") is True
        assert db.reserve_api_request("semantic_scholar", 2, "2026-08-25") is True
        assert db.reserve_api_request("semantic_scholar", 2, "2026-08-25") is False
        assert db.api_request_count("semantic_scholar", "2026-08-25") == 2
    finally:
        db.close()

    reopened = StateDB(path)
    try:
        assert reopened.reserve_api_request("semantic_scholar", 2, "2026-08-25") is False
        assert reopened.reserve_api_request("semantic_scholar", 2, "2026-08-26") is True
        assert reopened.api_request_count("semantic_scholar", "2026-08-26") == 1
    finally:
        reopened.close()


def test_partial_delivery_is_resumable_without_duplicate_summary(tmp_path):
    db = StateDB(tmp_path / "state.sqlite3")
    try:
        paper = make_paper()
        db.upsert_paper(paper)
        db.save_review(paper.id, make_review(paper.id))
        assert db.pending_delivery()["summary_delivered_at"] is None
        db.mark_summary_delivered(paper.id)
        assert db.pending_delivery()["summary_delivered_at"] is not None
        db.mark_file_delivered(paper.id)
        assert db.pending_delivery() is None
        assert db.stats()["delivered"] == 1
    finally:
        db.close()


def test_baidupan_share_is_persisted_and_link_delivery_is_atomic(tmp_path):
    db = StateDB(tmp_path / "state.sqlite3")
    try:
        paper = make_paper()
        db.upsert_paper(paper)
        db.save_review(paper.id, make_review(paper.id))
        db.save_share(
            paper.id,
            "/PaperCollector/paper.pdf",
            "https://pan.baidu.com/s/abc?pwd=1234",
            "1234",
        )
        row = db.pending_delivery()
        assert row["share_password"] == "1234"
        db.mark_link_delivered(paper.id)
        row = db.work_row(paper.id)
        assert row["summary_delivered_at"] is not None
        assert row["file_delivered_at"] is not None
        assert row["status"] == "delivered"
    finally:
        db.close()


def test_generated_package_is_resumed_before_other_queue_items(tmp_path):
    db = StateDB(tmp_path / "state.sqlite3")
    try:
        first = make_paper("doi:10.test/first")
        first.doi = "10.test/first"
        first.title = "First paper"
        second = make_paper("doi:10.test/second")
        second.doi = "10.test/second"
        second.title = "Second paper"
        for paper in (first, second):
            db.upsert_paper(paper)
            db.save_review(paper.id, make_review(paper.id))
        package = tmp_path / "first.zip"
        package.write_bytes(b"PK-test")
        db.save_artifacts(first.id, package_path=package)
        db.set_status(first.id, "failed", "temporary notification failure")

        row = db.inflight_delivery()

        assert row is not None
        assert row["id"] == first.id
    finally:
        db.close()


def test_eligible_pool_can_be_read_without_discovery_order_truncation(tmp_path):
    db = StateDB(tmp_path / "pool.sqlite3")
    try:
        for index in range(205):
            paper = make_paper(f"paper-{index}")
            paper.doi = None
            paper.title = f"Distinct paper {index}"
            db.upsert_paper(paper)
        assert len(db.eligible_papers()) == 200
        assert len(db.eligible_papers(limit=None)) == 205
    finally:
        db.close()


def test_delivery_cooldown_is_persistent_and_does_not_reject_paper(tmp_path):
    path = tmp_path / "state.sqlite3"
    db = StateDB(path)
    paper = make_paper()
    db.upsert_paper(paper)
    db.save_review(paper.id, make_review(paper.id))
    db.save_artifacts(paper.id, dual_pdf=tmp_path / "dual.pdf")
    db.defer_delivery(paper.id, "translation timeout")
    assert db.inflight_delivery() is None
    assert db.queued_deliveries() == []
    db.close()
    db = StateDB(path)
    try:
        row = db.work_row(paper.id)
        assert row["review_json"] and row["rejected_reason"] is None
        assert row["delivery_failures"] == 1
        assert row["dual_pdf"].endswith("dual.pdf")
        db.connection.execute("UPDATE works SET delivery_retry_after='2000-01-01T00:00:00+00:00'")
        db.connection.commit()
        assert db.inflight_delivery()["id"] == paper.id
    finally:
        db.close()


def test_daily_delivery_guard_uses_beijing_midnight_not_utc(tmp_path):
    from datetime import date

    db = StateDB(tmp_path / "day.sqlite3")
    paper = make_paper()
    db.upsert_paper(paper)
    try:
        db.connection.execute("UPDATE works SET file_delivered_at='2026-11-10T16:05:00+00:00'")
        db.connection.commit()
        assert db.delivered_on(date(2026, 11, 11))
        assert not db.delivered_on(date(2026, 11, 10))
        assert not db.delivered_on(date(2026, 11, 12))
    finally:
        db.close()


def test_failed_download_is_not_immediately_selected_again(tmp_path):
    db = StateDB(tmp_path / "failed-download.sqlite3")
    paper = make_paper()
    db.upsert_paper(paper)
    try:
        db.defer_delivery(paper.id, "PDF source returned HTTP 403")
        assert not db.eligible_papers(limit=None)
        assert db.work_row(paper.id)["rejected_reason"] is None
    finally:
        db.close()
