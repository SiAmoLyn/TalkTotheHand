import json

import yaml

from hand import cli


def test_demo_and_status(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    assert cli.main(["demo"]) == 0
    assert "Demo passed" in capsys.readouterr().out
    files = list((tmp_path / "outbox").glob("*/evidence.json"))
    assert len(files) == 1
    assert json.loads(files[0].read_text())["status"] == "simulated"
    assert cli.main(["status", files[0].parent.name]) == 0
    assert '"status": "RETURNED"' in capsys.readouterr().out


def test_run_pause_resume_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    path = tmp_path / "task.yaml"
    path.write_text(yaml.safe_dump(cli.demo_task("manual-cli")))
    assert cli.main(["run", str(path)]) == 0
    assert '"status": "WAITING_APPROVAL"' in capsys.readouterr().out
    assert cli.main(["resume", "manual-cli"]) == 0
    assert '"status": "RETURNED"' in capsys.readouterr().out


def test_unknown_status_returns_clear_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    assert cli.main(["status", "unknown"]) == 2
    assert "Unknown task" in capsys.readouterr().err


def test_invalid_task_cli_errors(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    path = tmp_path / "bad.yaml"
    path.write_text("id: broken")
    assert cli.main(["run", str(path)]) == 2
    assert "Task package validation failed" in capsys.readouterr().err
