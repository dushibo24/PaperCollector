---
name: paper-reading-notes
description: Write source-grounded HTML reading notes for dataset/benchmark papers, with precise PDF figure/table crops, an eight-anchor narrative, a reconstructed data record, and automated validation. Use when the user asks for 论文阅读笔记 / 数据集论文拆解 / 把论文写成 HTML 笔记, or wants to continue an existing 阅读笔记 project.
---

# Paper Reading Notes

Produce one self-contained HTML note per paper plus an index card. Every number must be traceable to a PDF page; every figure/table must be a precise crop, not a full page. Never invent values, quotes, or metadata.

## Non-negotiables

1. **H1 is a literal translation of the paper title.** Put the interpretive one-liner in `.subtitle` below it.
2. **Open with `.paper-brief`**: one paragraph that says what the paper does, with the headline numbers. Model it on a research survey, not an abstract.
3. **Required chapters, fixed order** (sidebar anchors): `overview` → `writing` → `metadata` → `story` → `technical` (+ `evaluation-protocol`) → `experiments` → `source-discrepancies`. All eight anchor ids must exist.
4. **Storyline must be one continuous argument**: 背景/重要性 → 相关工作与不足 → 挑战 → 解决方式 → 贡献. Each paragraph must hand off to the next; no bullet fragments standing in for logic.
5. **Every figure/table gets**: a crop, `.axes` (what the axes/rows/columns mean), `.interpretation` (what it shows), `.limit` (what it cannot show), and the original caption in `<details class="original-caption">`.
6. **One reconstructable data record per note** in `<pre class="data-record">` as JSON, followed by field-by-field prose. Mark reconstructed fields in the source-ref line.
7. **Quote page numbers** (`PDF p.7`) in every `.source-ref`.
8. **Separate "论文主张" from "笔记分析"**. Unverified items are labelled 未报告 / 未给出.
9. **Banned AI-flavored connectives**: `而是`, `而非`, `而不是`, `并非` — count must be 0. Minimize `不是`; rewrite when possible.
10. **No full-page screenshots.** Crop to the artifact. If a box is invalid, skip loudly.

## Template

`assets/note-template.html` is a complete, annotated skeleton: every section, every required artifact sub-part, a filled data record, the evidence-synthesis table, and the discrepancy table. Copy it to `papers/<slug>.html` and replace the `{{...}}` placeholders. Keep the HTML comments while drafting; delete them before shipping.

For a filled reference, read any finished note in the target project (30k–80k characters).

## Workflow

### 0. Batch size

Work in passes of 3–5 papers. Within a pass: read all PDFs, write all crop plans, crop all, QA all contact sheets, then write HTML one by one. Writing 25 notes in one uninterrupted run degrades crop precision and storyline continuity.

### 1. Locate inputs

Typical layout:

```
papers/                     # PDFs (one level above the notes root)
阅读笔记/
  index.html
  README.md
  assets/style.css
  assets/app.js
  assets/crops/<paper-slug>/
  papers/<slug>.html
  .tools/crop_plan.json
  .tools/crop_ledger.json
```

`<paper-slug>` is the PDF filename without extension (keep spaces and punctuation; URL-encode in HTML).

### 2. Read the PDF structurally

Use PyMuPDF. Dump text blocks with coordinates and image/drawing boxes; search captions with `page.search_for("Table 3")`. Read the full text once before choosing crops. Record: venue, pages, authors, affiliations, dataset sizes, every table/figure number.

### 2.5 Inventory artifacts before planning crops

```bash
python3 scripts/find_artifacts.py <pdf> --pages 1-12 --json .tools/artifacts.json
```

It prints, per page: caption blocks (Figure/Table/Listing/Algorithm/Box) with bboxes, embedded raster bboxes, and merged vector-drawing clusters (a rough figure extent), plus the page size. Use it to build the crop list from the actual layout instead of guessing. Remember that a caption block sits *below* its figure: the drawing cluster above it is the figure body.

Coverage rule: every numbered Figure and Table in the main text gets a crop. Appendix tables that carry unique numbers (ablations, per-category results) get one too; stop when a table repeats numbers already shown. Post a coverage checklist (`Figure 1–10 ✓, Table 1–7 ✓`) before writing.

### 3. Write the crop plan

Add an entry to `.tools/crop_plan.json`:

```json
"<paper-slug>": [
  ["Figure 1", 2, [60, 48, 552, 263], "figure"],
  ["Table 3", 5, [48, 188, 300, 315], "table"]
]
```

Coordinates are PDF points: `[x0, y0, x1, y1]`, origin top-left. Include the caption when it belongs to the artifact; leave out surrounding body text. Check by rendering and looking.

### 4. Crop and QA

```bash
python3 scripts/crop.py --root <notes-root> --lib <pdf-dir> --plan .tools/crop_plan.json --slug "<paper-slug>"
python3 scripts/contact_sheet.py --root <notes-root> --slug "<paper-slug>" --out .tools
```

Then inspect the contact sheet, and open any suspicious crop individually. Fix clipped captions, table rules, or partial headers by adjusting the rectangle and re-running. The ledger records page, box, pixel size, and SHA-256.

### 5. Write the HTML

Copy `assets/note-template.html` from this skill into `papers/<slug>.html`, then fill it. Keep the shared stylesheet/script paths (`../assets/...`). Use:

- `.story-chain` for the compressed argument steps (one `<span>` per step).
- `.tech-step` for each technical move; start with a plain-language sentence before the artifact.
- `.artifact.evidence-figure` with `data-label` and `data-role` (`method`, `data`, `result`, `analysis`).
- `.evidence-synthesis` table at the end of experiments: 主张 / 证据 / 关键数值 / 边界.
- `.source-discrepancies` table: 项目 / 论文表述 / 核对结果与边界.

For the data record, prefer a real example printed in the paper. Reconstruct the field names only when the paper does not print JSON, and say so.

### 6. Validate

```bash
python3 scripts/validate_note.py papers/<slug>.html   # one note
python3 scripts/validate_all.py --root .              # every note + index linkage
```

`validate_note.py` checks: missing local links/anchors, banned connectives, tag balance, artifact/crop counts, character count. `validate_all.py` additionally checks the required anchors, the reading-progress element, the data record, and runs the index audit. Fix everything before indexing.

### 7. Update the index

Cards in `index.html` and the JSON index must agree with the notes. Generate and audit in one step:

```bash
python3 scripts/build_index.py --root .          # writes 阅读索引.json
python3 scripts/build_index.py --root . --check  # audit only; non-zero on problems
```

It parses note TOCs, cards (`data-group`, `data-tags`), primary PDF link, crop directory, crop count, artifact count; reports note↔card↔PDF mismatches; prints hero/footer counts so you can bump them. Then update the two hero stats and the footer line in `index.html`, and the progress section in `README.md`.

### 8. Done definition

A note is done only when all of these hold:

- H1 is the literal translation; `.subtitle` carries the interpretive line; `.paper-brief` has the headline numbers.
- All eight anchors exist and the sidebar matches.
- Every numbered main-text Figure/Table has a precise crop and the five-part explanation.
- One data record exists with field-by-field prose and reconstruction notes.
- `validate_note.py` exits 0; `validate_all.py` exits 0.
- The card exists, the PDF and crop directory resolve, and counts match `阅读索引.json`.

A batch is done when hero stats, footer, README counts, cards, and JSON all agree.

## Crop QA heuristics

- Width < 35 pt or height < 15 pt → skip.
- Area > 92% of the page → skip (that is a full-page screenshot).
- A table crop must include the full top rule and the bottom rule; a figure crop must include the caption line if it starts within the box.
- Render at 3.2× (`fitz.Matrix(3.2, 3.2)`) for legibility.
- Contact sheet is 3 columns; thumbnails 470 px. Inspect at full size before shipping.

### Crop failure catalog

Check the contact sheet for these, then reopen the suspect crop at full size:

| Symptom | Cause | Fix |
| --- | --- | --- |
| Top row/header missing | y0 set at the caption, not the table top | move y0 up to the top rule |
| Nodes/title clipped at top | figure body starts above the caption block | use the drawing cluster or image bbox from `find_artifacts.py` |
| Body text or section heading inside | y1 extended past the artifact | tighten y1 to the bottom rule |
| Large blank band | caption and body separated by whitespace | set y0 to just above the first content |
| Neighboring column bleeding in | two-column layout, box crosses the gutter | clamp x1/x0 to the column boundary (~300 pt on A4 two-column) |
| Stray sentence at top/bottom | paragraph between table and caption | trim to the rule, not the text block |
| Table split across pages | long table continues | crop the page that holds the header, note continuation in `.limit` |

After any fix, re-run `crop.py` for that slug and regenerate the contact sheet. Never ship a crop you have not seen at full size.

## Prose rules

- Lead with the concrete number or mechanism, then interpret.
- One idea per paragraph; the first sentence carries it.
- Prefer short declaratives. Avoid rhetorical scaffolding ("值得注意的是", "不难发现").
- When the paper is weak, say where: no human upper bound, no variance, oracle leakage, tiny model set, label ambiguity, contamination. Put these in `.limit` and in the discrepancy table.
- Do not apologize for the paper or pad the note. A note is 30k–80k characters; density matters more than length.

## Dependencies

`PyMuPDF` (`fitz`) and `Pillow`. Both are already installed on this machine.
