import hashlib
import json
from pathlib import Path
from threading import Timer
import warnings

import pytest

from hand import cli
from hand.contracts import TaskPackage
from hand.executors import FakeExecutor, OpenCodeExecutor
from hand.graph import HandRunner

FIXTURES = Path(__file__).parent / "fixtures"


class BoundsExecutor(FakeExecutor):
    def execute(self, task, workspace, **kwargs):
        result = super().execute(task, workspace, **kwargs)
        target = Path(result.artifacts[0].path)
        target.write_bytes((FIXTURES / "aabb.json").read_bytes())
        result.artifacts[0].sha256 = hashlib.sha256(target.read_bytes()).hexdigest()
        return result


def test_failed_real_gate_cannot_be_forged_by_executor(tmp_path, task_data):
    task_data.update(gates=["gate_aabb"], gate_options={"gate_aabb": {
        "artifact": "demo.txt", "expected_dimensions": {"part": [5, 2, 3]}}})
    with HandRunner(tmp_path, executor=BoundsExecutor()) as runner:
        state = runner.run(TaskPackage.model_validate(task_data))
    assert state["status"] == "BLOCKED"
    assert state["attempt"] == 3 and state["retries"] == 2
    assert state["history"].count("GATING") == 3
    assert "RETURNED" not in state["history"]
    assert state["evidence"]["gate_reports"]["gate_aabb"]["status"] == "failed"
    assert "measured" in state["reason"]
    assert len(list((tmp_path / "work/test-task/.hand/gates").glob("attempt-*/*.json"))) == 3


def test_missing_gate_script_blocks_after_existing_retry_path(tmp_path, task_data):
    work = tmp_path / "work/test-task"
    work.mkdir(parents=True)
    (work / "reference.png").write_bytes(b"reference")
    task_data.update(gates=["gate_clipsim"], gate_options={"gate_clipsim": {
        "artifact": "demo.txt", "reference": "reference.png", "script": "missing.py",
        "reference_sha256": hashlib.sha256(b"reference").hexdigest(), "reference_band": [0.7, 0.9]}})
    with HandRunner(tmp_path) as runner:
        state = runner.run(TaskPackage.model_validate(task_data))
    assert state["status"] == "BLOCKED" and state["retries"] == 2
    report = state["evidence"]["gate_reports"]["gate_clipsim"]
    assert report["status"] == "blocked" and "score" not in report
    assert "missing" in report["error"]


def test_fixture_demo_runs_real_aabb_gate(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    assert cli.main(["demo", "--executor", "opencode-fixture"]) == 0
    capsys.readouterr()
    evidence = json.loads(next((tmp_path / "outbox").glob("*/evidence.json")).read_text())
    assert evidence["status"] == "verified"
    assert evidence["gate_reports"]["gate_aabb"]["passed"] is True
    assert "fake" not in evidence["gate_reports"]["gate_aabb"]
    assert evidence["cost"]["tokens"] == 22
    assert "fixture" in evidence["cost"]["model"]


def test_status_assignment_emits_no_serializer_warning(tmp_path, task_data):
    with warnings.catch_warnings():
        warnings.simplefilter("error", UserWarning)
        with HandRunner(tmp_path) as runner:
            assert runner.run(TaskPackage.model_validate(task_data))["status"] == "RETURNED"


def test_pre_cancelled_graph_cannot_return(tmp_path, task_data):
    with HandRunner(tmp_path) as runner:
        runner.cancel()
        state = runner.run(TaskPackage.model_validate(task_data))
    assert state["status"] == "BLOCKED" and state["retries"] == 0
    assert "cancelled" in state["reason"]


def test_graph_cancel_terminates_running_executor(tmp_path, task_data):
    import sys
    binary = tmp_path / "fake-opencode"
    binary.write_text(f"#!{sys.executable}\nimport time\nprint('{{\"type\":\"text\",\"sessionID\":\"ses_x\",\"part\":{{\"text\":\"partial\"}}}}',flush=True)\ntime.sleep(30)\n")
    binary.chmod(0o700)
    task_data["opencode"] = {"model": "test/offline"}
    with HandRunner(tmp_path, executor=OpenCodeExecutor(executable=str(binary))) as runner:
        timer = Timer(0.5, runner.cancel)
        timer.start()
        try:
            state = runner.run(TaskPackage.model_validate(task_data))
        finally:
            timer.cancel()
    assert state["status"] == "BLOCKED" and state["retries"] == 0
    assert "cancelled" in state["reason"]
    assert any("events.jsonl" in p for p in state["evidence"]["logs"])


def test_fixture_retry_cost_is_cumulative_and_reports_preserved(tmp_path, task_data):
    work = tmp_path / "work/test-task"
    work.mkdir(parents=True)
    (work / "events.jsonl").write_bytes((FIXTURES / "opencode-normal.jsonl").read_bytes())
    (work / "bounds.json").write_bytes((FIXTURES / "aabb.json").read_bytes())
    task_data.update(executor_hint="opencode-fixture", opencode={"fixture": "events.jsonl"},
                     deliverables=["bounds.json"], gates=["gate_aabb"],
                     gate_options={"gate_aabb": {"artifact": "bounds.json", "expected_dimensions": {"part": [9, 2, 3]}}})
    with HandRunner(tmp_path) as runner:
        state = runner.run(TaskPackage.model_validate(task_data))
    assert state["status"] == "BLOCKED" and state["retries"] == 2
    assert state["evidence"]["cost"]["tokens"] == 66
    assert "fixture" in state["evidence"]["cost"]["model"]
    assert len(list((work / ".hand/opencode").glob("attempt-*/events.jsonl"))) == 3
    assert state["evidence"]["gate_reports"]["gate_aabb"]["status"] == "failed"
