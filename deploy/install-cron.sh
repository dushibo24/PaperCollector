#!/usr/bin/env bash
set -euo pipefail

export PATH="${PATH}:/usr/sbin:/sbin"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ "$EUID" -ne 0 ]]; then
    printf 'Run this installer as root.\n' >&2
    exit 1
fi
if [[ "$(date +%z)" != +0800 ]]; then
    printf 'The server timezone must be UTC+08:00 for the Beijing-time schedule.\n' >&2
    exit 1
fi
if [[ ! -x "$repo_root/.venv/bin/paper-collector" ]]; then
    printf 'Install project dependencies with uv sync first.\n' >&2
    exit 1
fi
if ! command -v cron >/dev/null; then
    printf 'Install cron first: apt-get install cron\n' >&2
    exit 1
fi

umask 022
mkdir -p /etc/cron.d
cat >/etc/cron.d/paper-collector <<EOF
SHELL=/bin/bash
HOME=/root
PATH=/root/.local/bin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
MAILTO=""
0 9 * * 2,4,5 root /bin/bash "$repo_root/deploy/run-scheduled.sh"
EOF
chmod 644 /etc/cron.d/paper-collector
service cron start
printf 'Installed: Tuesday, Thursday, Friday at 09:00 (Asia/Shanghai).\n'
