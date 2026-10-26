"""Behavioural regression tests for the second debug round (R2-01..R2-11).

Every test runs the real behaviour it pins — executor/graph accounting with
fake OpenCode binaries, real report/guard code, real fake Cua receipts, the
generated pick script executed under Node against a fake driver, and a
killed-mid-attempt executor subprocess. Source-string assertions are not
used as evidence (that was R2-10's lesson).
"""
import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest

from hand.cli import demo_task
from hand.contracts import TaskPackage
from hand.executors import OpenCodeExecutor
from hand.graph import HandRunner
from hand.gui_tools import _pick_script, gui_click, gui_screenshot
from hand.opencode import EventParser
from hand.tools import gate_clipsim, godot_run


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------

def _event(identity, total, session="ses_x"):
    return {"type": "step_finish", "sessionID": session,
            "part": {"id": identity, "reason": "stop", "tokens": {"total": total}}}


def _fake_opencode(tmp_path, event_sets, name="fake-opencode"):
    """Fake opencode binary: invocation i emits event_sets[i]; writes demo.txt."""
    counter = tmp_path / f"{name}.count"
    counter.write_text("0")
    fake = tmp_path / name
    body = (
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "from pathlib import Path\n"
        f"sets = {event_sets!r}\n"
        f"c = Path({str(counter)!r}); i = int(c.read_text()); c.write_text(str(i + 1))\n"
        "args = sys.argv[1:]\n"
        "Path(args[args.index('--dir') + 1], 'demo.txt').write_text('made')\n"
        "for e in sets[min(i, len(sets) - 1)]:\n"
        "    sys.stdout.write(json.dumps(e) + '\\n')\n"
    )
    fake.write_text(body)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    return fake


def _task(task_data):
    task_data["approval_points"] = []
    task_data["opencode"] = {"model": "test/model"}
    return TaskPackage.model_validate(task_data)


def _execute(tmp_path, task, fake):
    return OpenCodeExecutor(executable=str(fake)).execute(
        task, tmp_path, attempt=1, allowed_directories=[])


def _session_path(tmp_path):
    return tmp_path / ".hand" / "opencode" / "session.json"


def _write_session(tmp_path, payload):
    store = tmp_path / ".hand" / "opencode"
    store.mkdir(parents=True, exist_ok=True)
    _session_path(tmp_path).write_text(json.dumps(payload) + "\n")


# --------------------------------------------------------------------------
# R2-01: a graph retry must add each attempt's tokens exactly once
# --------------------------------------------------------------------------

def test_graph_retry_adds_each_attempts_tokens_once(tmp_path, task_data):
    task = _task(task_data)
    fake = _fake_opencode(tmp_path, [[_event("one", 10)], [_event("two", 5)]])
    with HandRunner(tmp_path) as runner:
        runner.executor = OpenCodeExecutor(executable=str(fake))
        workspace, _ = runner._paths(task)
        workspace.mkdir(parents=True, exist_ok=True)
        state = {"task": task.model_dump(mode="json"), "history": [], "logs": [],
                 "attempt": 0, "retries": 0, "tokens": 0, "active_minutes": 0.0}
        state.update(runner._running(state))
        assert state["tokens"] == 10
        state.update(runner._running(state))
        # Distinct step-finish events total 10 + 5; the seeded cumulative
        # ledger must not be re-added (was 25 before the fix).
        assert state["candidate"]["cost"]["tokens"] == 5
        assert state["tokens"] == 15


def test_executor_second_run_reports_only_unsettled_tokens(tmp_path, task_data):
    task = _task(task_data)
    fake = _fake_opencode(tmp_path, [[_event("one", 10)], [_event("two", 5)]])
    assert _execute(tmp_path, task, fake).cost.tokens == 10
    assert _execute(tmp_path, task, fake).cost.tokens == 5


# --------------------------------------------------------------------------
# R2-02: already-accounted event ids survive in the ledger; replays dedupe
# --------------------------------------------------------------------------

def test_unsettled_crash_spend_counts_once_even_when_replayed(tmp_path, task_data):
    # Ledger exactly as the fixed executor persists it mid-attempt, i.e.
    # after a crash: 40 tokens counted (id "old"), nothing settled yet.
    _write_session(tmp_path, {"session_id": "ses_x", "model": "test/model",
                              "fixture_replay": False, "tokens": 40, "reported_cost": 0,
                              "accounted_ids": ["old"], "usage_missing": False,
                              "settled_tokens": 0, "settled_cost": 0, "pending_minutes": 0})
    task = _task(task_data)
    fake = _fake_opencode(tmp_path, [[_event("old", 40), _event("new", 5)]])
    evidence = _execute(tmp_path, task, fake)
    # The crashed 40 are reported once (not lost, not doubled); the
    # replayed event adds nothing; only the new 5 are new.
    assert evidence.cost.tokens == 45


def test_settled_replay_reports_zero_new_tokens(tmp_path, task_data):
    _write_session(tmp_path, {"session_id": "ses_x", "model": "test/model",
                              "fixture_replay": False, "tokens": 40, "reported_cost": 0,
                              "accounted_ids": ["old"], "usage_missing": False,
                              "settled_tokens": 40, "settled_cost": 0, "pending_minutes": 0})
    task = _task(task_data)
    fake = _fake_opencode(tmp_path, [[_event("old", 40)]])
    assert _execute(tmp_path, task, fake).cost.tokens == 0


# --------------------------------------------------------------------------
# R2-03: synthetic or foreign ledgers never seed a real run
# --------------------------------------------------------------------------

@pytest.mark.parametrize("stored", [
    {"session_id": "ses_fixture", "model": "test/model", "fixture_replay": True,
     "tokens": 40, "reported_cost": 0},
    {"session_id": "ses_other", "model": "other/model", "fixture_replay": False,
     "tokens": 40, "reported_cost": 0},
])
def test_synthetic_or_foreign_session_never_seeds_real_run(tmp_path, task_data, stored):
    _write_session(tmp_path, stored)
    task = _task(task_data)
    fake = _fake_opencode(tmp_path, [[_event("new", 5)]])
    evidence = _execute(tmp_path, task, fake)
    assert evidence.status == "in_progress"
    assert evidence.cost.tokens == 5


# --------------------------------------------------------------------------
# R2-04: empty/null/unrecognisable token structures are unknown usage
# --------------------------------------------------------------------------

@pytest.mark.parametrize("usage", [None, {}, [], {"cache": {}}, {"other": 3}])
def test_empty_token_structures_flag_usage_missing(usage):
    parser = EventParser()
    parser.consume(json.dumps({"type": "step_finish", "sessionID": "s",
                               "part": {"reason": "stop", "tokens": usage}}).encode())
    assert parser.usage_missing is True
    assert parser.tokens == 0


@pytest.mark.parametrize("usage,expected", [
    ({"total": 0}, 0), ({"total": 7}, 7), ({"input": 2, "output": 1}, 3),
    ({"cache": {"read": 3}}, 3), ({"cache": {"read": 1, "write": 2}, "reasoning": 4}, 7),
])
def test_recognisable_token_structures_still_count(usage, expected):
    parser = EventParser()
    parser.consume(json.dumps({"type": "step_finish", "sessionID": "s",
                               "part": {"reason": "stop", "tokens": usage}}).encode())
    assert parser.usage_missing is False
    assert parser.tokens == expected


def test_executor_blocks_on_null_token_usage(tmp_path, task_data):
    task = _task(task_data)
    event = {"type": "step_finish", "sessionID": "ses_x",
             "part": {"id": "n", "reason": "stop", "tokens": None}}
    fake = _fake_opencode(tmp_path, [[event]])
    evidence = _execute(tmp_path, task, fake)
    assert evidence.status == "blocked"
    assert any("missing token usage" in item for item in evidence.unresolved)


# --------------------------------------------------------------------------
# R2-05: unknown usage persists in the ledger and keeps resumed runs blocked
# --------------------------------------------------------------------------

def test_unknown_usage_survives_into_next_run(tmp_path, task_data):
    task = _task(task_data)
    missing = {"type": "step_finish", "sessionID": "ses_x",
               "part": {"id": "m", "reason": "stop"}}
    fake = _fake_opencode(tmp_path, [[missing], [_event("later", 5)]])
    assert _execute(tmp_path, task, fake).status == "blocked"
    saved = json.loads(_session_path(tmp_path).read_text())
    assert saved["usage_missing"] is True
    resumed = _execute(tmp_path, task, fake)
    assert resumed.status == "blocked"
    assert any("missing token usage" in item for item in resumed.unresolved)


# --------------------------------------------------------------------------
# R2-06: reports/logs must not overwrite secondary inputs either
# --------------------------------------------------------------------------

def _fake_godot(tmp_path):
    fake = tmp_path / "fake-godot"
    fake.write_text(f"#!{sys.executable}\nprint('PASS=1 FAIL=0')\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    return fake


def _godot_project(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "project.godot").write_text("config_version=5\n")
    (project / "verify.gd").write_text("original verify script")
    return project


def test_godot_report_cannot_overwrite_verify_script(tmp_path):
    project = _godot_project(tmp_path)
    result = godot_run(project, workspace=tmp_path, script=project / "verify.gd",
                       result_path=project / "verify.gd", executable=str(_fake_godot(tmp_path)))
    assert result["passed"] is not True
    assert (project / "verify.gd").read_text() == "original verify script"


def test_godot_report_cannot_overwrite_project_manifest(tmp_path):
    project = _godot_project(tmp_path)
    result = godot_run(project, workspace=tmp_path,
                       result_path=project / "project.godot", executable=str(_fake_godot(tmp_path)))
    assert result["passed"] is not True
    assert (project / "project.godot").read_text() == "config_version=5\n"


def test_godot_logs_cannot_overwrite_verify_script(tmp_path):
    # The verify script lives at the path the report's stdout log would take.
    project = tmp_path / "project"
    project.mkdir()
    (project / "project.godot").write_text("config_version=5\n")
    script = project / "out.stdout.log"
    script.write_text("original verify script")
    result = godot_run(project, workspace=tmp_path, script=script,
                       result_path=project / "out.json", executable=str(_fake_godot(tmp_path)))
    assert result["passed"] is not True
    assert script.read_text() == "original verify script"


def test_clipsim_report_cannot_overwrite_adapter_script(tmp_path):
    (tmp_path / "image.png").write_bytes(b"image")
    (tmp_path / "ref.png").write_bytes(b"locked")
    adapter = tmp_path / "adapter.py"
    adapter.write_text("original adapter")
    result = gate_clipsim(tmp_path / "image.png", tmp_path / "ref.png", workspace=tmp_path,
                          script=adapter, result_path=adapter,
                          reference_sha256=hashlib.sha256(b"locked").hexdigest(),
                          reference_band=[0.7, 0.9])
    assert result["passed"] is not True
    assert adapter.read_text() == "original adapter"


# --------------------------------------------------------------------------
# R2-07: GUI receipts must explicitly and consistently confirm success
# --------------------------------------------------------------------------

def _fake_cua(tmp_path, payload):
    fake = tmp_path / "fake-cua"
    body = (
        f"#!{sys.executable}\n"
        "import sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "if '--screenshot-out-file' in args:\n"
        "    Path(args[args.index('--screenshot-out-file') + 1]).write_bytes("
        "b'\\x89PNG\\r\\n\\x1a\\nfake screenshot')\n"
        f"print({payload!r})\n"
    )
    fake.write_text(body)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    return fake


@pytest.mark.parametrize("payload", ["{}", "[]"])
def test_screenshot_requires_an_explicit_success_receipt(tmp_path, monkeypatch, payload):
    monkeypatch.setenv("HAND_CUA_BIN", str(_fake_cua(tmp_path, payload)))
    result = gui_screenshot(workspace=tmp_path)
    assert result["status"] == "blocked"
    assert result["passed"] is False
    assert "path" not in result  # no screenshot artifact was registered


def test_screenshot_with_success_receipt_still_passes(tmp_path, monkeypatch):
    monkeypatch.setenv("HAND_CUA_BIN", str(_fake_cua(tmp_path, '{"success": true}')))
    result = gui_screenshot(workspace=tmp_path)
    assert result["status"] == "passed"


@pytest.mark.parametrize("payload", ['{"ok": true, "success": "false"}',
                                     '{"ok": true, "success": 0}',
                                     '{"success": true, "ok": "yes"}'])
def test_click_rejects_contradictory_receipts(tmp_path, monkeypatch, payload):
    monkeypatch.setenv("HAND_CUA_BIN", str(_fake_cua(tmp_path, payload)))
    result = gui_click(workspace=tmp_path, element="TOKEN")
    assert result["status"] == "blocked"


def test_click_with_consistent_receipt_still_passes(tmp_path, monkeypatch):
    monkeypatch.setenv("HAND_CUA_BIN", str(_fake_cua(tmp_path, '{"ok": true, "success": true}')))
    result = gui_click(workspace=tmp_path, element="TOKEN")
    assert result["status"] == "passed"
    assert result["receipt"] == {"ok": True, "success": True}


# --------------------------------------------------------------------------
# R2-08: fixture replay evidence is stamped simulated, never verified
# --------------------------------------------------------------------------

def test_fixture_replay_evidence_is_stamped_simulated(tmp_path, task_data):
    fixture = tmp_path / "events.jsonl"
    fixture.write_text(json.dumps(_event("f1", 22, session="ses_fx")) + "\n")
    task_data["approval_points"] = []
    task_data["executor_hint"] = "opencode-fixture"
    task_data["opencode"] = {"fixture": "events.jsonl"}
    task_data["gates"] = ["gate_aabb"]
    task = TaskPackage.model_validate(task_data)
    workspace = tmp_path / "work" / task.id
    workspace.mkdir(parents=True)
    (workspace / "events.jsonl").write_bytes(fixture.read_bytes())
    (workspace / "demo.txt").write_text("fixture deliverable")
    with HandRunner(tmp_path) as runner:
        runner.executor = OpenCodeExecutor(fixture=workspace / "events.jsonl")
        evidence = runner.executor.execute(task, workspace, attempt=1, allowed_directories=[])
        evidence.gate_reports = {"gate_aabb": {"passed": True}}
        state = {"task": task.model_dump(mode="json"),
                 "candidate": evidence.model_dump(mode="json"),
                 "history": [], "logs": list(evidence.logs), "attempt": 1,
                 "tokens": evidence.cost.tokens, "active_minutes": evidence.cost.minutes}
        out = runner._returned(state)
    assert out["evidence"]["status"] == "simulated"


# --------------------------------------------------------------------------
# R2-09: the CLIP adapter loads cached weights by path, never by model name
# --------------------------------------------------------------------------

def _run_adapter(tmp_path, weights_bytes):
    """Run compute_score with a stub clip module; return (stdout, load calls)."""
    home = tmp_path / "home"
    cache = home / ".cache" / "clip"
    cache.mkdir(parents=True)
    if weights_bytes is not None:
        (cache / "ViT-L-14.pt").write_bytes(weights_bytes)
    stub = tmp_path / "stub"
    stub.mkdir()
    calls = tmp_path / "calls.log"
    (stub / "clip.py").write_text(
        "import os\n"
        "def load(name, device='cpu'):\n"
        f"    open({str(calls)!r}, 'a').write(str(name) + '\\n')\n"
        "    raise RuntimeError('stub load stop')\n")
    (stub / "numpy.py").write_text("")
    (stub / "torch.py").write_text("")
    (stub / "PIL.py").write_text("class Image:\n    @staticmethod\n    def open(p):\n        return None\n")
    tools = Path(__file__).resolve().parents[1] / "tools"
    code = (f"import sys; sys.path.insert(0, {str(tools)!r})\n"
            "from clipsim_adapter import compute_score\n"
            "try:\n    compute_score('a.png', 'b.png')\n"
            "except Exception as e:\n    print(type(e).__name__ + ': ' + str(e))\n")
    env = {**os.environ, "HOME": str(home), "PYTHONPATH": str(stub)}
    proc = subprocess.run([sys.executable, "-c", code], env=env,
                          capture_output=True, text=True, timeout=30)
    recorded = calls.read_text().splitlines() if calls.exists() else []
    return proc.stdout.strip(), recorded, home / ".cache" / "clip" / "ViT-L-14.pt"


def test_clip_adapter_loads_cached_weights_by_local_path(tmp_path):
    buffer = tmp_path / "weights.zip"
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("archive/data.pkl", "stub weights")
    out, calls, weights = _run_adapter(tmp_path, buffer.read_bytes())
    # The loader is reached exactly once, with the local file path — the
    # model-name form is what would enter CLIP's download path.
    assert calls == [str(weights)]
    assert "stub load stop" in out


def test_clip_adapter_refuses_corrupt_cache_without_loading(tmp_path):
    out, calls, _ = _run_adapter(tmp_path, b"corrupt-not-real-weights")
    assert calls == []
    assert "not a valid TorchScript archive" in out


def test_clip_adapter_refuses_missing_cache_without_loading(tmp_path):
    out, calls, _ = _run_adapter(tmp_path, None)
    assert calls == []
    assert "not cached" in out


# --------------------------------------------------------------------------
# R2-10: the generated pick script is exercised behaviourally under Node
# --------------------------------------------------------------------------

_PICK_RUNNER = r"""
const fs = require('fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;
(async () => {
  const out = [];
  for (const c of input.cases) {
    const keys = [];
    let lists = 0;
    const panel = {window_id: 7, title: c.save ? 'Save' : 'Open'};
    const app = {
      getState: async () => ({returned_element_count: c.empty ? 0 : 3}),
      hotkey: async () => {},
      setValue: async () => {},
      pressKey: async k => { keys.push(k); },
      query: async () => { if (c.queryError) throw new Error('fake query failure');
                           return [{selected: c.selected === true}]; },
    };
    const cua = {
      listWindows: async () => ({windows: (lists++ === 0 || c.stillOpen) ? [panel] : []}),
      getApp: async () => app,
      sleep: async () => {},
    };
    try {
      const outcome = await new AsyncFunction('cua', input.script)(cua);
      out.push({case: c.name, outcome, returns: keys.filter(k => k === 'return').length});
    } catch (e) { out.push({case: c.name, error: String(e), returns: keys.length}); }
  }
  console.log(JSON.stringify(out));
})();
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required to execute the generated pick script")
def test_pick_script_behaviour_under_fake_driver(tmp_path):
    script = _pick_script(123, tmp_path / "wanted.txt")
    cases = [
        {"name": "save_panel", "save": True, "selected": True},
        {"name": "open_unselected", "selected": False},
        {"name": "query_throws", "queryError": True},
        {"name": "open_selected_panel_closes", "selected": True},
        {"name": "open_selected_panel_stays", "selected": True, "stillOpen": True},
        {"name": "empty_ax", "empty": True},
    ]
    proc = subprocess.run(["node", "-e", _PICK_RUNNER],
                          input=json.dumps({"script": script, "cases": cases}),
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    results = {row["case"]: row for row in json.loads(proc.stdout)}
    # A Save panel is not an Open panel: no keys are pressed at all.
    assert results["save_panel"]["outcome"]["stage"] == "not_found"
    assert results["save_panel"]["returns"] == 0
    # Unselected or unverifiable selection: only the navigation Return is
    # sent; the confirmation Return must never be pressed.
    assert results["open_unselected"]["outcome"]["stage"] == "unconfirmed"
    assert results["open_unselected"]["returns"] == 1
    assert results["query_throws"]["outcome"]["stage"] == "unconfirmed"
    assert results["query_throws"]["returns"] == 1
    # An empty accessibility tree is not drivable.
    assert results["empty_ax"]["outcome"]["stage"] == "not_drivable"
    assert results["empty_ax"]["returns"] == 0
    # Verified selection + panel gone afterwards is the only opened path.
    assert results["open_selected_panel_closes"]["outcome"] == {"stage": "opened", "opened": True}
    assert results["open_selected_panel_closes"]["returns"] == 2
    # Verified selection but the panel is still there: not a success.
    assert results["open_selected_panel_stays"]["outcome"]["stage"] == "unconfirmed"
    assert results["open_selected_panel_stays"]["outcome"]["opened"] is False


# --------------------------------------------------------------------------
# R2-11: a killed attempt leaves its unsettled time (and tokens) in the
# ledger; the resumed run reports them exactly once
# --------------------------------------------------------------------------

def test_killed_attempt_leaves_unsettled_time_and_tokens(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "demo.txt").write_text("made")
    fake = _fake_opencode(tmp_path, [[_event("later", 1)]], name="unused")
    hanging = tmp_path / "fake-hanging"
    event_line = json.dumps(_event("crash1", 7))
    hanging.write_text(
        f"#!{sys.executable}\n"
        "import sys, time\n"
        f"print({event_line!r}, flush=True)\n"
        "time.sleep(30)\n")
    hanging.chmod(hanging.stat().st_mode | stat.S_IEXEC)
    data = demo_task("r2-crash")
    data["approval_points"] = []
    data["opencode"] = {"model": "test/model"}
    driver = tmp_path / "driver.py"
    src = Path(__file__).resolve().parents[1] / "src"
    driver.write_text(
        f"import sys\nsys.path.insert(0, {str(src)!r})\n"
        "from pathlib import Path\n"
        "from hand.contracts import TaskPackage\n"
        "from hand.executors import OpenCodeExecutor\n"
        f"task = TaskPackage.model_validate({data!r})\n"
        f"OpenCodeExecutor(executable={str(hanging)!r}).execute("
        f"task, Path({str(workspace)!r}), attempt=1, allowed_directories=[])\n")
    proc = subprocess.Popen([sys.executable, str(driver)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    session = workspace / ".hand" / "opencode" / "session.json"
    saved = None
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if session.is_file():
                try:
                    candidate = json.loads(session.read_text())
                except ValueError:
                    candidate = None
                if candidate and candidate.get("tokens") == 7:
                    saved = candidate
                    break
            time.sleep(0.1)
        assert saved is not None, "executor never checkpointed the in-flight ledger"
    finally:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)
    # The killed attempt settled nothing, but its spend and its elapsed
    # time are in the ledger, with the counted event id recorded.
    assert saved["settled_tokens"] == 0
    assert saved["accounted_ids"] == ["crash1"]
    assert saved["pending_minutes"] > 0

    # Resume: the crashed event is replayed by the stream and a new one
    # arrives; the evidence reports the crashed spend once, plus the new.
    task = TaskPackage.model_validate(data)
    fast = _fake_opencode(tmp_path, [[_event("crash1", 7), _event("new1", 5)]], name="fake-fast")
    evidence = OpenCodeExecutor(executable=str(fast)).execute(
        task, workspace, attempt=2, allowed_directories=[])
    assert evidence.cost.tokens == 12
    assert evidence.cost.minutes >= saved["pending_minutes"] - 1e-9
