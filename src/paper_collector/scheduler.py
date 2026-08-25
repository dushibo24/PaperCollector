from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
from pathlib import Path

from paper_collector.config import AppConfig

LABEL = "com.papercollector.daily"


def install_launchd(config: AppConfig, hour: int, minute: int) -> Path:
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("hour/minute 超出范围")
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("找不到 uv，无法创建稳定的定时运行环境")
    launch_agents = Path.home() / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True, exist_ok=True)
    plist_path = launch_agents / f"{LABEL}.plist"
    logs = config.data_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    env_path = os.environ.get("PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")
    payload = {
        "Label": LABEL,
        "ProgramArguments": [
            uv,
            "run",
            "--project",
            str(config.repo_root),
            "paper-collector",
            "run",
            "--config",
            str(config.path),
        ],
        "WorkingDirectory": str(config.repo_root),
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "StandardOutPath": str(logs / "launchd.out.log"),
        "StandardErrorPath": str(logs / "launchd.err.log"),
        "EnvironmentVariables": {"PATH": env_path},
        "RunAtLoad": False,
    }
    with plist_path.open("wb") as handle:
        plistlib.dump(payload, handle, sort_keys=False)
    subprocess.run(
        ["launchctl", "bootout", f"gui/{os.getuid()}", str(plist_path)],
        capture_output=True,
        check=False,
    )
    result = subprocess.run(
        ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist_path)],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap 失败：{result.stderr.strip()}")
    return plist_path


def uninstall_launchd() -> Path:
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    if plist_path.exists():
        subprocess.run(
            ["launchctl", "bootout", f"gui/{os.getuid()}", str(plist_path)],
            capture_output=True,
            check=False,
        )
        plist_path.unlink()
    return plist_path
