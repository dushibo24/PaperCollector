import os
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "name", ["run-scheduled.sh", "install-cron.sh", "run-notes-server.sh", "install-notes-service.sh", "install-vps-static.sh"]
)
def test_deployment_scripts_have_valid_bash_syntax(name):
    script = Path(__file__).parents[1] / "deploy" / name
    subprocess.run(["bash", "-n", str(script)], check=True)


def test_scheduled_runner_uses_project_environment_and_private_logs(tmp_path):
    repo_root = tmp_path / "project with spaces"
    deploy_dir = repo_root / "deploy"
    deploy_dir.mkdir(parents=True)
    source = Path(__file__).parents[1] / "deploy" / "run-scheduled.sh"
    shutil.copy2(source, deploy_dir / source.name)
    bin_dir = repo_root / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    command = bin_dir / "paper-collector"
    command.write_text(
        '#!/bin/bash\nprintf "TZ=%s\\nHF_HUB_OFFLINE=%s\\n" "$TZ" "$HF_HUB_OFFLINE"\n'
        'printf "%s\\n" "$@"\n'
    )
    command.chmod(0o755)

    result = subprocess.run(
        ["bash", str(deploy_dir / source.name), "--no-notify"],
        cwd=tmp_path,
        env={**os.environ, "TZ": "UTC"},
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout == ""
    assert result.stderr == ""
    logs = list((repo_root / "data" / "logs").glob("cron-*.log"))
    assert len(logs) == 1
    assert "TZ=Asia/Shanghai" in logs[0].read_text()
    assert "HF_HUB_OFFLINE=1" in logs[0].read_text()
    assert f"run\n--config\n{repo_root}/config.toml\n--no-notify\n" in logs[0].read_text()
    assert logs[0].stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("lock_available", [True, False])
def test_notes_runner_keeps_lock_descriptor_and_starts_only_one_instance(tmp_path, lock_available):
    root = tmp_path / "project with spaces"
    deploy = root / "deploy"
    deploy.mkdir(parents=True)
    source = Path(__file__).parents[1] / "deploy" / "run-notes-server.sh"
    shutil.copy2(source, deploy / source.name)
    binaries = root / ".venv" / "bin"
    binaries.mkdir(parents=True)
    command = binaries / "paper-collector"
    command.write_text('#!/bin/bash\ntest -e /dev/fd/9\nprintf "%s\\n" "$@"\n')
    command.chmod(0o755)
    home = tmp_path / "home"
    local_bin = home / ".local" / "bin"
    local_bin.mkdir(parents=True)
    flock = local_bin / "flock"
    flock.write_text(f'#!/bin/bash\nexit {0 if lock_available else 1}\n')
    flock.chmod(0o755)
    result = subprocess.run(
        ["bash", str(deploy / source.name)],
        env={**os.environ, "HOME": str(home)}, capture_output=True, text=True, check=True,
    )
    assert result.stdout == "" and result.stderr == ""
    log = root / "data" / "logs" / "notes-server.log"
    if lock_available:
        assert f"serve-notes\n--config\n{root}/config.toml\n" in log.read_text()
        assert log.stat().st_mode & 0o777 == 0o600
    else:
        assert not log.exists()
