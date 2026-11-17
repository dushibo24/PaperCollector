# -*- coding: utf-8 -*-
"""Validate every note in a notes project, plus index linkage.

Runs validate_note.py on each papers/*.html, checks that the required
anchors and reading-progress element are present, then audits
note <-> index.html <-> PDF <-> crop-directory linkage with
build_index.py --check.

Usage:
  python3 validate_all.py --root .
  python3 validate_all.py --root . --quiet     # only problems
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
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


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".", help="notes root (contains papers/, index.html)")
    ap.add_argument("--quiet", action="store_true", help="print only failures")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    notes = sorted((root / "papers").glob("*.html"))
    if not notes:
        sys.exit("no notes found under " + str(root / "papers"))

    failures = []
    for note in notes:
        slug = note.stem
        text = note.read_text(encoding="utf-8")

        res = run([sys.executable, str(HERE / "validate_note.py"), str(note)])
        problems = []
        if res.returncode != 0:
            for line in res.stdout.splitlines():
                m = re.match(r"^missing (.*)$", line)
                if m and m.group(1) != "[]":
                    problems.append(line)
                    continue
                m = re.match(r"^(而是|而非|而不是|并非) (\d+)$", line)
                if m and int(m.group(2)):
                    problems.append(line)
                    continue
                m = re.match(r"^(\w+) (\d+) (\d+)$", line)
                if m and m.group(2) != m.group(3):
                    problems.append(f"unbalanced <{m.group(1)}>")

        for anchor in ANCHORS:
            if f'id="{anchor}"' not in text:
                problems.append(f'missing anchor #{anchor}')
        if 'id="reading-progress"' not in text:
            problems.append("missing reading-progress element")
        if text.count('class="data-record"') < 1:
            problems.append("no data-record")

        if problems:
            failures.append((slug, problems))
            print(f"FAIL {slug}")
            for p in problems:
                print("  -", p)
        elif not args.quiet:
            print(f"ok   {slug}")

    idx = run([sys.executable, str(HERE / "build_index.py"), "--root", str(root), "--check"])
    if idx.returncode != 0:
        print("FAIL index linkage")
        for line in idx.stdout.splitlines():
            if line.startswith(("  -", "PROBLEMS")) or "missing" in line:
                print(line)
        failures.append(("index", ["linkage audit failed"]))

    print(f"\nnotes: {len(notes)}  failures: {len(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
