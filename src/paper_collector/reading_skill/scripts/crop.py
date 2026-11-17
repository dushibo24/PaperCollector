# -*- coding: utf-8 -*-
"""Precise figure/table crops from native PDF coordinates.

Each plan entry: [label, page_number, [x0, y0, x1, y1], kind]
Coordinates are PDF points, origin top-left, clipped to the page.
Full-page and degenerate boxes are skipped loudly.
"""
import argparse
import hashlib
import json
from pathlib import Path

import fitz


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True, help="notes root (contains assets/, papers/)")
    ap.add_argument("--lib", required=True, help="directory containing the PDFs")
    ap.add_argument("--plan", required=True, help="crop_plan.json")
    ap.add_argument("--out", default="assets/crops")
    ap.add_argument("--slug", help="only this paper slug")
    ap.add_argument("--zoom", type=float, default=3.2)
    args = ap.parse_args()

    root = Path(args.root).resolve()
    lib = Path(args.lib).resolve()
    plan = json.loads(Path(args.plan).read_text())
    out_root = root / args.out
    out_root.mkdir(parents=True, exist_ok=True)

    made = []
    for slug, items in plan.items():
        if args.slug and slug != args.slug:
            continue
        matches = sorted(lib.glob(slug + "*.pdf"))
        if not matches:
            print("MISSING PDF", slug)
            continue
        doc = fitz.open(matches[0])
        outdir = out_root / slug
        outdir.mkdir(parents=True, exist_ok=True)
        for label, pn, box, kind in items:
            page = doc[pn - 1]
            rect = fitz.Rect(box) & page.rect
            if rect.is_empty or rect.width < 35 or rect.height < 15:
                print("SKIP invalid", slug, label, pn)
                continue
            if (rect.width * rect.height) / (page.rect.width * page.rect.height) > 0.92:
                print("SKIP near-full-page", slug, label, pn)
                continue
            pix = page.get_pixmap(matrix=fitz.Matrix(args.zoom, args.zoom), clip=rect, alpha=False)
            name = label.lower().replace(" ", "-").replace(".", "") + ".png"
            dest = outdir / name
            pix.save(dest)
            made.append({
                "paper": slug,
                "label": label,
                "kind": kind,
                "page": pn,
                "box": [round(v, 2) for v in rect],
                "image": str(dest.relative_to(root)),
                "pixels": [pix.width, pix.height],
                "sha256": hashlib.sha256(dest.read_bytes()).hexdigest(),
            })
        doc.close()

    ledger_path = root / ".tools" / "crop_ledger.json"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    old = {}
    if ledger_path.exists():
        old = {r["paper"] + "|" + r["label"]: r for r in json.loads(ledger_path.read_text())}
    for r in made:
        old[r["paper"] + "|" + r["label"]] = r
    ledger_path.write_text(json.dumps(list(old.values()), ensure_ascii=False, indent=2) + "\n")

    print("cropped", len(made))
    for r in made:
        print(f'  {r["label"]:<10} p{r["page"]:<3} {r["pixels"][0]}x{r["pixels"][1]}  {r["image"]}')


if __name__ == "__main__":
    main()
