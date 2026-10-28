#!/usr/bin/env bash
set -euo pipefail

export PATH="${PATH}:/usr/local/bin:/usr/sbin:/sbin"
if [[ "$EUID" -ne 0 ]]; then
    printf 'Run on the VPS as root.\n' >&2
    exit 1
fi
server_source="${1:?Usage: bash install-vps-static.sh /path/to/notes_http.py [port]}"
port="${2:-8765}"
python_bin="$(command -v python3)"
root="${NOTES_STATIC_ROOT:-/root/paper-collector-notes}"
owner="${NOTES_STATIC_USER:-root}"
bind="${NOTES_STATIC_BIND:-0.0.0.0}"
prefix="${NOTES_STATIC_PREFIX:-}"
"$python_bin" - "$server_source" "$port" "$root" "$owner" "$bind" "$prefix" <<'PY'
import sys
import pwd
import re
from pathlib import Path
if sys.version_info < (3, 8):
    raise SystemExit('The static server requires Python 3.8 or newer.')
if not sys.argv[2].isdigit() or not 1 <= int(sys.argv[2]) <= 65535:
    raise SystemExit('Invalid HTTP port.')
root, owner, bind, prefix = sys.argv[3:]
if not re.fullmatch(r'/[A-Za-z0-9_./-]+', root) or '..' in Path(root).parts or not root.strip('/'):
    raise SystemExit('Invalid static service directory.')
if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]*', owner):
    raise SystemExit('Invalid static service account.')
pwd.getpwnam(owner)
if bind not in ('127.0.0.1', '0.0.0.0') or not re.fullmatch(r'(?:/[A-Za-z0-9_-]+)*', prefix):
    raise SystemExit('Invalid static service bind address or URL prefix.')
compile(Path(sys.argv[1]).read_text(encoding='utf-8'), sys.argv[1], 'exec')
PY
umask 077
mkdir -p "$root/public/notes"
install -m 600 "$server_source" "$root/server.py"
chown "$owner" "$root" "$root/public" "$root/public/notes" "$root/server.py"
if command -v systemctl >/dev/null && [[ "$(ps -p 1 -o comm=)" == systemd ]]; then
    umask 022
    cat >/etc/systemd/system/paper-collector-notes-static.service <<EOF
[Unit]
Description=Paper Collector HTML-only static service
After=network.target

[Service]
Type=simple
User=$owner
ExecStart=$python_bin $root/server.py --directory $root/public --host $bind --port $port --prefix=$prefix
Restart=on-failure
RestartSec=5
UMask=0077
Environment=PYTHONDONTWRITEBYTECODE=1
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
    chmod 644 /etc/systemd/system/paper-collector-notes-static.service
    systemctl daemon-reload
    systemctl enable paper-collector-notes-static.service
    systemctl restart paper-collector-notes-static.service
else
    if [[ "$owner" != root ]]; then
        printf 'A non-root static service account requires systemd.\n' >&2
        exit 1
    fi
    for command in cron flock; do
        if ! command -v "$command" >/dev/null; then
            printf 'Without systemd, install cron and flock first.\n' >&2
            exit 1
        fi
    done
    umask 077
    cat >"$root/run.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail
umask 077
exec 9>$root/server.lock
/usr/bin/flock -n 9 || exit 0
exec >>$root/server.log 2>&1
exec "$python_bin" $root/server.py --directory $root/public --host $bind --port $port --prefix=$prefix
EOF
    chmod 700 "$root/run.sh"
    umask 022
    cat >/etc/cron.d/paper-collector-notes-static <<EOF
SHELL=/bin/bash
HOME=/root
PATH=/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
MAILTO=""
@reboot root /bin/bash $root/run.sh
* * * * * root /bin/bash $root/run.sh
EOF
    chmod 644 /etc/cron.d/paper-collector-notes-static
    nohup /bin/bash "$root/run.sh" >/dev/null 2>&1 </dev/null &
    service cron restart
fi
"$python_bin" - "$port" "$prefix" <<'PY'
import sys
import time
import urllib.error
import urllib.request
endpoint = 'http://127.0.0.1:' + sys.argv[1] + sys.argv[2] + '/healthz'
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
for attempt in range(20):
    try:
        with opener.open(endpoint, timeout=1) as response:
            if response.status == 200 and response.headers.get('Server', '').startswith('PaperCollectorNotes/'):
                break
    except (urllib.error.URLError, TimeoutError):
        pass
    time.sleep(0.5)
else:
    raise SystemExit('Static service failed health check; inspect the service log or port conflicts.')
print('VPS static service healthy. Only public HTML notes are served; no paper pipeline runs here.')
PY
