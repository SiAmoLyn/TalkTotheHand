import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from hand.contracts import EvidencePackage, TaskPackage
from hand.executors import FakeExecutor
from hand.graph import HandRunner
from hand.tools import SnapshotResult


def test_full_workflow(tmp_path, task_data):
    with HandRunner(tmp_path) as runner:
        state = runner.run(TaskPackage.model_validate(task_data))
        assert state["history"] == ["RECEIVED", "VALIDATED", "PLANNED", "RUNNING", "GATING", "RETURNED"]
        assert runner.status(task_data["id"])["next"] == []
        assert runner.saver.get_tuple({"configurable": {"thread_id": task_data["id"]}})
    evidence = EvidencePackage.model_validate_json((tmp_path / "outbox/test-task/evidence.json").read_text())
    assert evidence.status == "verified"
    assert evidence.gate_reports["fake_artifact_check"]["fake"] is True
    for artifact in evidence.artifacts:
        assert hashlib.sha256(Path(artifact.path).read_bytes()).hexdigest() == artifact.sha256
    assert (tmp_path / "work/checkpoints.sqlite").is_file()


def test_interrupt_then_resume_in_new_process(tmp_path, task_data):
    task_data["approval_points"] = ["manual approval"]
    with HandRunner(tmp_path) as runner:
        state = runner.run(TaskPackage.model_validate(task_data))
        assert state["status"] == "WAITING_APPROVAL"
        assert state["attempt"] == 0
        assert runner.status("test-task")["interrupts"]
        assert not (tmp_path / "work/test-task/demo.txt").exists()
        assert not (tmp_path / "outbox/test-task/evidence.json").exists()
    script = "from pathlib import Path; from hand.graph import HandRunner; import sys;\nwith HandRunner(Path(sys.argv[1])) as runner:\n state = runner.resume('test-task'); assert state['status'] == 'RETURNED'; print(state['status'])"
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path)], capture_output=True, text=True,
                            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")})
    assert result.returncode == 0, result.stderr
    assert "RETURNED" in result.stdout


def test_untriggered_approval_does_not_pause(tmp_path, task_data):
    task_data["approval_points"] = [{"id": "inactive", "triggered": False}]
    with HandRunner(tmp_path) as runner:
        state = runner.run(TaskPackage.model_validate(task_data))
    assert state["status"] == "RETURNED"
    assert "WAITING_APPROVAL" not in state["history"]


def test_approval_rejected_blocks_before_execution(tmp_path, task_data):
    task_data["approval_points"] = ["manual"]
    with HandRunner(tmp_path) as runner:
        runner.run(TaskPackage.model_validate(task_data))
        state = runner.resume("test-task", approved=False)
    assert state["status"] == "BLOCKED"
    assert state["attempt"] == 0
    assert "denied" in state["evidence"]["unresolved"][0]


def test_failed_gate_retries_twice_then_blocks(tmp_path, task_data):
    with HandRunner(tmp_path, executor=FakeExecutor(fail_attempts=99)) as runner:
        state = runner.run(TaskPackage.model_validate(task_data))
    assert state["status"] == "BLOCKED"
    assert state["retries"] == 2
    assert state["attempt"] == 3
    assert state["history"].count("RETRY") == 2
    assert "RETURNED" not in state["history"]
    assert state["evidence"]["gate_reports"]["fake_artifact_check"]["passed"] is False
    assert sum("RETRY" in line for line in state["evidence"]["logs"]) == 2


def test_retry_recovers(tmp_path, task_data):
    with HandRunner(tmp_path, executor=FakeExecutor(fail_attempts=1)) as runner:
        state = runner.run(TaskPackage.model_validate(task_data))
    assert state["status"] == "RETURNED"
    assert state["retries"] == 1
    assert state["attempt"] == 2


class InvalidEvidenceExecutor(FakeExecutor):
    def execute(self, *args, **kwargs):
        return {"status": "verified"}


def test_invalid_evidence_cannot_return(tmp_path, task_data):
    with HandRunner(tmp_path, executor=InvalidEvidenceExecutor()) as runner:
        state = runner.run(TaskPackage.model_validate(task_data))
    assert state["status"] == "BLOCKED"
    assert "validation error" in state["reason"].lower()


class TamperingExecutor(FakeExecutor):
    def __init__(self, mode):
        self.mode = mode
        super().__init__()

    def execute(self, *args, **kwargs):
        evidence = super().execute(*args, **kwargs)
        if self.mode == "sha":
            Path(evidence.artifacts[0].path).write_text("tampered after hashing")
        elif self.mode == "gates":
            evidence.gate_reports = {}
        elif self.mode == "truthy":
            evidence.gate_reports["fake_artifact_check"]["passed"] = "true"
        elif self.mode == "missing":
            evidence.artifacts = []
        elif self.mode == "unresolved":
            evidence.unresolved = ["not actually done"]
        elif self.mode == "tokens":
            evidence.cost.tokens = 200
        elif self.mode == "minutes":
            evidence.cost.minutes = 2
        return evidence


@pytest.mark.parametrize("mode", ["sha", "gates", "truthy", "missing", "unresolved", "tokens", "minutes"])
def test_dishonest_or_over_budget_evidence_blocks(tmp_path, task_data, mode):
    with HandRunner(tmp_path, executor=TamperingExecutor(mode)) as runner:
        state = runner.run(TaskPackage.model_validate(task_data))
    assert state["status"] == "BLOCKED"
    assert "RETURNED" not in state["history"]


def test_path_escape_blocks_before_writing(tmp_path, task_data):
    task_data["deliverables"] = ["../escape.txt"]
    with HandRunner(tmp_path) as runner:
        state = runner.run(TaskPackage.model_validate(task_data))
    assert state["status"] == "BLOCKED"
    assert state["attempt"] == 0
    assert not (tmp_path / "work/escape.txt").exists()


def test_declared_directory_is_confined_to_project(tmp_path, task_data):
    task_data["allowed_directories"] = [str(tmp_path.parent)]
    with HandRunner(tmp_path) as runner:
        state = runner.run(TaskPackage.model_validate(task_data))
    assert state["status"] == "BLOCKED"


def test_declared_project_copy_is_allowed(tmp_path, task_data):
    task_data["allowed_directories"] = ["copies/demo"]
    task_data["deliverables"] = [{"path": str(tmp_path / "copies/demo/output.txt"), "type": "text"}]
    with HandRunner(tmp_path) as runner:
        state = runner.run(TaskPackage.model_validate(task_data))
    assert state["status"] == "RETURNED"
    assert (tmp_path / "copies/demo/output.txt").is_file()


def test_existing_task_and_terminal_resume_rejected(tmp_path, task_data):
    task = TaskPackage.model_validate(task_data)
    with HandRunner(tmp_path) as runner:
        runner.run(task)
        with pytest.raises(ValueError, match="already exists"):
            runner.run(task)
        with pytest.raises(ValueError, match="terminal"):
            runner.resume(task.id)
        with pytest.raises(ValueError, match="Unknown task"):
            runner.status("unknown")
        with pytest.raises(ValueError, match="Invalid task id"):
            runner.status("../outside")


def test_snapshot_failure_does_not_block(tmp_path, task_data, monkeypatch):
    monkeypatch.setattr("hand.graph.git_snapshot", lambda _: SnapshotResult(False, "git snapshot failed: test failure"))
    with HandRunner(tmp_path) as runner:
        state = runner.run(TaskPackage.model_validate(task_data))
    assert state["status"] == "RETURNED"
    assert any("git snapshot failed" in line for line in state["evidence"]["logs"])


def test_telemetry_forced_off():
    assert os.environ["LANGGRAPH_CLI_NO_ANALYTICS"] == "1"
    assert os.environ["LANGSMITH_TRACING"] == "false"
    assert os.environ["LANGCHAIN_TRACING_V2"] == "false"


def test_opencode_without_model_blocks(tmp_path, task_data, monkeypatch):
    monkeypatch.delenv("HAND_OPENCODE_MODEL", raising=False)
    task_data["executor_hint"] = "opencode"
    with HandRunner(tmp_path) as runner:
        state = runner.run(TaskPackage.model_validate(task_data))
    assert state["status"] == "BLOCKED"
    assert "model not configured" in state["reason"]
