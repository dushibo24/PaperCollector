#!/usr/bin/env bash
set -euo pipefail

umask 077
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="${HOME}/.local/bin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
cd "$repo_root"
mkdir -p data/logs
exec 9>data/notes-server.lock
flock -n 9 || exit 0
exec >>data/logs/notes-server.log 2>&1
printf '\n[%s] Starting reading-notes HTTP server\n' "$(date -Is)"
exec "$repo_root/.venv/bin/paper-collector" serve-notes --config "$repo_root/config.toml"
