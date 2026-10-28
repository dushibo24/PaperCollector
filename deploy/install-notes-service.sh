#!/usr/bin/env bash
set -euo pipefail

export PATH="${PATH}:/usr/sbin:/sbin"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ "$EUID" -ne 0 ]]; then
    printf 'Run this installer as root.\n' >&2
    exit 1
fi
if [[ ! -x "$repo_root/.venv/bin/paper-collector" ]]; then
    printf 'Install project dependencies with uv sync --extra notes first.\n' >&2
    exit 1
fi
for command in cron flock; do
    if ! command -v "$command" >/dev/null; then
        printf 'Missing dependency: %s\n' "$command" >&2
        exit 1
    fi
done
"$repo_root/.venv/bin/python" - "$repo_root/config.toml" <<'PY'
import sys
from paper_collector.config import load_config
config = load_config(sys.argv[1])
if not config.reading_notes_enabled:
    raise SystemExit('Enable reading_notes.enabled and set public_base_url first.')
import pymupdf
PY
umask 077
mkdir -p "$repo_root/data/logs"
umask 022
mkdir -p /etc/cron.d
cat >/etc/cron.d/paper-collector-notes <<EOF
SHELL=/bin/bash
HOME=/root
PATH=/root/.local/bin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
MAILTO=""
@reboot root /bin/bash "$repo_root/deploy/run-notes-server.sh"
* * * * * root /bin/bash "$repo_root/deploy/run-notes-server.sh"
EOF
chmod 644 /etc/cron.d/paper-collector-notes
nohup /bin/bash "$repo_root/deploy/run-notes-server.sh" >/dev/null 2>&1 </dev/null &
service cron restart
"$repo_root/.venv/bin/python" - "$repo_root/config.toml" <<'PY'
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from paper_collector.config import load_config
config = load_config(sys.argv[1])
host = '127.0.0.1' if config.reading_notes_host == '0.0.0.0' else config.reading_notes_host
prefix = urlsplit(config.reading_notes_base_url).path.rstrip('/')
endpoint = f'http://{host}:{config.reading_notes_port}{prefix}/healthz'
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
for attempt in range(10):
    try:
        with opener.open(endpoint, timeout=1) as response:
            if response.status == 200 and response.headers.get('Server', '').startswith('PaperCollectorNotes/'):
                break
    except (urllib.error.URLError, TimeoutError):
        pass
    time.sleep(0.5)
else:
    raise SystemExit('Notes server failed to start. Check data/logs/notes-server.log and port conflicts.')
PY
printf 'Reading-notes server is healthy; cron checks it every minute.\n'
printf 'Log: %s/data/logs/notes-server.log\n' "$repo_root"
