#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
uv tool install --python python3 --constraint "$repo_root/deploy/pdf2zh-constraints.txt" pdf2zh==1.9.11
tool_root="$(uv tool dir)"
PYTHONPATH="$repo_root/src${PYTHONPATH:+:$PYTHONPATH}" \
    "$tool_root/pdf2zh/bin/python" -m paper_collector.font_repair
