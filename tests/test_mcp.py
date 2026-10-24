"""Real official-SDK clients, SQLite checkpoints and offline FakeExecutor only."""

from functools import partial
import hashlib
import json
import os
from pathlib import Path
import sys
from threading import Event

import anyio
from mcp import Client
from mcp.client.stdio import StdioServerParameters
import pytest
import yaml

from hand import cli
from hand.contracts import EvidencePackage
from hand.executors import FakeExecutor
from hand.graph import HandRunner
from hand.mcp_server import create_server


pytestmark = pytest.mark.anyio
TOOL_NAMES = {"hand_submit_task", "hand_task_status", "hand_task_resume", "hand_get_evidence"}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def client(tmp_path):
    async with Client(create_server(tmp_path), raise_exceptions=True, cache=None) as connected:
        yield connected


async def call(client, name, arguments, *, error=None):
    result = await client.call_tool(name, arguments)
    assert result.structured_content is not None
    payload = result.structured_content
    assert json.loads(result.content[0].text) == payload
    assert result.is_error is (error is not None)
    assert payload["ok"] is (error is None)
    if error is not None:
        assert payload["error"]["code"] == error
    return payload


async def submit(client, data, *, error=None):
    return await call(client, "hand_submit_task", {"task_text": json.dumps(data)}, error=error)


async def test_sdk_tool_names_and_schemas(client):
    tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    assert set(tools) == TOOL_NAMES
    for tool in tools.values():
        assert tool.input_schema["additionalProperties"] is False
        assert tool.output_schema["required"] == ["ok"]
    assert tools["hand_submit_task"].input_schema["required"] == ["task_text"]
    for name in TOOL_NAMES - {"hand_submit_task"}:
        assert tools[name].input_schema["required"] == ["task_id"]
        assert "pattern" in tools[name].input_schema["properties"]["task_id"]
    assert tools["hand_task_resume"].input_schema["properties"]["approved"]["default"] is None


async def test_submit_status_resume_evidence_rejection_chain(client, tmp_path, task_data):
    task_data["approval_points"] = ["operator decision"]
    state = await submit(client, task_data)
    assert state["task_id"] == task_data["id"]
    assert state["status"] == state["stage"] == "WAITING_APPROVAL"
    state = await call(client, "hand_task_status", {"task_id": task_data["id"]})
    assert state["next"] == ["WAITING_APPROVAL"]
    assert state["evidence_summary"] is None
    assert state["interrupts"][0]["approval_points"][0]["id"] == "operator decision"
    assert not (tmp_path / "work/test-task/demo.txt").exists()

    state = await call(client, "hand_task_resume", {"task_id": task_data["id"], "approved": False})
    assert state["status"] == "BLOCKED"
    evidence = (await call(client, "hand_get_evidence", {"task_id": task_data["id"]}))["evidence"]
    assert evidence == json.loads((tmp_path / "outbox/test-task/evidence.json").read_text())
    assert EvidencePackage.model_validate(evidence).status == "blocked"
    assert "denied" in evidence["unresolved"][0]
    assert not (tmp_path / "work/test-task/demo.txt").exists()
    with HandRunner(tmp_path) as runner:
        assert runner.status(task_data["id"])["attempt"] == 0


@pytest.mark.parametrize("decision", [{}, {"approved": None}, {"approved": True}])
async def test_mcp_never_self_approves(client, tmp_path, task_data, decision):
    task_data["approval_points"] = ["external-send", "spend", "canonical-edit", "outside-delete"]
    await submit(client, task_data)
    for _ in range(2):
        result = await call(client, "hand_task_resume", {"task_id": task_data["id"], **decision},
                            error="APPROVAL_REQUIRED")
        assert result["error"]["operator_command"] == "hand resume test-task"
        assert len(result["error"]["approval_points"]) == 4
    with HandRunner(tmp_path) as runner:
        state = runner.status(task_data["id"])
        assert state["status"] == "WAITING_APPROVAL"
        assert state["attempt"] == 0
        assert not state.get("approvals")
    assert not (tmp_path / "work/test-task/demo.txt").exists()
    assert not (tmp_path / "outbox/test-task/evidence.json").exists()


async def test_operator_cli_approval_is_visible_to_connected_mcp(client, tmp_path, task_data, monkeypatch, capsys):
    task_data["approval_points"] = ["operator approval"]
    await submit(client, task_data)
    await call(client, "hand_task_resume", {"task_id": task_data["id"]}, error="APPROVAL_REQUIRED")
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    # This explicit CLI action represents the separate operator, not an MCP grant.
    assert await anyio.to_thread.run_sync(partial(cli.main, ["resume", task_data["id"]])) == 0
    capsys.readouterr()
    state = await call(client, "hand_task_status", {"task_id": task_data["id"]})
    assert state["status"] == state["stage"] == "RETURNED"
    assert state["evidence_summary"]["status"] == "simulated"
    assert state["evidence_summary"]["unresolved_count"] == 0
    assert state["evidence_summary"]["cost"]["tokens"] == 0
    evidence = (await call(client, "hand_get_evidence", {"task_id": task_data["id"]}))["evidence"]
    assert EvidencePackage.model_validate(evidence).status == "simulated"
    assert evidence == json.loads((tmp_path / "outbox/test-task/evidence.json").read_text())
    for artifact in evidence["artifacts"]:
        assert hashlib.sha256(Path(artifact["path"]).read_bytes()).hexdigest() == artifact["sha256"]
    await call(client, "hand_task_resume", {"task_id": task_data["id"]}, error="TASK_TERMINAL")


@pytest.mark.parametrize("approval_points", [[], [{"id": "inactive", "triggered": False}]])
async def test_normal_and_untriggered_tasks_match_cli(client, task_data, approval_points):
    task_data["approval_points"] = approval_points
    state = await call(client, "hand_submit_task", {"task_text": yaml.safe_dump(task_data)})
    assert state["status"] == "RETURNED"
    assert state["history"] == ["RECEIVED", "VALIDATED", "PLANNED", "RUNNING", "GATING", "RETURNED"]
    assert state["interrupts"] == []


@pytest.mark.parametrize("text, field", [
    ("id: [broken", []), ("!!python/object/apply:os.system ['false']", []),
    ("{}", ["id"]), ("[]", []), ("null", []), ("", []),
    ('{"id":"../escape"}', ["id"]),
])
async def test_invalid_packages_are_structured_and_create_no_task(client, tmp_path, text, field):
    result = await call(client, "hand_submit_task", {"task_text": text}, error="VALIDATION_ERROR")
    assert field in [item["path"] for item in result["error"]["fields"]]
    assert all("input" not in item and "ctx" not in item for item in result["error"]["fields"])
    assert not (tmp_path / "work/checkpoints.sqlite").exists()


async def test_nested_contract_fields_and_extra_approval_are_rejected(client, tmp_path, task_data):
    task_data["budget"] = {"max_minutes": -1, "max_tokens": "100"}
    task_data["approved"] = True
    result = await submit(client, task_data, error="VALIDATION_ERROR")
    paths = [field["path"] for field in result["error"]["fields"]]
    assert ["budget", "max_minutes"] in paths
    assert ["budget", "max_tokens"] in paths
    assert ["approved"] in paths
    assert not (tmp_path / "work/checkpoints.sqlite").exists()


@pytest.mark.parametrize("name, arguments, field", [
    ("hand_submit_task", {}, ["task_text"]),
    ("hand_submit_task", {"task_text": {}}, ["task_text"]),
    ("hand_task_status", {"task_id": "../outside"}, ["task_id"]),
    ("hand_task_status", {"task_id": "a\n"}, ["task_id"]),
    ("hand_get_evidence", {"task_id": 123}, ["task_id"]),
    ("hand_task_resume", {"task_id": "x", "approved": "true"}, ["approved"]),
    ("hand_task_resume", {"task_id": "x", "approved": 1}, ["approved"]),
    ("hand_task_resume", {"task_id": "x", "approval_points": ["x"]}, ["approval_points"]),
    ("hand_task_status", {"task_id": "x", "root": "/"}, ["root"]),
])
async def test_bad_tool_arguments_have_field_errors(client, name, arguments, field):
    result = await call(client, name, arguments, error="INVALID_ARGUMENTS")
    assert field in [item["path"] for item in result["error"]["fields"]]


@pytest.mark.parametrize("name", ["hand_task_status", "hand_task_resume", "hand_get_evidence"])
async def test_unknown_task_ids(client, name):
    await call(client, name, {"task_id": "unknown"}, error="TASK_NOT_FOUND")


async def test_storage_failures_are_structured(client, monkeypatch):
    def inaccessible(self, task_id):
        raise OSError("private filesystem detail")

    monkeypatch.setattr(HandRunner, "status", inaccessible)
    result = await call(client, "hand_task_status", {"task_id": "known"}, error="IO_ERROR")
    assert "private filesystem detail" not in json.dumps(result)


async def test_duplicate_submit_does_not_overwrite_checkpoint_or_evidence(client, tmp_path, task_data):
    await submit(client, task_data)
    evidence = (tmp_path / "outbox/test-task/evidence.json").read_bytes()
    task_data["goal"] = "This duplicate must not run"
    await submit(client, task_data, error="TASK_EXISTS")
    assert (tmp_path / "outbox/test-task/evidence.json").read_bytes() == evidence
    with HandRunner(tmp_path) as runner:
        assert runner.status(task_data["id"])["task"]["goal"] != task_data["goal"]


async def test_missing_and_invalid_published_evidence(client, tmp_path, task_data):
    task_data["approval_points"] = ["manual"]
    await submit(client, task_data)
    await call(client, "hand_get_evidence", {"task_id": task_data["id"]}, error="EVIDENCE_NOT_FOUND")
    directory = tmp_path / "outbox/test-task"
    directory.mkdir(parents=True)
    path = directory / "evidence.json"
    for raw in ('{"status": "verified"}', "not json"):
        path.write_text(raw)
        result = await call(client, "hand_get_evidence", {"task_id": task_data["id"]}, error="INVALID_EVIDENCE")
        assert result["error"]["fields"]
    path.unlink()
    target = tmp_path / "other.json"
    target.write_text('{}')
    path.symlink_to(target)
    await call(client, "hand_get_evidence", {"task_id": task_data["id"]}, error="INVALID_OPERATION")


async def test_path_whitelist_cannot_be_bypassed(client, tmp_path, task_data):
    task_data["deliverables"] = ["../escape.txt"]
    state = await submit(client, task_data)
    assert state["status"] == "BLOCKED"
    assert state["evidence_summary"]["unresolved_count"] == 1
    assert not (tmp_path / "work/escape.txt").exists()
    with HandRunner(tmp_path) as runner:
        assert runner.status(task_data["id"])["attempt"] == 0


async def test_gates_are_still_enforced(tmp_path, task_data):
    async with Client(create_server(tmp_path, executor=FakeExecutor(fail_attempts=99)), cache=None) as client:
        state = await submit(client, task_data)
        assert state["status"] == "BLOCKED"
        assert state["history"].count("RETRY") == 2
        evidence = (await call(client, "hand_get_evidence", {"task_id": task_data["id"]}))["evidence"]
        assert evidence["gate_reports"]["fake_artifact_check"]["passed"] is False
        assert evidence["status"] == "blocked"


@pytest.mark.parametrize("with_approval", [False, True])
@pytest.mark.parametrize("node, status, stage", [
    ("_received", "RECEIVED", "RECEIVED"), ("_planned", "VALIDATED", "PLANNED"),
])
async def test_crash_recovery_resumes_checkpoint_without_auto_approval(client, tmp_path, task_data,
                                                                      monkeypatch, with_approval, node, status, stage):
    original = getattr(HandRunner, node)
    if with_approval:
        task_data["approval_points"] = ["manual"]

    def crash(self, state):
        raise RuntimeError("private failure detail")

    monkeypatch.setattr(HandRunner, node, crash)
    result = await submit(client, task_data, error="INTERNAL_ERROR")
    assert "private failure detail" not in json.dumps(result)
    state = await call(client, "hand_task_status", {"task_id": task_data["id"]})
    assert state["status"] == status
    assert state["stage"] == stage
    monkeypatch.setattr(HandRunner, node, original)
    state = await call(client, "hand_task_resume", {"task_id": task_data["id"], "approved": True})
    assert state["status"] == ("WAITING_APPROVAL" if with_approval else "RETURNED")
    if with_approval:
        assert not (tmp_path / "work/test-task/demo.txt").exists()
    else:
        evidence = (await call(client, "hand_get_evidence", {"task_id": task_data["id"]}))["evidence"]
        assert EvidencePackage.model_validate(evidence).status == "simulated"


async def test_recovery_status_race_cannot_use_cli_default_approval(client, tmp_path, task_data, monkeypatch):
    task_data["approval_points"] = ["manual"]
    await submit(client, task_data)
    original = HandRunner.status
    seen = False

    def stale_status(self, task_id):
        nonlocal seen
        state = original(self, task_id)
        if not seen:
            seen = True
            return {**state, "interrupts": []}
        return state

    monkeypatch.setattr(HandRunner, "status", stale_status)
    # The outer status misses the pause; resume's locked status sees it.
    state = await call(client, "hand_task_resume", {"task_id": task_data["id"], "approved": True})
    assert state["status"] == "BLOCKED"
    assert not (tmp_path / "work/test-task/demo.txt").exists()
    with HandRunner(tmp_path) as runner:
        assert runner.status(task_data["id"])["attempt"] == 0


async def test_parallel_calls_isolate_tasks_and_reject_same_task_overlap(tmp_path, task_data):
    entered, release = Event(), Event()

    class PausingExecutor(FakeExecutor):
        def execute(self, task, *args, **kwargs):
            if task.id == "test-task":
                entered.set()
                if not release.wait(10):
                    raise RuntimeError("test release timed out")
            return super().execute(task, *args, **kwargs)

    async with Client(create_server(tmp_path, executor=PausingExecutor()), cache=None) as client:
        results = []

        async def first_submit():
            results.append(await submit(client, task_data))

        async with anyio.create_task_group() as group:
            group.start_soon(first_submit)
            try:
                assert await anyio.to_thread.run_sync(entered.wait, 5)
                with anyio.fail_after(5):
                    state = await call(client, "hand_task_status", {"task_id": "test-task"})
                    assert state["task_id"] == "test-task"
                    assert state["stage"] == "RUNNING"
                    await submit(client, task_data, error="TASK_BUSY")
                    await call(client, "hand_task_resume", {"task_id": "test-task"}, error="TASK_BUSY")
                    other = {**task_data, "id": "other-task", "goal": "Independent checkpoint"}
                    assert (await submit(client, other))["status"] == "RETURNED"
            finally:
                release.set()
        assert results[0]["status"] == "RETURNED"
        for task_id in ("test-task", "other-task"):
            evidence = (await call(client, "hand_get_evidence", {"task_id": task_id}))["evidence"]
            assert f"/{task_id}/" in evidence["artifacts"][0]["path"]
            with HandRunner(tmp_path) as runner:
                assert runner.status(task_id)["task"]["id"] == task_id


async def test_parallel_first_submissions_share_sqlite_without_crossing_tasks(tmp_path, task_data):
    async with Client(create_server(tmp_path), cache=None) as client:
        results = {}

        async def run(index):
            data = {**task_data, "id": f"parallel-{index}"}
            results[index] = await submit(client, data)

        with anyio.fail_after(20):
            async with anyio.create_task_group() as group:
                for index in range(4):
                    group.start_soon(run, index)
        assert len(results) == 4
        for index, state in results.items():
            assert state["status"] == "RETURNED"
            assert state["task_id"] == f"parallel-{index}"


async def test_checkpoint_survives_new_server_instance(tmp_path, task_data):
    task_data["approval_points"] = ["manual"]
    async with Client(create_server(tmp_path), cache=None) as first:
        await submit(first, task_data)
    async with Client(create_server(tmp_path), cache=None) as second:
        assert (await call(second, "hand_task_status", {"task_id": "test-task"}))["status"] == "WAITING_APPROVAL"
        await submit(second, task_data, error="TASK_EXISTS")
        assert (await call(second, "hand_task_resume", {"task_id": "test-task", "approved": False}))["status"] == "BLOCKED"


def test_server_has_no_telemetry_middleware_or_listening_transport(tmp_path):
    server = create_server(tmp_path)
    assert server.middleware == []
    assert os.environ["OTEL_SDK_DISABLED"] == "true"
    assert os.environ["LANGGRAPH_CLI_NO_ANALYTICS"] == "1"
    assert os.environ["LANGSMITH_TRACING"] == "false"
    assert os.environ["LANGCHAIN_TRACING_V2"] == "false"
    assert not (tmp_path / "work").exists()


def test_cli_registers_stdio_entry_without_opening_cli_runner(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    seen = []
    monkeypatch.setattr("hand.mcp_server.serve", lambda root: seen.append(root))

    def unexpected_runner(*args, **kwargs):
        raise AssertionError("CLI must not hold a runner while serving MCP")

    monkeypatch.setattr(cli, "HandRunner", unexpected_runner)
    assert cli.main(["mcp-serve"]) == 0
    assert seen == [tmp_path]
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_real_stdio_cli_submit_status_resume_evidence_and_restart(tmp_path, task_data, mode):
    source = Path(__file__).resolve().parents[1] / "src"
    # Redirect only the installation root in this subprocess, keeping the real
    # CLI parser, mcp-serve branch, SDK stdio transport and executor unchanged.
    bootstrap = (
        "import sys; from pathlib import Path; from hand import cli; "
        "cli.PROJECT_ROOT = Path(sys.argv[1]); raise SystemExit(cli.main(['mcp-serve']))"
    )
    temporary = tmp_path / "tmp"
    temporary.mkdir()
    parameters = StdioServerParameters(
        command=sys.executable, args=["-B", "-c", bootstrap, str(tmp_path)],
        cwd=str(tmp_path), env={
            **os.environ, "PYTHONPATH": str(source), "PYTHONDONTWRITEBYTECODE": "1", "TMPDIR": str(temporary),
        },
    )
    task_data["approval_points"] = ["stdio-manual"]
    with anyio.fail_after(30):
        async with Client(parameters, mode=mode, cache=None) as client:
            assert {tool.name for tool in (await client.list_tools()).tools} == TOOL_NAMES
            assert (await submit(client, task_data))["status"] == "WAITING_APPROVAL"
            assert (await call(client, "hand_task_status", {"task_id": "test-task"}))["stage"] == "WAITING_APPROVAL"
            await call(client, "hand_task_resume", {"task_id": "test-task", "approved": True}, error="APPROVAL_REQUIRED")
            assert (await call(client, "hand_task_resume", {"task_id": "test-task", "approved": False}))["status"] == "BLOCKED"
            evidence = (await call(client, "hand_get_evidence", {"task_id": "test-task"}))["evidence"]
            assert EvidencePackage.model_validate(evidence).status == "blocked"
            normal = {**task_data, "id": "stdio-success", "approval_points": []}
            assert (await submit(client, normal))["status"] == "RETURNED"
            assert (await call(client, "hand_get_evidence", {"task_id": "stdio-success"}))["evidence"]["status"] == "simulated"
        async with Client(parameters, mode=mode, cache=None) as client:
            state = await call(client, "hand_task_status", {"task_id": "test-task"})
            assert state["status"] == "BLOCKED"
            await submit(client, task_data, error="TASK_EXISTS")
            await call(client, "hand_task_status", {"task_id": "unknown"}, error="TASK_NOT_FOUND")
