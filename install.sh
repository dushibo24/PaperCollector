#!/usr/bin/env bash
# PaperCollector installer for macOS and Linux.
# Usage: ./install.sh [setup options], e.g. ./install.sh --no-translation --notify none
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"
if ! command -v uv >/dev/null 2>&1; then
    printf '未找到 uv（Python 环境管理器）。现在从 https://astral.sh/uv 安装？[Y/n] '
    read -r answer || answer=n
    case "$answer" in
        [nN]*) printf '请先安装 uv 后重试：https://docs.astral.sh/uv/\n' >&2; exit 1 ;;
    esac
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi
# --inexact keeps optional extras (e.g. notes) that setup installed on a previous run.
uv sync --inexact
exec uv run paper-collector setup "$@"
