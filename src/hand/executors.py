"""Executor/model boundaries. The default implementation is entirely fake/offline."""

from abc import ABC, abstractmethod
import hashlib
import json
import os
import re
from threading import Event
import time
import uuid

from .opencode import EventParser, permission_config
from .processes import run_process
from pathlib import Path

from .contracts import Artifact, Cost, EvidencePackage, TaskPackage
from .tools import fs_guard


class LLM(ABC):
    @abstractmethod
    def plan(self, task: TaskPackage) -> str:
        """Return a plan; any future model integration lives behind this interface."""


class FakeLLM(LLM):
    def plan(self, task: TaskPackage) -> str:
        return f"FakeLLM: {task.tool_class.value} line; create {len(task.deliverables)} fake artifact(s), then gate."


class Executor(ABC):
    @abstractmethod
    def execute(self, task: TaskPackage, workspace: Path, *, attempt: int,
                allowed_directories: list[Path]) -> EvidencePackage:
        """Execute one attempt and return schema-valid candidate evidence."""


class FakeExecutor(Executor):
    """Produces explicitly labelled fake outputs, never authentic media/tool results."""

    def __init__(self, *, fail_attempts: int = 0):
        self.fail_attempts = fail_attempts

    def execute(self, task: TaskPackage, workspace: Path, *, attempt: int,
                allowed_directories: list[Path]) -> EvidencePackage:
        artifacts = []
        for deliverable in task.deliverables:
            target = fs_guard(deliverable.path, workspace=workspace, allowed_directories=allowed_directories)
            target.parent.mkdir(parents=True, exist_ok=True)
            content = f"FAKE ARTIFACT — Package 1 only\nTask: {task.id}\nType: {deliverable.type}\nGoal: {task.goal}\n"
            target.write_text(content, encoding="utf-8")
            artifacts.append(Artifact(path=str(target), sha256=hashlib.sha256(target.read_bytes()).hexdigest()))
        return EvidencePackage(
            status="in_progress", artifacts=artifacts,
            gate_reports={name: {"passed": attempt > self.fail_attempts, "fake": True,
                                 "attempt": attempt, "detail": "Simulated gate; no real acceptance performed"}
                          for name in task.gates},
            logs=[f"FakeExecutor attempt {attempt}; no external tools or models called"],
            cost=Cost(tokens=0, minutes=0, model="FakeLLM"), unresolved=[],
            self_check="Fake artifacts and fake gates only. Sisi retains real acceptance authority.",
        )


class OpenCodeExecutor(Executor):
    """Real CLI or fixture replay, sharing one byte-preserving parser path.

    Fixture replay never writes declared deliverables: pre-stage honest fixture
    assets just as a real execution must produce its own outputs.
    """
    def __init__(self, *, executable: str | None = None, fixture: Path | None = None,
                 cancel: Event | None = None):
        self.executable = executable or os.environ.get("HAND_OPENCODE", "opencode")
        self.fixture = fixture
        self.cancel_event = cancel or Event()
        self.retry_feedback = ""

    def execute(self, task: TaskPackage, workspace: Path, *, attempt: int,
                allowed_directories: list[Path]) -> EvidencePackage:
        started = time.monotonic()
        directory = fs_guard(f".hand/opencode/attempt-{attempt}-{uuid.uuid4().hex[:12]}", workspace=workspace)
        directory.mkdir(parents=True, exist_ok=True)
        raw = fs_guard(directory / "events.jsonl", workspace=workspace)
        stderr = fs_guard(directory / "stderr.log", workspace=workspace)
        parsed_path = fs_guard(directory / "parsed.json", workspace=workspace)
        config_path = fs_guard(directory / "opencode.json", workspace=workspace)
        config = permission_config(workspace, allowed_directories)
        config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        parser = EventParser()
        session_path = fs_guard(".hand/opencode/session.json", workspace=workspace)
        errors, result = [], {}
        model = task.opencode.model or os.environ.get("HAND_OPENCODE_MODEL")
        fixture = self.fixture or task.opencode.fixture
        replay = fixture is not None
        recovered = None
        if not replay and session_path.is_file():
            try:
                stored = json.loads(session_path.read_text(encoding="utf-8"))
                if not stored.get("fixture_replay") and stored.get("model") == model:
                    value = stored.get("session_id")
                    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]+", value):
                        recovered = value
            except (OSError, ValueError, AttributeError):
                # Never trust an invalid recovery file as a session argument.
                pass

        def consume(line: bytes) -> None:
            parser.consume(line)
            if parser.session_id:
                session_path.write_text(json.dumps({"session_id": parser.session_id,
                                                    "model": model, "fixture_replay": replay}) + "\n", encoding="utf-8")

        raw.write_bytes(b"")
        stderr.write_bytes(b"")
        try:
            if replay:
                source = fs_guard(fixture, workspace=workspace, allowed_directories=allowed_directories)
                # Same consume method and exact raw bytes as the subprocess path.
                with source.open("rb") as stream, raw.open("wb") as destination:
                    for line in stream:
                        if self.cancel_event.is_set():
                            errors.append("cancelled")
                            break
                        if time.monotonic() - started >= task.budget.max_minutes * 60:
                            errors.append("timeout")
                            break
                        destination.write(line)
                        consume(line)
                        if parser.tokens > task.budget.max_tokens:
                            errors.append("token budget exceeded")
                            break
                model = "opencode-fixture (synthetic; no model called)"
            else:
                if not model:
                    raise ValueError("OpenCode model not configured: set opencode.model or HAND_OPENCODE_MODEL")
                if not re.fullmatch(r"[^/\s]+/[^\s]+", model):
                    raise ValueError("OpenCode model must be provider/model")
                argv = [self.executable, "run", "--format", "json", "--dir", str(workspace), "-m", model]
                if task.opencode.session_id:
                    argv.extend(["--session", task.opencode.session_id])
                elif task.opencode.continue_session:
                    argv.append("--continue")
                elif recovered:
                    argv.extend(["--session", recovered])
                prompt = {"task": task.model_dump(mode="json"), "retry_feedback": self.retry_feedback,
                          "note": "Produce declared deliverables. Gates are independently run by HAND. Shell is disabled."}
                argv.extend(["--", json.dumps(prompt, ensure_ascii=False)])
                # Isolate config/plugins/cache/temp and omit inherited keys or auth.
                # A subscription may be provisioned by the operator in this isolated
                # HOME; this package does not copy or read host credentials.
                runtime = fs_guard(".hand/runtime", workspace=workspace)
                runtime.mkdir(parents=True, exist_ok=True)
                env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "SYSTEMROOT") if key in os.environ}
                for key, folder in (("HOME", "home"), ("XDG_CONFIG_HOME", "config"),
                                    ("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache"),
                                    ("XDG_STATE_HOME", "state"), ("TMPDIR", "tmp")):
                    path = fs_guard(runtime / folder, workspace=workspace)
                    path.mkdir(parents=True, exist_ok=True)
                    env[key] = str(path)
                env.update(OPENCODE_CONFIG=str(config_path), OPENCODE_CONFIG_CONTENT=json.dumps(config),
                           OPENCODE_DISABLE_AUTOUPDATE="1", OPENCODE_DISABLE_LSP_DOWNLOAD="1",
                           OPENCODE_DISABLE_DEFAULT_PLUGINS="1", OPENCODE_DISABLE_PROJECT_CONFIG="1",
                           LANGGRAPH_CLI_NO_ANALYTICS="1", LANGSMITH_TRACING="false", LANGCHAIN_TRACING_V2="false")
                remaining = task.budget.max_minutes * 60 - (time.monotonic() - started)
                if remaining <= 0:
                    raise TimeoutError("timeout before OpenCode launch")
                result = run_process(argv, cwd=workspace, stdout=raw, stderr=stderr,
                                     timeout=remaining, cancel=self.cancel_event,
                                     on_line=consume, env=env,
                                     stop_reason=lambda: "token budget exceeded" if parser.tokens > task.budget.max_tokens else None)
                if result["reason"] or result["exit_code"] != 0:
                    errors.append(str(result["reason"] or f"OpenCode exit {result['exit_code']}"))
                    errors.append(stderr.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError) as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
        errors.extend(parser.errors)
        if not parser.finished:
            errors.append("OpenCode stream ended without a terminal step_finish")
        if not parser.session_id:
            errors.append("OpenCode stream has no session id")
        if parser.tokens > task.budget.max_tokens:
            errors.append("token budget exceeded")
        summary = {**parser.summary(), "fixture_replay": replay, "process": result, "errors": errors}
        artifacts = []
        for item in task.deliverables:
            try:
                path = fs_guard(item.path, workspace=workspace, allowed_directories=allowed_directories)
                artifacts.append(Artifact(path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
            except (OSError, ValueError) as exc:
                errors.append(f"Deliverable {item.path}: {exc}")
        parsed_path.write_text(json.dumps(summary, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        return EvidencePackage(
            status="blocked" if errors else "in_progress", artifacts=artifacts, gate_reports={},
            logs=[str(raw), str(stderr), str(parsed_path), str(config_path),
                  f"OpenCode session: {parser.session_id}", *errors],
            cost=Cost(tokens=parser.tokens, minutes=(time.monotonic() - started) / 60,
                      model=model or "unconfigured"), unresolved=errors,
            self_check="Fixture pipeline only; no model/tool execution" if replay else "OpenCode candidate; independent gates and Sisi acceptance required",
        )
