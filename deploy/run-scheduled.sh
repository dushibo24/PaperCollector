#!/usr/bin/env bash
set -euo pipefail

umask 077
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export TZ=Asia/Shanghai
export PATH="${HOME}/.local/bin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export HF_HUB_OFFLINE=1
export OMP_NUM_THREADS=4
cd "$repo_root"
mkdir -p data/logs
exec >>"data/logs/cron-$(date +%F).log" 2>&1
printf '\n[%s] Starting scheduled paper collection\n' "$(date -Is)"
exec "$repo_root/.venv/bin/paper-collector" run --config "$repo_root/config.toml" "$@"
