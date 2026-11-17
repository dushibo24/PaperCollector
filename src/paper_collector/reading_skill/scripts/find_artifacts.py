# -*- coding: utf-8 -*-
"""Inventory figures, tables and vector clusters before planning crops.

Prints, per page:
  - caption blocks (Figure/Table/Listing/Algorithm/Box) with bboxes
  - embedded raster image bboxes
  - clusters of vector drawing rectangles (a rough figure extent)
  - the page size and the two-column split, when detectable

Usage:
  python3 find_artifacts.py paper.pdf --pages 1-12
  python3 find_artifacts.py paper.pdf --pages 5,9,31-33 --json /tmp/artifacts.json
"""
import argparse
import json
import re

import fitz

CAPTION_RE = re.compile(
    r"^\s*(Figure|Fig\.?|Table|Listing|Algorithm|Box)\s*([0-9]+[A-Za-z]?)\b"
)


def parse_pages(spec, total):
    if not spec:
        return list(range(1, total + 1))
    pages = set()
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            a, b = chunk.split("-", 1)
            pages.update(range(int(a), int(b) + 1))
        else:
            pages.add(int(chunk))
    return sorted(p for p in pages if 1 <= p <= total)


def rect_list(r):
    return [round(r.x0, 1), round(r.y0, 1), round(r.x1, 1), round(r.y1, 1)]


def drawing_clusters(page, gap=12.0):
    rects = []
    for d in page.get_drawings():
        r = d["rect"]
        if r.is_empty:
            continue
        if r.width < 1.5 and r.height < 1.5:
            continue
        rects.append(fitz.Rect(r))
    if not rects:
        return []

    clusters = []
    for r in sorted(rects, key=lambda x: (x.y0, x.x0)):
        if clusters and r.y0 <= clusters[-1]["rect"].y1 + gap:
            c = clusters[-1]
            c["rect"] |= r
            c["count"] += 1
        else:
            clusters.append({"rect": fitz.Rect(r), "count": 1})

    # Drop single hairline rules; keep clusters with real extent.
    out = []
    for c in clusters:
        r = c["rect"]
        if c["count"] < 2 or (r.width < 4 and r.height < 4):
            continue
        out.append({
            "bbox": rect_list(r),
            "count": c["count"],
            "page_area_ratio": round((r.width * r.height) / (page.rect.width * page.rect.height), 3),
        })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--pages", help="1-based page range, e.g. 1-12,15,31-33")
    ap.add_argument("--json", help="write the inventory to this JSON file")
    args = ap.parse_args()

    doc = fitz.open(args.pdf)
    pages = parse_pages(args.pages, len(doc))
    report = {"pdf": args.pdf, "pages": len(doc), "page_size": rect_list(doc[0].rect), "items": []}

    for pn in pages:
        page = doc[pn - 1]
        captions = []
        for b in page.get_text("blocks"):
            text = b[4].strip()
            m = CAPTION_RE.match(text)
            if m:
                captions.append({
                    "kind": m.group(1).lower().rstrip("."),
                    "number": m.group(2),
                    "bbox": [round(v, 1) for v in b[:4]],
                    "text": " ".join(text.split())[:180],
                })

        images = []
        for im in page.get_image_info():
            r = fitz.Rect(im["bbox"])
            if r.width >= 8 and r.height >= 8:
                images.append({"bbox": rect_list(r), "pixels": [im.get("width"), im.get("height")]})

        item = {
            "page": pn,
            "captions": captions,
            "images": images,
            "drawing_clusters": drawing_clusters(page),
        }
        report["items"].append(item)

        print(f"\n===== PAGE {pn} =====")
        if not captions:
            print("  captions: none")
        for c in captions:
            print(f"  {c['kind']} {c['number']:<3} {c['bbox']}  {c['text'][:110]}")
        if images:
            print(f"  images: {len(images)}")
            for im in images[:12]:
                print(f"    {im['bbox']}  pixels={im['pixels']}")
        if item["drawing_clusters"]:
            print(f"  drawing clusters: {len(item['drawing_clusters'])}")
            for c in item["drawing_clusters"][:10]:
                print(f"    {c['bbox']}  n={c['count']}  area={c['page_area_ratio']}")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
        print("\nwrote", args.json)


if __name__ == "__main__":
    main()
