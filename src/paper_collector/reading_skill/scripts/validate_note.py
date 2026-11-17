# -*- coding: utf-8 -*-
"""Validate one HTML reading note before indexing.

Checks missing local links and anchors, banned connectives, tag balance,
crop/artifact counts, and size.
"""
import re
import sys
import urllib.parse
from pathlib import Path

BANNED = ["而是", "而非", "而不是", "并非"]
TAGS = ["section", "article", "figure", "div", "dl", "details", "pre", "table"]


def main(path):
    f = Path(path).resolve()
    s = f.read_text(encoding="utf-8")
    missing = []

    for m in re.finditer(r'(?:href|src)="([^"]+)"', s):
        u = m.group(1)
        if u.startswith(("http", "mailto:", "#")):
            continue
        target = (f.parent / urllib.parse.unquote(u.split("#")[0])).resolve()
        if not target.exists():
            missing.append(u)

    ids = set(re.findall(r'id="([^"]+)"', s))
    for m in re.finditer(r'href="#([^"]+)"', s):
        if m.group(1) not in ids:
            missing.append("#" + m.group(1))

    print("missing", missing)
    for pat in BANNED:
        print(pat, s.count(pat))
    print("不是 count", s.count("不是"))

    for tag in TAGS:
        print(tag, s.count("<" + tag), s.count("</" + tag + ">"))

    print("artifact", s.count('class="artifact'))
    print("figures", s.count('class="chart-image'))
    print("data-records", s.count('class="data-record"'))
    print("source-refs", s.count('class="source-ref"'))
    print("chars", len(s))

    problems = bool(missing) or any(s.count(p) for p in BANNED)
    problems = problems or any(s.count("<" + t) != s.count("</" + t + ">") for t in TAGS)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
