# -*- coding: utf-8 -*-
"""Build a labeled contact sheet of every crop for one paper (visual QA)."""
import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True, help="notes root")
    ap.add_argument("--slug", required=True)
    ap.add_argument("--out", default=".tools")
    ap.add_argument("--cols", type=int, default=3)
    ap.add_argument("--thumb", type=int, default=470)
    args = ap.parse_args()

    crop_dir = Path(args.root).resolve() / "assets/crops" / args.slug
    files = sorted(crop_dir.glob("*.png"))
    if not files:
        raise SystemExit("no crops for " + args.slug)

    font = ImageFont.load_default()
    cells = []
    for f in files:
        im = Image.open(f).convert("RGB")
        im.thumbnail((args.thumb, args.thumb * 2))
        cells.append((f.stem, im))

    rows = (len(cells) + args.cols - 1) // args.cols
    row_h = max(im.height for _, im in cells) + 34
    width = args.cols * (args.thumb + 20) + 20
    height = rows * row_h + 20
    sheet = Image.new("RGB", (width, height), "white")
    d = ImageDraw.Draw(sheet)
    for i, (name, im) in enumerate(cells):
        c, r = i % args.cols, i // args.cols
        x = 20 + c * (args.thumb + 20)
        y = 20 + r * row_h
        d.text((x, y), name, fill="black", font=font)
        sheet.paste(im, (x, y + 18))

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    dest = out / (args.slug.replace(" ", "_") + "_contact.png")
    sheet.save(dest)
    print(dest, sheet.size)


if __name__ == "__main__":
    main()
