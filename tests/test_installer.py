import os
import stat
import sys
from dataclasses import replace
from pathlib import Path

from test_db import make_review

from paper_collector import doctor, scheduler
from paper_collector.config import load_config
from paper_collector.doctor import Check
from paper_collector.models import Paper
from paper_collector.pipeline import Pipeline, brief_for_serverchan
from paper_collector.setup_wizard import run_setup, set_toml_value, update_env

REPO = Path(__file__).parents[1]


def make_paper(paper_id: str = "p1") -> Paper:
    return Paper(
        id=paper_id,
        title="Title",
        abstract="x",
        authors=[],
        publication_date="2026-11-01",
        year=2026,
        landing_url="https://example.org/paper",
    )


def test_set_toml_value_only_touches_the_named_section():
    text = "[translation]\nenabled = true\n# keep\n\n[reading_notes]\nenabled = true\n"
    result = set_toml_value(text, "translation", "enabled", False)
    assert result == "[translation]\nenabled = false\n# keep\n\n[reading_notes]\nenabled = true\n"
    assert 'channel = "none"' in set_toml_value(text, "notify", "channel", "none")
    assert set_toml_value(text, "reading_notes", "engine", "json").endswith(
        '[reading_notes]\nengine = "json"\nenabled = true\n'
    )


def test_update_env_merges_values_and_is_owner_only(tmp_path):
    template = tmp_path / ".env.example"
    template.write_text("# comment\nA=\nB=\n", encoding="utf-8")
    env = tmp_path / ".env"
    update_env(env, {"A": "secret", "C": "new"}, template=template)
    assert env.read_text(encoding="utf-8") == "# comment\nA=secret\nB=\nC=new\n"
    update_env(env, {"B": "two"}, template=template)
    assert env.read_text(encoding="utf-8") == "# comment\nA=secret\nB=two\nC=new\n"
    if os.name == "posix":
        assert stat.S_IMODE(env.stat().st_mode) == 0o600


def _project(tmp_path: Path, monkeypatch) -> Path:
    for name in ("config.example.toml", ".env.example"):
        (tmp_path / name).write_text((REPO / name).read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.deepseek.com/anthropic")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "token")
    return tmp_path


def test_non_interactive_setup_writes_minimal_local_only_config(tmp_path, monkeypatch):
    root = _project(tmp_path, monkeypatch)
    run_setup(
        ["--yes", "--skip-install", "--no-translation", "--no-notes", "--notify", "none"],
        root,
    )
    config = load_config(root / "config.toml")
    assert config.translation_enabled is False
    assert config.reading_notes_enabled is False
    assert config.baidupan_enabled is False
    assert config.notify_channel == "none"
    assert (root / ".env").exists()


def test_baidupan_is_forced_off_without_serverchan(tmp_path, monkeypatch):
    root = _project(tmp_path, monkeypatch)
    run_setup(["--yes", "--skip-install", "--baidupan", "--notify", "wecom"], root)
    config = load_config(root / "config.toml")
    assert config.notify_channel == "wecom" and config.baidupan_enabled is False


def test_serverchan_brief_omits_download_section_without_share():
    paper, review = make_paper(), make_review("p1")
    assert "百度网盘" not in brief_for_serverchan(paper, review, "", "")
    assert "提取码：`abcd`" in brief_for_serverchan(paper, review, "https://pan/s/1", "abcd")


def test_serverchan_without_baidupan_sends_without_upload(config, monkeypatch):
    config.notify_channel = "serverchan"
    config.serverchan_sendkey = "key"
    config.baidupan_enabled = False
    sent = []

    def must_not_publish(*_args):
        raise AssertionError("Baidu upload is disabled")

    class FakeNotifier:
        def __init__(self, _key):
            pass

        def send(self, _title, content):
            sent.append(content)

    monkeypatch.setattr("paper_collector.pipeline.BaiduPCSGoPublisher", must_not_publish)
    monkeypatch.setattr("paper_collector.pipeline.ServerChanNotifier", FakeNotifier)
    pipeline = Pipeline(config)
    try:
        paper = make_paper()
        pipeline.db.upsert_paper(paper)
        pipeline.db.save_review(paper.id, make_review(paper.id))
        pipeline._deliver(paper, make_review(paper.id), config.data_dir / "x.zip")
        assert sent and "百度网盘" not in sent[0]
        assert pipeline.db.work_row(paper.id)["file_delivered_at"]
    finally:
        pipeline.close()


def test_channel_none_counts_local_package_as_delivered(config):
    pipeline = Pipeline(config)
    try:
        paper = make_paper()
        pipeline.db.upsert_paper(paper)
        pipeline.db.save_review(paper.id, make_review(paper.id))
        pipeline._deliver(paper, make_review(paper.id), config.data_dir / "x.zip")
        assert pipeline.db.work_row(paper.id)["file_delivered_at"]
    finally:
        pipeline.close()


def test_doctor_skips_disabled_optional_features(config, monkeypatch):
    commands = []

    def command_check(name, executable, args, needle=None):
        commands.append(name)
        return Check(True, name, executable)

    def must_not_login(*_args):
        raise AssertionError("Baidu login check must be skipped")

    monkeypatch.setattr(doctor, "_command_check", command_check)
    monkeypatch.setattr(doctor, "_network_check", lambda name, url: Check(True, name, url))
    monkeypatch.setattr(doctor, "BaiduPCSGoPublisher", must_not_login)
    checks = doctor.run_checks(
        replace(config, notify_channel="serverchan", serverchan_sendkey="k", baidupan_enabled=False)
    )
    assert "pdf2zh" not in commands
    assert all(check.ok for check in checks if check.name in {"pdf2zh", "百度网盘"})


def test_cron_install_replaces_only_its_own_line(config, monkeypatch):
    crontab = {"content": "0 1 * * * other-job\n"}

    class Result:
        returncode = 0
        stderr = ""

        def __init__(self, stdout):
            self.stdout = stdout

    def fake_run(command, **kwargs):
        if command == ["crontab", "-"]:
            crontab["content"] = kwargs["input"]
        return Result(crontab["content"])

    monkeypatch.setattr(scheduler.shutil, "which", lambda _name: "/usr/bin/crontab")
    monkeypatch.setattr(scheduler.subprocess, "run", fake_run)
    scheduler.install_cron(config, 9, 30)
    scheduler.install_cron(config, 8, 0)
    lines = crontab["content"].splitlines()
    assert lines[0] == "0 1 * * * other-job"
    assert len(lines) == 2 and lines[1].startswith("0 8 * * * cd ")
    assert "-m paper_collector run --scheduled" in lines[1]
    assert sys.executable in lines[1]
    scheduler.uninstall_cron()
    assert crontab["content"] == "0 1 * * * other-job\n"


def test_schtasks_install_uses_short_wrapper(config, monkeypatch):
    calls = []

    class Result:
        returncode = 0
        stdout = stderr = ""

    def fake_run(command, **_kwargs):
        calls.append(command)
        return Result()

    monkeypatch.setattr(scheduler.subprocess, "run", fake_run)
    wrapper = scheduler.install_schtasks(config, 7, 5)
    assert calls[0][:5] == ["schtasks", "/Create", "/F", "/SC", "DAILY"]
    assert "07:05" in calls[0] and calls[0][-1] == f'"{wrapper}"'
    assert "--scheduled" in wrapper.read_text(encoding="utf-8")
