import pytest

from paper_collector import cli
from paper_collector.pipeline import PipelineBusyError


def test_recovery_tick_with_active_worker_is_a_clean_skip(config_file, monkeypatch, capsys):
    closed = []

    class BusyPipeline:
        def __init__(self, _config):
            pass

        def run(self, **_kwargs):
            raise PipelineBusyError("already running")

        def close(self):
            closed.append(True)

    monkeypatch.setattr(cli, "Pipeline", BusyPipeline)
    cli.main(["run", "--scheduled", "--config", str(config_file)])
    output = capsys.readouterr()
    assert "跳过" in output.out and not output.err
    assert closed


def test_audit_returns_nonzero_when_delivery_is_not_confirmed(config_file, monkeypatch):
    closed = []

    class AuditPipeline:
        def __init__(self, _config):
            pass

        def check_daily_delivery(self):
            return False

        def close(self):
            closed.append(True)

    monkeypatch.setattr(cli, "Pipeline", AuditPipeline)
    with pytest.raises(SystemExit) as exc:
        cli.main(["check-delivery", "--config", str(config_file)])
    assert exc.value.code == 1
    assert closed
