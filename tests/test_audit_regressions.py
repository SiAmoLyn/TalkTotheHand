"""Regression tests for the 2026-10-24 cross-audit (Codex A01-A12, Gemini G1-G8).

Each test pins one confirmed finding so the same fake-pass / bypass cannot
return silently.
"""
import json
import stat
import sys
from pathlib import Path

import pytest

from hand.contracts import TaskPackage
from hand.executors import OpenCodeExecutor
from hand.graph import HandRunner
from hand.monitor import _advance
from hand.opencode import EventParser
from hand.tools import PathViolation, blender_run, fs_guard, gate_aabb


# --- A06 / G1: case-insensitive filesystems must not bypass fs_guard --------
@pytest.mark.parametrize("rel", [".GIT/config", ".Git/config", ".SSH/id_ed25519",
                                 ".Aws/credentials", "Memory.md", "MEMORY.md",
                                 "sub/Credentials.JSON"])
def test_fs_guard_blocks_case_variants(tmp_path, rel):
    with pytest.raises(PathViolation):
        fs_guard(rel, workspace=tmp_path)


# --- A07 / G2: the hand's own source/tools dirs are not task-writable -------
@pytest.mark.parametrize("declared", ["tools", "Src", "TESTS", "Work", ".GIT"])
def test_paths_reject_reserved_directories(tmp_path, task_data, declared):
    task_data["allowed_directories"] = [declared]
    task = TaskPackage.model_validate(task_data)
    with HandRunner(tmp_path) as runner:
        with pytest.raises(ValueError):
            runner._paths(task)


# --- A01 / G4: Blender runs must fail on script exceptions ------------------
def test_blender_run_requests_python_exit_code(tmp_path):
    script = tmp_path / "build.py"
    script.write_text("# build\n")
    recorder = tmp_path / "argv.json"
    fake = tmp_path / "fake-blender"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"json.dump(sys.argv[1:], open({str(recorder)!r}, 'w'))\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    result = blender_run(script, workspace=tmp_path, executable=str(fake))
    assert result["passed"] is True
    assert "--python-exit-code" in json.loads(recorder.read_text())


# --- A08: a gate report must never overwrite its own input ------------------
def test_gate_aabb_refuses_to_overwrite_input(tmp_path):
    bounds = tmp_path / "bounds.json"
    original = json.dumps({"objects": [{"name": "part", "min": [0, 0, 0], "max": [1, 1, 1]}]})
    bounds.write_text(original)
    result = gate_aabb(bounds, workspace=tmp_path, result_path=bounds)
    assert result["passed"] is not True
    assert bounds.read_text() == original


# --- A03: a later step_start revokes an earlier terminal marker -------------
def test_event_parser_finished_is_not_sticky():
    parser = EventParser()
    parser.consume(json.dumps({"type": "step_finish", "sessionID": "s1",
                               "part": {"reason": "stop", "tokens": {"total": 1}}}).encode())
    assert parser.finished is True
    parser.consume(json.dumps({"type": "step_start", "sessionID": "s1", "part": {}}).encode())
    assert parser.finished is False


# --- A04 (parser half): missing token usage is flagged, not zeroed ----------
def test_event_parser_flags_missing_token_usage():
    parser = EventParser()
    parser.consume(json.dumps({"type": "step_finish", "sessionID": "s1",
                               "part": {"reason": "stop"}}).encode())
    assert parser.finished is True
    assert parser.usage_missing is True
    assert parser.tokens == 0


def _fake_opencode(tmp_path, events, *, write_deliverable=True):
    fake = tmp_path / "fake-opencode"
    lines = "".join(json.dumps(event) + "\n" for event in events)
    body = f"#!{sys.executable}\nimport sys\n"
    if write_deliverable:
        body += ("args = sys.argv[1:]\n"
                 "from pathlib import Path\n"
                 "Path(args[args.index('--dir') + 1], 'demo.txt').write_text('made')\n")
    body += f"sys.stdout.write({lines!r})\n"
    fake.write_text(body)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    return fake


def _run_fake_executor(tmp_path, task_data, fake):
    task_data["approval_points"] = []
    task_data["opencode"] = {"model": "test/model"}
    task = TaskPackage.model_validate(task_data)
    executor = OpenCodeExecutor(executable=str(fake))
    return executor.execute(task, tmp_path, attempt=1, allowed_directories=[])


# --- A04 (executor half): a real run without usage cannot settle ------------
def test_executor_blocks_when_token_usage_missing(tmp_path, task_data):
    fake = _fake_opencode(tmp_path, [
        {"type": "step_finish", "sessionID": "ses_a", "part": {"reason": "stop"}}])
    evidence = _run_fake_executor(tmp_path, task_data, fake)
    assert evidence.status == "blocked"
    assert any("missing token usage" in item for item in evidence.unresolved)


# --- A05: a crashed attempt's spend is seeded into the resumed run ----------
def test_executor_seeds_usage_from_previous_attempt(tmp_path, task_data):
    store = tmp_path / ".hand" / "opencode"
    store.mkdir(parents=True)
    (store / "session.json").write_text(json.dumps(
        {"session_id": "ses_old", "model": "test/model", "fixture_replay": False,
         "tokens": 40, "reported_cost": 0}) + "\n")
    fake = _fake_opencode(tmp_path, [
        {"type": "step_finish", "sessionID": "ses_new",
         "part": {"reason": "stop", "tokens": {"total": 5}}}])
    evidence = _run_fake_executor(tmp_path, task_data, fake)
    assert evidence.cost.tokens == 45


# --- A09 + G6: closure needs a passing receipt; persisted recovery works ---
def _monitor_config():
    return {"actions": {"action1": {"enabled": True}, "action2": {"enabled": True},
                        "action3": {"enabled": True}},
            "min_action_interval_seconds": 600, "action2_interval_seconds": 1800,
            "wait_seconds": 300}


def _recovered_state(**overrides):
    state = {"phase": "recovered", "closure_done": False, "recovered_at": 100.0,
             "detected_at": 50.0, "last_action_at": 100.0}
    state.update(overrides)
    return state


def test_monitor_reservation_does_not_complete_closure():
    state = _recovered_state()
    action, _reason = _advance(state, {"blackhole": False, "latest_success_at": None},
                               _monitor_config(), now=1000.0)
    # The persisted recovery confirmation lets action3 fire even though the
    # confirming record aged out of the detection window (G6)...
    assert action == "action3"
    # ...but merely reserving it must not mark closure done (A09).
    assert state["closure_done"] is False


def test_monitor_still_waits_without_any_recovery_evidence():
    state = _recovered_state(recovered_at=None)
    action, reason = _advance(state, {"blackhole": False, "latest_success_at": None},
                              _monitor_config(), now=1000.0)
    assert action is None
    assert reason == "awaiting_recovery_confirmation"


# --- G8: a task must declare at least one acceptance gate -------------------
def test_task_package_requires_at_least_one_gate(task_data):
    task_data["gates"] = []
    with pytest.raises(Exception):
        TaskPackage.model_validate(task_data)


# --- A10: the pick script only trusts open panels and verified selection ----
def test_pick_script_rejects_save_panels_and_requires_selection():
    from hand.gui_tools import _pick_script
    script = _pick_script(123, Path("/tmp/wanted.txt"))
    assert "/^(Open|Save)/" not in script
    assert "/Save/.test(t)" in script
    assert "selected === true" in script
