from types import SimpleNamespace

from paper_collector.reading_notes import (
    inline_skill_document,
    skill_title,
    skill_validation_problems,
    skill_visible_text,
)

SLUG = "demo-paper"
ANCHORS = [
    "overview",
    "writing",
    "metadata",
    "story",
    "technical",
    "evaluation-protocol",
    "experiments",
    "source-discrepancies",
]


def build_workspace(tmp_path):
    workspace = tmp_path / "ws"
    (workspace / "papers").mkdir(parents=True)
    (workspace / "assets" / "crops" / SLUG).mkdir(parents=True)
    (workspace / "scripts").mkdir()
    (workspace / "assets" / "style.css").write_text("body{color:#111}", encoding="utf-8")
    (workspace / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    (workspace / "assets" / "crops" / SLUG / "fig.png").write_bytes(b"png-bytes")
    (workspace / "papers" / f"{SLUG}.pdf").write_bytes(b"pdf")
    (workspace / "scripts" / "validate_note.py").write_text(
        "import sys\nsys.exit(0)\n", encoding="utf-8"
    )
    sections = "".join(
        f'<section id="{anchor}"><p>{anchor} 内容足够长，包含具体数字和解释。</p></section>'
        for anchor in ANCHORS
    )
    document = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        '<link rel="stylesheet" href="../assets/style.css">'
        '<script defer src="../assets/app.js"></script></head><body>'
        '<progress id="reading-progress"></progress>'
        "<h1>测试笔记标题</h1>"
        f"{sections}"
        '<article class="artifact"><a class="chart-image cropped-image" href="../assets/crops/'
        f'{SLUG}/fig.png"><img src="../assets/crops/{SLUG}/fig.png" alt="图"></a></article>'
        '<div class="gallery"><a href="../assets/crops/'
        f'{SLUG}/fig.png"><img src="../assets/crops/{SLUG}/fig.png" alt="图">'
        "<span>Figure 1 · p1</span></a></div>"
        '<pre class="data-record">{}</pre>'
        f'<a href="./{SLUG}.pdf#page=2">p2</a>'
        '<a href="../index.html">index</a>'
        "<p>正文" + "解释" * 2000 + "</p>"
        "</body></html>"
    )
    note = workspace / "papers" / f"{SLUG}.html"
    note.write_text(document, encoding="utf-8")
    return workspace, document


def test_inline_skill_document_makes_html_self_contained(tmp_path):
    workspace, document = build_workspace(tmp_path)
    paper = SimpleNamespace(pdf_url="https://example.com/paper.pdf")

    result, title, artifacts = inline_skill_document(document, workspace, SLUG, paper)

    assert title == "测试笔记标题"
    assert artifacts == 1
    assert "body{color:#111}" in result
    assert "console.log(1)" in result
    assert "data:image/png;base64," in result
    assert "assets/crops" not in result
    assert 'class="chart-image cropped-image"' in result
    assert "Figure 1 · p1" in result
    assert 'href="https://example.com/paper.pdf#page=2"' in result
    assert 'href="../index.html"' not in result
    assert "{{" not in result


def test_skill_validation_accepts_complete_note(tmp_path):
    workspace, document = build_workspace(tmp_path)

    assert skill_title(document) == "测试笔记标题"
    assert len(skill_visible_text(document)) > 1000
    assert skill_validation_problems(document, workspace, SLUG, 1000) == []


def test_skill_validation_reports_missing_anchor_and_banned_word(tmp_path):
    workspace, document = build_workspace(tmp_path)
    broken = document.replace('id="story"', 'id="other"').replace("具体数字", "而是数字")

    problems = skill_validation_problems(broken, workspace, SLUG, 1000)

    assert any("#story" in problem for problem in problems)
    assert any("而是" in problem for problem in problems)
