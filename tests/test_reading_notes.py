import copy
import json
from dataclasses import fields
from pathlib import Path

import pytest

from paper_collector.models import Paper, Review
from paper_collector.reading_note_schema import SECTION_IDS
from paper_collector.reading_notes import (
    ReadingNoteError,
    ReadingNotes,
    apply_text_repairs,
    banned_passages,
    contact_sheets,
    crop_artifacts,
    pdf_inventory,
    render_note,
    validate_note,
    validate_plan,
)

pymupdf = pytest.importorskip("pymupdf")


def test_targeted_repair_keeps_original_note_numbers_and_citations():
    note = {"paragraphs": [{"text": "这并非 10.5% 的提升。", "pages": [3], "kind": "analysis"}]}
    passages = banned_passages(note)
    edits = [{**passages[0], "replacement": "这不代表 10.5% 的提升。"}]
    repaired = apply_text_repairs(note, edits)
    assert not banned_passages(repaired)
    assert repaired["paragraphs"][0]["pages"] == [3]
    assert repaired["paragraphs"][0]["kind"] == "analysis"
    assert note["paragraphs"][0]["text"] == "这并非 10.5% 的提升。"


@pytest.mark.parametrize("replacement", ["这不代表 20% 的提升。", "这并非 10.5% 的提升。", ""])
def test_targeted_repair_rejects_changed_values_or_unfixed_style(replacement):
    note = {"text": "这并非 10.5% 的提升。"}
    with pytest.raises(ReadingNoteError):
        apply_text_repairs(note, [{**banned_passages(note)[0], "replacement": replacement}])


@pytest.fixture
def notes_inputs(config, tmp_path):
    config.repo_root = Path(__file__).parents[1]
    config.reading_notes_enabled = True
    config.reading_notes_base_url = "http://198.51.100.25:8765"
    config.reading_notes_min_chars = 1
    paper = Paper(
        id="doi:10.test/notes",
        title="Measured Evidence",
        abstract="Test paper.",
        authors=[],
        publication_date="2026-10-26",
        year=2026,
        landing_url="https://example.test/paper",
    )
    values = {field.name: 8.0 for field in fields(Review)}
    values.update(
        paper_id=paper.id,
        recommend=True,
        confidence=0.9,
        verdict="推荐",
        rejection_reason="",
        title_zh="实测证据",
        hook="实验说明适用边界。",
        summary="报告实验设置。",
        contributions=["完整记录"],
        evidence=["PDF p.1"],
        limitations=["样本有限"],
        audience="研究人员",
        why_now="最新结果",
        tags=["test"],
    )
    review = Review(**values)
    pdf = tmp_path / "paper.pdf"
    with pymupdf.open() as document:
        page = document.new_page(width=612, height=792)
        page.insert_text((72, 55), "Measured Evidence. A small controlled experiment.")
        page.draw_rect(pymupdf.Rect(80, 90, 530, 270))
        page.insert_text((90, 120), "Baseline 1.0     Method 1.2")
        page.insert_text((80, 305), "Figure 1: Measured results.")
        page.insert_text(
            (72, 380), "Figure 1 shows the observed difference in the controlled experiment."
        )
        document.save(pdf)
    workspace = tmp_path / "workspace"
    inventory, _ = pdf_inventory(pdf, workspace)
    plan = {
        "artifacts": [
            {
                "label": "Figure 1",
                "page": 1,
                "bbox": [70, 75, 550, 320],
                "role": "result",
                "caption": "Figure 1: Measured results.",
            }
        ],
        "coverage_notes": "Figure 1 ✓",
        "no_artifacts_reason": "",
    }
    paragraph = {
        "text": "实验记录了输入、设置与结果。证据范围有限，需要分别核对定义和泛化条件。",
        "pages": [1],
        "kind": "analysis",
    }
    note = {
        "paper_id": paper.id,
        "title_zh": "实测证据",
        "subtitle": "从设置解释观测差异",
        "brief": copy.deepcopy(paragraph),
        "sections": [
            {"id": identity, "title": identity, "paragraphs": [copy.deepcopy(paragraph)]}
            for identity in SECTION_IDS
        ],
        "artifacts": [
            {
                "label": "Figure 1",
                "page": 1,
                "section": "experiments",
                "crop_verified": True,
                **{
                    key: copy.deepcopy(paragraph)
                    for key in ("paper_claim", "analysis", "axes", "interpretation", "limit")
                },
            }
        ],
        "data_record": {
            "json": '{"input": "demonstration only"}',
            "reconstructed": True,
            "pages": [1],
            "fields": [{"name": "input", "description": copy.deepcopy(paragraph)}],
        },
        "evidence_synthesis": [
            {
                key: copy.deepcopy(paragraph)
                for key in ("claim", "evidence", "key_values", "boundary")
            }
        ],
        "discrepancies": [
            {
                "item": "核对范围",
                "claim": copy.deepcopy(paragraph),
                "verification": copy.deepcopy(paragraph),
            }
        ],
    }
    note["artifacts"][0]["paper_claim"]["kind"] = "claim"
    return config, paper, review, pdf, workspace, inventory, plan, note


def test_inventory_does_not_confuse_inline_figure_reference_with_caption(notes_inputs):
    *_, inventory, plan, note = notes_inputs
    assert len(inventory["pages"][0]["captions"]) == 1
    validate_plan(plan, inventory)
    validate_note(note, plan, note["paper_id"], 1, 1)


@pytest.mark.parametrize(
    "box", [[0, 0, 612, 792], [-1, 75, 550, 320], [70, 75, 71, 320], [70, 75, 550, 280]]
)
def test_invalid_or_incomplete_crops_are_rejected(notes_inputs, box):
    *_, inventory, plan, _note = notes_inputs
    plan["artifacts"][0]["bbox"] = box
    with pytest.raises(ReadingNoteError):
        validate_plan(plan, inventory)


def test_missing_figure_and_invented_caption_are_rejected(notes_inputs):
    *_, inventory, plan, _note = notes_inputs
    broken = copy.deepcopy(plan)
    broken["artifacts"] = []
    with pytest.raises(ReadingNoteError, match="覆盖不完整"):
        validate_plan(broken, inventory)
    plan["artifacts"][0]["caption"] = "Figure 1: Invented result."
    with pytest.raises(ReadingNoteError, match="caption"):
        validate_plan(plan, inventory)


@pytest.mark.parametrize(
    "defect", ["anchor", "page", "length", "banned", "crop", "record", "artifact"]
)
def test_note_validation_enforces_skill_requirements(notes_inputs, defect):
    *_, plan, note = notes_inputs
    minimum = 1
    if defect == "anchor":
        note["sections"].pop()
    elif defect == "page":
        note["brief"]["pages"] = [2]
    elif defect == "length":
        minimum = 30000
    elif defect == "banned":
        note["subtitle"] = "并非可信"
    elif defect == "crop":
        note["artifacts"][0]["crop_verified"] = False
    elif defect == "record":
        note["data_record"]["fields"] = []
    else:
        note["artifacts"] = []
    with pytest.raises(ReadingNoteError):
        validate_note(note, plan, note["paper_id"], 1, minimum)


def test_note_is_self_contained_mobile_friendly_and_escapes_untrusted_text(notes_inputs):
    _, paper, _, pdf, workspace, _, plan, note = notes_inputs
    note["title_zh"] = '<script>alert("title")</script>'
    note["brief"]["text"] = '<img src=x onerror="alert(1)"> {{SECTIONS}}'
    crops = crop_artifacts(pdf, plan, workspace)
    assert contact_sheets(crops, workspace)[0].is_file()
    document = render_note(note, plan, crops, paper)
    assert '<script>alert("title")</script>' not in document
    assert "<img src=x onerror=" not in document
    assert "&lt;script&gt;" in document
    assert "data:image/png;base64," in document
    assert document.count("<script>") == 1
    assert 'name="viewport"' in document
    assert 'id="reading-progress"' in document
    assert 'class="data-record"' in document
    assert 'class="story-chain"' in document
    assert 'class="tech-step"' in document
    assert "字段/记录为解释性重构" in document
    for identity in SECTION_IDS:
        assert f'id="{identity}"' in document
        assert f'href="#{identity}"' in document


def test_valid_note_is_cached_and_missing_public_copy_is_restored_without_model_call(notes_inputs):
    config, paper, review, pdf, _, _, plan, note = notes_inputs
    calls = []

    class FakeAgent:
        def _run(self, _prompt, schema, _output, **kwargs):
            calls.append(schema.name)
            assert kwargs["images"]
            return copy.deepcopy(plan if "plan" in schema.name else note)

    publisher = ReadingNotes(config, FakeAgent())
    artifact_dir = config.knowledge_base_dir / "papers" / "test"
    path, url = publisher.ensure(paper, review, pdf, artifact_dir)
    assert len(calls) == 2
    public = config.reading_notes_dir / "notes" / url.rsplit("/", 1)[-1]
    assert public.read_bytes() == path.read_bytes()
    public.unlink()
    config.reading_notes_base_url = "http://203.0.113.17:8765"
    restored, new_url = publisher.ensure(paper, review, pdf, artifact_dir)
    assert len(calls) == 2
    assert restored == path and public.is_file()
    assert new_url.startswith(config.reading_notes_base_url)
    assert new_url.rsplit("/", 1)[-1] == url.rsplit("/", 1)[-1]
    index = json.loads((config.reading_notes_dir / "index.json").read_text())
    assert list(index) == [paper.id]
    assert index[paper.id]["artifact_count"] == 1
    assert path.stat().st_mode & 0o777 == 0o600


def test_failed_visual_qa_never_publishes_a_note(notes_inputs):
    config, paper, review, pdf, _, _, plan, note = notes_inputs
    note["artifacts"][0]["crop_verified"] = False
    calls = []

    class BadAgent:
        def _run(self, _prompt, schema, _output, **_kwargs):
            calls.append(schema.name)
            return copy.deepcopy(plan if "plan" in schema.name else note)

    publisher = ReadingNotes(config, BadAgent())
    with pytest.raises(ReadingNoteError, match="两次"):
        publisher.ensure(paper, review, pdf, config.knowledge_base_dir / "papers" / "test")
    assert len(calls) == 4
    assert not list(config.reading_notes_dir.glob("notes/*.html"))


def test_validated_cache_survives_a_publication_failure(notes_inputs, monkeypatch):
    from paper_collector import reading_notes

    config, paper, review, pdf, _, _, plan, note = notes_inputs
    calls = []

    class FakeAgent:
        def _run(self, _prompt, schema, _output, **_kwargs):
            calls.append(schema.name)
            return copy.deepcopy(plan if "plan" in schema.name else note)

    original_write = reading_notes.atomic_write

    def fail_public_write(path, content):
        if config.reading_notes_dir in path.parents:
            raise OSError("Public directory temporarily unavailable")
        return original_write(path, content)

    publisher = ReadingNotes(config, FakeAgent())
    artifact_dir = config.knowledge_base_dir / "papers" / "test"
    monkeypatch.setattr(reading_notes, "atomic_write", fail_public_write)
    with pytest.raises(OSError, match="temporarily"):
        publisher.ensure(paper, review, pdf, artifact_dir)
    assert (artifact_dir / "reading-note" / "validated.json").is_file()
    monkeypatch.setattr(reading_notes, "atomic_write", original_write)
    publisher.ensure(paper, review, pdf, artifact_dir)
    assert len(calls) == 2


def test_failed_vps_publication_retries_from_validated_cache_without_model_calls(
    notes_inputs, monkeypatch
):
    from paper_collector.note_publishing import NotePublishError

    config, paper, review, pdf, _, _, plan, note = notes_inputs
    calls = []
    uploads = []

    class FakeAgent:
        def _run(self, _prompt, schema, _output, **_kwargs):
            calls.append(schema.name)
            return copy.deepcopy(plan if "plan" in schema.name else note)

    def upload(_config, path, filename):
        uploads.append(filename)
        assert path.is_file()
        if len(uploads) == 1:
            raise NotePublishError("VPS temporarily unavailable")

    monkeypatch.setattr("paper_collector.reading_notes.publish_remote_note", upload)
    publisher = ReadingNotes(config, FakeAgent())
    directory = config.knowledge_base_dir / "papers" / "test"
    with pytest.raises(NotePublishError):
        publisher.ensure(paper, review, pdf, directory)
    _, url = publisher.ensure(paper, review, pdf, directory)
    assert len(calls) == 2
    assert uploads[0] == uploads[1] == url.rsplit("/", 1)[-1]
