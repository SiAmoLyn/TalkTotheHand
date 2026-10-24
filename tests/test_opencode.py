"""Offline only: no test starts an OpenCode model session."""
import json
import os
from pathlib import Path
import sys
from threading import Event, Timer
import time

import pytest

from hand.contracts import TaskPackage
from hand.executors import OpenCodeExecutor
from hand.opencode import EventParser, permission_config
from hand.processes import run_process

FIXTURES = Path(__file__).parent / "fixtures"


def stage(tmp_path, task_data, fixture="opencode-normal.jsonl"):
    (tmp_path / "events.jsonl").write_bytes((FIXTURES / fixture).read_bytes())
    (tmp_path / "demo.txt").write_text("pre-staged offline fixture")
    task_data["opencode"] = {"fixture": "events.jsonl"}
    return TaskPackage.model_validate(task_data)


def executable(tmp_path, body):
    path = tmp_path / "fake-opencode"
    path.write_text(f"#!{sys.executable}\n" + body)
    path.chmod(0o700)
    return str(path)


def test_normal_parser_session_text_tools_cost():
    parser = EventParser()
    for line in (FIXTURES / "opencode-normal.jsonl").read_bytes().splitlines():
        parser.consume(line)
    summary = parser.summary()
    assert summary["session_id"] == "ses_fixture_pkg2"
    assert summary["tokens"] == 22
    assert summary["event_duration_minutes"] == pytest.approx(1 / 60)
    assert summary["finished"] and not summary["errors"]
    assert {s["type"] for s in summary["steps"]} >= {"text", "tool_use", "step_finish"}


@pytest.mark.parametrize("fixture,expected", [("opencode-normal.jsonl", "in_progress"),
                                              ("opencode-error.jsonl", "blocked"),
                                              ("opencode-timeout.jsonl", "blocked")])
def test_fixture_replay_preserves_all_bytes(tmp_path, task_data, fixture, expected):
    task = stage(tmp_path, task_data, fixture)
    result = OpenCodeExecutor().execute(task, tmp_path, attempt=1, allowed_directories=[])
    assert result.status == expected
    assert Path(result.logs[0]).read_bytes() == (FIXTURES / fixture).read_bytes()
    parsed = json.loads(Path(result.logs[2]).read_text())
    assert parsed["fixture_replay"]
    assert parsed["session_id"]
    assert result.cost.tokens == parsed["tokens"]


@pytest.mark.parametrize("line", [b"not json", b"[]", b'{"part":5}',
                                 b'{"type":"step_finish","part":{"tokens":{"input":-1}}}'])
def test_invalid_events_retained_as_errors(line):
    parser = EventParser()
    parser.consume(line)
    assert parser.errors


def test_repeated_steps_not_double_counted():
    parser = EventParser()
    line = b'{"type":"step_finish","part":{"id":"x","reason":"stop","tokens":{"total":9}}}'
    parser.consume(line)
    parser.consume(line)
    assert parser.tokens == 9


def test_policy_denies_shell_and_sensitive_files(tmp_path):
    config = permission_config(tmp_path, [tmp_path / "copy"])
    assert config["share"] == "disabled"
    assert config["permission"]["bash"] == "deny"
    assert config["permission"]["read"]["*"] == "deny"
    assert config["permission"]["read"][str(tmp_path) + "/**"] == "allow"
    assert config["permission"]["read"]["**/.ssh/**"] == "deny"
    assert config["permission"]["edit"]["**/MEMORY.md"] == "deny"


def test_no_model_or_missing_executable_blocks(tmp_path, task_data, monkeypatch):
    monkeypatch.delenv("HAND_OPENCODE_MODEL", raising=False)
    task = TaskPackage.model_validate(task_data)
    result = OpenCodeExecutor().execute(task, tmp_path, attempt=1, allowed_directories=[])
    assert result.status == "blocked"
    assert any("model not configured" in e for e in result.unresolved)
    task.opencode.model = "test/offline"
    result = OpenCodeExecutor(executable=str(tmp_path / "missing")).execute(task, tmp_path, attempt=2, allowed_directories=[])
    assert result.status == "blocked"
    assert any("No such file" in e for e in result.unresolved)


def test_fake_cli_arguments_environment_and_shared_parser(tmp_path, task_data, monkeypatch):
    stage(tmp_path, task_data)
    content = (FIXTURES / "opencode-normal.jsonl").read_text()
    binary = executable(tmp_path, "import json, os, sys\nfrom pathlib import Path\n"
                        "Path('args.json').write_text(json.dumps(sys.argv[1:]))\n"
                        "Path('env.json').write_text(json.dumps(dict(os.environ)))\n"
                        f"print({content!r}, end='', flush=True)\n")
    monkeypatch.setenv("OPENAI_API_KEY", "do-not-inherit-test-value")
    task_data["opencode"] = {"model": "test/offline", "session_id": "ses_old"}
    task = TaskPackage.model_validate(task_data)
    result = OpenCodeExecutor(executable=binary).execute(task, tmp_path, attempt=1, allowed_directories=[])
    assert result.status == "in_progress"
    assert result.cost.tokens == 22
    args = json.loads((tmp_path / "args.json").read_text())
    assert args[:4] == ["run", "--format", "json", "--dir"]
    assert "--auto" not in args
    assert args[args.index("--session") + 1] == "ses_old"
    env = json.loads((tmp_path / "env.json").read_text())
    assert "OPENAI_API_KEY" not in env
    assert json.loads(env["OPENCODE_CONFIG_CONTENT"])["share"] == "disabled"
    assert env["HOME"].startswith(str(tmp_path))


def test_fake_cli_timeout_is_hard_and_preserves_partial_line(tmp_path, task_data):
    task_data["budget"]["max_minutes"] = 0.005
    task_data["opencode"] = {"model": "test/offline"}
    binary = executable(tmp_path, "import os, time\nos.write(1, b'{\"partial\":')\ntime.sleep(30)\n")
    # Warm this exact path once before the timed run. Measured on macOS
    # (2026-10-24): the first launch of a fresh executable path pays a
    # ~0.44s platform assessment penalty (repeat launches: ~0.01s), which
    # alone exceeds this test's 0.3s budget and leaves the child no time
    # to write anything before the hard kill. The warm-up isolates the
    # executor semantics under test (hard timeout + byte preservation);
    # the budget and every assertion below are unchanged.
    run_process([binary], cwd=tmp_path, stdout=tmp_path / "warm.out",
                stderr=tmp_path / "warm.err", timeout=1.5)
    started = time.monotonic()
    result = OpenCodeExecutor(executable=binary).execute(TaskPackage.model_validate(task_data), tmp_path,
                                                       attempt=1, allowed_directories=[])
    assert time.monotonic() - started < 3
    assert result.status == "blocked"
    assert any("timeout" in e for e in result.unresolved)
    assert Path(result.logs[0]).read_bytes() == b'{"partial":'


def test_cancellation_kills_child_and_saves_output(tmp_path):
    cancel = Event()
    timer = Timer(0.25, cancel.set)
    timer.start()
    try:
        result = run_process([sys.executable, "-c", "import time; print('partial', flush=True); time.sleep(30)"],
                             cwd=tmp_path, stdout=tmp_path / "stdout", stderr=tmp_path / "stderr",
                             timeout=10, cancel=cancel)
    finally:
        timer.cancel()
    assert result["reason"] == "cancelled"
    assert result["exit_code"] < 0
    assert (tmp_path / "stdout").read_text() == "partial\n"


def test_timeout_kills_entire_group_even_when_parent_has_exited(tmp_path):
    child = "import time; from pathlib import Path; time.sleep(.8); Path('survived').touch()"
    parent = f"import subprocess,sys; subprocess.Popen([sys.executable,'-c',{child!r}]); print('spawned',flush=True)"
    result = run_process([sys.executable, "-c", parent], cwd=tmp_path,
                         stdout=tmp_path / "stdout", stderr=tmp_path / "stderr", timeout=0.25)
    assert result["reason"] == "timeout"
    time.sleep(0.9)
    assert not (tmp_path / "survived").exists()


def test_fixture_whitelist_rejection(tmp_path, task_data):
    task = stage(tmp_path, task_data)
    task.opencode.fixture = "../escape.jsonl"
    result = OpenCodeExecutor().execute(task, tmp_path, attempt=1, allowed_directories=[])
    assert result.status == "blocked"
    assert any("whitelist" in e for e in result.unresolved)


def test_live_token_budget_stops_process(tmp_path, task_data):
    task_data["opencode"] = {"model": "test/offline"}
    content = '{"type":"step_finish","sessionID":"ses_x","part":{"reason":"tool-calls","tokens":{"total":101}}}'
    binary = executable(tmp_path, f"import time\nprint({content!r},flush=True)\ntime.sleep(30)\n")
    result = OpenCodeExecutor(executable=binary).execute(TaskPackage.model_validate(task_data), tmp_path,
                                                       attempt=1, allowed_directories=[])
    assert result.status == "blocked"
    assert result.cost.tokens == 101
    assert any("token budget" in e for e in result.unresolved)


def test_cost_sums_multiple_steps_and_rejects_nonfinite_json():
    parser = EventParser()
    parser.consume(b'{"type":"step_finish","part":{"id":"a","tokens":{"total":3},"cost":0.1}}')
    parser.consume(b'{"type":"step_finish","part":{"id":"b","reason":"stop","tokens":{"total":4},"cost":0.2}}')
    assert parser.tokens == 7
    assert parser.reported_cost == pytest.approx(0.3)
    parser.consume(b'{"type":"text","part":{"text":NaN}}')
    assert "Non-finite" in parser.errors[-1]


def test_continue_and_recovered_session_flags(tmp_path, task_data):
    stage(tmp_path, task_data)
    content = (FIXTURES / "opencode-normal.jsonl").read_text()
    binary = executable(tmp_path, "import json,sys\nfrom pathlib import Path\n"
                        "Path('args.json').write_text(json.dumps(sys.argv[1:]))\n"
                        f"print({content!r},end='')\n")
    task_data["opencode"] = {"model": "test/offline", "continue_session": True}
    task = TaskPackage.model_validate(task_data)
    executor = OpenCodeExecutor(executable=binary)
    first = executor.execute(task, tmp_path, attempt=1, allowed_directories=[])
    assert first.status == "in_progress"
    assert "--continue" in json.loads((tmp_path / "args.json").read_text())
    task.opencode.continue_session = False
    second = executor.execute(task, tmp_path, attempt=2, allowed_directories=[])
    assert second.status == "in_progress"
    args = json.loads((tmp_path / "args.json").read_text())
    assert args[args.index("--session") + 1] == "ses_fixture_pkg2"
