"""Build end-user release archives from the committed tree and refuse to ship secrets.

    uv run python scripts/build_release.py

Archives come from `git archive HEAD`, so untracked/ignored files (config.toml,
.env, data/) can never be included. Each archive is then scanned for forbidden
paths, credential-shaped strings and every credential from the local .env/config.toml.
"""

from __future__ import annotations

import io
import re
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_PATHS = re.compile(
    r"(^|/)(config\.toml|\.env|data/|.*\.sqlite3|.*\.plist|\.claude/|\.venv/)"
)
SECRET_PATTERNS = [
    re.compile(rb"sk-[A-Za-z0-9]{20,}"),
    re.compile(rb"SCT\d+T[A-Za-z0-9]{10,}"),
    re.compile(rb"BDUSS=[A-Za-z0-9~-]{20,}"),
    re.compile(rb"STOKEN=[0-9a-f]{20,}"),
    re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(rb"(?m)^[A-Z][A-Z0-9_]*(TOKEN|KEY|SENDKEY|PASSWORD|COOKIE)=\S+"),
]


def git(*args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True).stdout


SENSITIVE_KEY = re.compile(
    r"key|token|cookie|password|secret|email|ssh_host|ssh_directory", re.IGNORECASE
)


def _sensitive_strings(table: dict) -> list[str]:
    values = []
    for key, value in table.items():
        if isinstance(value, dict):
            values.extend(_sensitive_strings(value))
        elif isinstance(value, str) and SENSITIVE_KEY.search(key):
            values.append(value)
    return values


def local_secrets() -> list[bytes]:
    """Every credential configured locally (.env or config.toml) must be absent."""
    values = []
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and not key.lstrip().startswith("#"):
                values.append(value.strip().strip("'\""))
    config = ROOT / "config.toml"
    if config.exists():
        values.extend(_sensitive_strings(tomllib.loads(config.read_text(encoding="utf-8"))))
    placeholders = {"you@example.com"}
    return [
        value.encode()
        for value in values
        if len(value) >= 8 and not value.startswith("${") and value not in placeholders
    ]


def scan(files: dict[str, bytes]) -> list[str]:
    problems = []
    secrets = local_secrets()
    for name, data in files.items():
        relative = name.split("/", 1)[1] if "/" in name else name
        if FORBIDDEN_PATHS.search(relative) and not relative.endswith(".example"):
            problems.append(f"{name}: 不应发布的文件")
        for pattern in SECRET_PATTERNS:
            if match := pattern.search(data):
                problems.append(f"{name}: 疑似凭据 {match.group()[:12]!r}…")
        problems.extend(
            f"{name}: 含有本机 .env/config.toml 中的凭据" for value in secrets if value in data
        )
    return problems


def main() -> int:
    if git("status", "--porcelain", "--untracked-files=no").strip():
        print("工作区有未提交改动；发布包只包含已提交内容，请先提交。", file=sys.stderr)
        return 1
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]
    prefix = f"PaperCollector-{version}/"
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    outputs = {
        "tar.gz": dist / f"PaperCollector-{version}.tar.gz",
        "zip": dist / f"PaperCollector-{version}.zip",
    }
    for fmt, target in outputs.items():
        target.write_bytes(git("archive", f"--format={fmt}", f"--prefix={prefix}", "HEAD"))

    with tarfile.open(outputs["tar.gz"]) as archive:
        tar_files = {
            member.name: archive.extractfile(member).read()
            for member in archive.getmembers()
            if member.isfile()
        }
    with zipfile.ZipFile(io.BytesIO(outputs["zip"].read_bytes())) as archive:
        zip_files = {
            name: archive.read(name) for name in archive.namelist() if not name.endswith("/")
        }

    problems = scan(tar_files) + scan(zip_files)
    if problems:
        for target in outputs.values():
            target.unlink()
        print("发现可能的隐私泄露，已删除发布包：", file=sys.stderr)
        print("\n".join(f"  {item}" for item in sorted(set(problems))), file=sys.stderr)
        return 1
    for target in outputs.values():
        print(f"{target.relative_to(ROOT)}  ({target.stat().st_size // 1024} KiB)")
    print(f"共 {len(tar_files)} 个文件，未发现凭据或本地配置。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
