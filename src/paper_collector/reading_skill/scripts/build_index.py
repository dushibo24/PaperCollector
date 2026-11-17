# -*- coding: utf-8 -*-
"""Build 阅读索引.json from the notes and audit index.html linkage.

Usage:
  python3 build_index.py --root .
  python3 build_index.py --root . --check

--check writes nothing and exits non-zero when a note has no card, a card
points to a missing note, a local PDF/crop is missing, or counts disagree.
"""
import argparse
import html
import json
import re
import sys
from datetime import date
from pathlib import Path
from urllib.parse import unquote


def strip_tags(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def first(pat, text, flags=re.S):
    m = re.search(pat, text, flags)
    return m.group(1) if m else ""


def parse_cards(index_html):
    cards = {}
    for m in re.finditer(
        r'<article class="card" data-group="([^"]+)" data-tags="([^"]+)">\s*'
        r'<h2><a href="papers/([^"]+)\.html">',
        index_html,
    ):
        cards[m.group(3)] = {"group": m.group(1), "tags": m.group(2).split()}
    return cards


def parse_primary_pdf(text):
    href = first(r'<a class="primary" href="([^"]+\.pdf)"', text)
    return unquote(href.replace("&amp;", "&"))


def parse_crop_dir(text):
    m = re.search(r'src="\.\./assets/crops/([^/]+)/', text)
    return unquote(m.group(1)) if m else ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".", help="notes root (contains papers/, index.html)")
    ap.add_argument("--out", default="阅读索引.json")
    ap.add_argument("--check", action="store_true", help="audit only; write nothing")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    index_path = root / "index.html"
    if not index_path.exists():
        sys.exit("missing index.html in " + str(root))
    index_html = index_path.read_text(encoding="utf-8")
    cards = parse_cards(index_html)

    note_paths = sorted((root / "papers").glob("*.html"))
    problems = []
    papers = []

    for path in note_paths:
        slug = path.stem
        text = path.read_text(encoding="utf-8")
        crop_dir = parse_crop_dir(text)
        crop_dir_path = root / "assets/crops" / crop_dir if crop_dir else None
        crop_count = len(list(crop_dir_path.glob("*.png"))) if crop_dir_path and crop_dir_path.exists() else 0

        pdf_href = parse_primary_pdf(text)
        pdf_rel = ""
        pdf_ok = True
        if pdf_href:
            # href is relative to papers/; resolve then relativize to the notes root.
            resolved = (path.parent / pdf_href).resolve()
            pdf_ok = resolved.exists()
            try:
                pdf_rel = str(resolved.relative_to(root))
            except ValueError:
                pdf_rel = str(resolved)
        else:
            pdf_ok = False

        if not crop_dir:
            problems.append(f"{slug}: no crop directory referenced")
        elif not crop_dir_path.exists():
            problems.append(f"{slug}: crop directory missing: {crop_dir}")
        if not pdf_ok:
            problems.append(f"{slug}: local PDF missing ({pdf_href or 'no primary link'})")
        if slug not in cards:
            problems.append(f"{slug}: note has no card in index.html")

        papers.append({
            "slug": slug,
            "title_zh": strip_tags(first(r"<h1>(.*?)</h1>", text)),
            "title_en": strip_tags(first(r'original-title">(.*?)</p>', text)),
            "venue": strip_tags(first(r'eyebrow">(.*?)</p>', text)),
            "ccf": strip_tags(first(r"<dt>CCF 等级</dt><dd>(.*?)</dd>", text)),
            "group": cards.get(slug, {}).get("group", ""),
            "tags": cards.get(slug, {}).get("tags", []),
            "note": f"papers/{slug}.html",
            "pdf": pdf_rel,
            "crops_dir": f"assets/crops/{crop_dir}" if crop_dir else "",
            "crop_count": crop_count,
            "artifact_count": text.count('class="artifact'),
            "chars": len(strip_tags(text)),
        })

    note_slugs = {p["slug"] for p in papers}
    for slug in sorted(set(cards) - note_slugs):
        problems.append(f"{slug}: card points to a missing note")

    group_order = {"经典": 0, "agent": 1, "近期": 2}
    papers.sort(key=lambda p: (group_order.get(p["group"], 9), p["slug"]))

    counts = {}
    for p in papers:
        counts[p["group"] or "(no card)"] = counts.get(p["group"] or "(no card)", 0) + 1

    stats = re.findall(r'<div class="stat"><b>(\d+)</b><span>([^<]+)</span>', index_html)
    footer = " ".join(re.findall(r"当前进度：([^<]+)", index_html))

    print(f"notes: {len(papers)}  cards: {len(cards)}  groups: {counts}")
    print(f"hero stats: {stats}")
    print(f"footer: {footer}")
    print(f"crops: {sum(p['crop_count'] for p in papers)}  artifacts: {sum(p['artifact_count'] for p in papers)}")

    if problems:
        print("\nPROBLEMS")
        for p in problems:
            print("  -", p)
    else:
        print("\nlinkage OK")

    if not args.check:
        out = root / args.out
        payload = {"generated": date.today().isoformat(), "total": len(papers), "papers": papers}
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("wrote", out)

    if args.check and problems:
        sys.exit(1)


if __name__ == "__main__":
    main()
