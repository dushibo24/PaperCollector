from __future__ import annotations

import os
import plistlib
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from paper_collector.config import AppConfig

LABEL = "com.papercollector.daily"
TASK_NAME = "PaperCollector"
CRON_MARKER = "# paper-collector daily"


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


def _validate_time(hour: int, minute: int) -> None:
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("hour/minute 超出范围")


def _scheduled_command(config: AppConfig) -> list[str]:
    # The interpreter of the current install; works for uv venvs and plain pip installs.
    return [
        sys.executable,
        "-m",
        "paper_collector",
        "run",
        "--scheduled",
        "--config",
        str(config.path),
    ]


def _read_crontab() -> str:
    result = subprocess.run(["crontab", "-l"], text=True, capture_output=True, check=False)
    # "no crontab for user" exits non-zero; treat it as empty.
    return result.stdout if result.returncode == 0 else ""


def _write_crontab(content: str) -> None:
    result = subprocess.run(
        ["crontab", "-"], input=content, text=True, capture_output=True, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(f"crontab 写入失败：{result.stderr.strip()}")


def _without_marker(content: str) -> list[str]:
    return [line for line in content.splitlines() if not line.endswith(CRON_MARKER)]


def install_cron(config: AppConfig, hour: int, minute: int) -> str:
    _validate_time(hour, minute)
    if not shutil.which("crontab"):
        raise RuntimeError("找不到 crontab，请先安装 cron")
    logs = config.data_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    command = shlex.join(_scheduled_command(config))
    line = (
        f"{minute} {hour} * * * cd {shlex.quote(str(config.repo_root))} && "
        f"{command} >> {shlex.quote(str(logs / 'cron.log'))} 2>&1 {CRON_MARKER}"
    )
    _write_crontab("\n".join([*_without_marker(_read_crontab()), line]) + "\n")
    return "当前用户 crontab"


def uninstall_cron() -> str:
    if shutil.which("crontab"):
        remaining = _without_marker(_read_crontab())
        _write_crontab("\n".join(remaining) + "\n" if remaining else "")
    return "当前用户 crontab"


def install_schtasks(config: AppConfig, hour: int, minute: int) -> Path:
    _validate_time(hour, minute)
    logs = config.data_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    # schtasks /TR is limited to 261 characters, so run through a wrapper script.
    wrapper = config.data_dir / "paper-collector-scheduled.cmd"
    command = subprocess.list2cmdline(_scheduled_command(config))
    wrapper.write_text(
        "@echo off\r\n"
        "chcp 65001 >nul\r\n"
        f'cd /d "{config.repo_root}"\r\n'
        f'{command} >> "{logs / "schtasks.log"}" 2>&1\r\n',
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            "schtasks",
            "/Create",
            "/F",
            "/SC",
            "DAILY",
            "/TN",
            TASK_NAME,
            "/ST",
            f"{hour:02d}:{minute:02d}",
            "/TR",
            f'"{wrapper}"',
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"schtasks 创建失败：{(result.stderr or result.stdout).strip()}")
    return wrapper


def uninstall_schtasks() -> str:
    subprocess.run(
        ["schtasks", "/Delete", "/F", "/TN", TASK_NAME],
        capture_output=True,
        check=False,
    )
    return f"计划任务 {TASK_NAME}"


def install_schedule(config: AppConfig, hour: int, minute: int) -> str:
    if sys.platform == "darwin":
        return str(install_launchd(config, hour, minute))
    if os.name == "nt":
        return str(install_schtasks(config, hour, minute))
    return install_cron(config, hour, minute)


def uninstall_schedule() -> str:
    if sys.platform == "darwin":
        return str(uninstall_launchd())
    if os.name == "nt":
        return uninstall_schtasks()
    return uninstall_cron()
