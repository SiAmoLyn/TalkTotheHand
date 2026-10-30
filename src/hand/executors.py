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


def _opencode_config_model() -> str | None:
    """Last-resort model: the ``model`` field of the operator's OpenCode config.

    Looks at $XDG_CONFIG_HOME/opencode/opencode.json first, then
    ~/.config/opencode/opencode.json. A missing or malformed file simply
    means "not configured"; the caller keeps its original error.
    """
    candidates = []
    config_home = os.environ.get("XDG_CONFIG_HOME")
    if config_home:
        candidates.append(Path(config_home) / "opencode" / "opencode.json")
    candidates.append(Path.home() / ".config" / "opencode" / "opencode.json")
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(payload, dict):
            value = payload.get("model")
            if isinstance(value, str) and value.strip():
                return value
    return None


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
        model = task.opencode.model or os.environ.get("HAND_OPENCODE_MODEL") or _opencode_config_model()
        fixture = self.fixture or task.opencode.fixture
        replay = fixture is not None
        recovered = None
        # Task ledger for this workspace (R2-01/02/03/05/11). The persisted
        # session file is the cumulative ledger: totals plus the ids of the
        # step-finish events already counted, whether usage was ever
        # unknown, how much active time is still unsettled, and the
        # settlement baseline — the totals a previous execute() already
        # returned as evidence. Evidence from this call reports only the
        # unsettled delta (cumulative minus baseline), so graph retries add
        # it exactly once; a crashed attempt never reached its final
        # persist, so its spend stays unsettled and is reported by the
        # resumed run instead of being lost (audit A05) or doubled.
        # A ledger written by a fixture replay, or for another model, is
        # synthetic/foreign usage and is never seeded into a real run.
        settled_tokens = 0
        settled_cost = 0.0
        pending_minutes = 0.0
        if not replay and session_path.is_file():
            try:
                stored = json.loads(session_path.read_text(encoding="utf-8"))
                if isinstance(stored, dict) and not stored.get("fixture_replay") \
                        and stored.get("model") == model:
                    seed_tokens = stored.get("tokens")
                    if isinstance(seed_tokens, int) and not isinstance(seed_tokens, bool) and seed_tokens > 0:
                        parser.tokens = seed_tokens
                    seed_cost = stored.get("reported_cost")
                    if isinstance(seed_cost, (int, float)) and not isinstance(seed_cost, bool) and seed_cost > 0:
                        parser.reported_cost = float(seed_cost)
                    accounted = stored.get("accounted_ids")
                    if isinstance(accounted, list):
                        parser._accounted = {item for item in accounted
                                             if isinstance(item, (str, int)) and not isinstance(item, bool)}
                    if stored.get("usage_missing") is True:
                        # Unknown usage survives the crash; the resumed run
                        # must stay blocked until it can be reconciled.
                        parser.usage_missing = True
                    baseline = stored.get("settled_tokens")
                    if isinstance(baseline, int) and not isinstance(baseline, bool) and baseline >= 0:
                        settled_tokens = min(baseline, parser.tokens)
                    baseline_cost = stored.get("settled_cost")
                    if isinstance(baseline_cost, (int, float)) and not isinstance(baseline_cost, bool) \
                            and baseline_cost >= 0:
                        settled_cost = min(float(baseline_cost), parser.reported_cost)
                    unsettled = stored.get("pending_minutes")
                    if isinstance(unsettled, (int, float)) and not isinstance(unsettled, bool) and unsettled > 0:
                        pending_minutes = float(unsettled)
                    value = stored.get("session_id")
                    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]+", value):
                        recovered = value
            except (OSError, ValueError, AttributeError):
                # Never trust an invalid recovery file as a session or ledger.
                pass

        def persist_session(*, final: bool) -> None:
            if not parser.session_id:
                return
            payload = {"session_id": parser.session_id, "model": model,
                       "fixture_replay": replay, "tokens": parser.tokens,
                       "reported_cost": parser.reported_cost,
                       "accounted_ids": sorted(parser._accounted, key=str),
                       "usage_missing": parser.usage_missing,
                       "settled_tokens": parser.tokens if final else settled_tokens,
                       "settled_cost": parser.reported_cost if final else settled_cost,
                       "pending_minutes": 0.0 if final else
                       pending_minutes + (time.monotonic() - started) / 60}
            try:
                session_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            except OSError:
                pass

        def consume(line: bytes) -> None:
            parser.consume(line)
            # Checkpoint the ledger per event, exactly like the token
            # spend: a crash after this line loses nothing already counted.
            persist_session(final=False)

        def unsettled_tokens() -> int:
            # Budget checks and evidence use this call's unsettled delta,
            # never the cumulative ledger total (R2-01).
            return parser.tokens - settled_tokens

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
                        if unsettled_tokens() > task.budget.max_tokens:
                            errors.append("token budget exceeded")
                            break
                model = "opencode-fixture (synthetic; no model called)"
            else:
                if not model:
                    raise ValueError("OpenCode model not configured: set opencode.model, "
                                     "HAND_OPENCODE_MODEL, or the model field in opencode.json")
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
                                     stop_reason=lambda: "token budget exceeded" if unsettled_tokens() > task.budget.max_tokens else None)
                if result["reason"] or result["exit_code"] != 0:
                    errors.append(str(result["reason"] or f"OpenCode exit {result['exit_code']}"))
                    errors.append(stderr.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError) as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
        errors.extend(parser.errors)
        if not replay and parser.usage_missing:
            errors.append("OpenCode step_finish missing token usage; cost accounting unavailable")
        if not parser.finished:
            errors.append("OpenCode stream ended without a terminal step_finish")
        if not parser.session_id:
            errors.append("OpenCode stream has no session id")
        if unsettled_tokens() > task.budget.max_tokens:
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
        # This execute() is about to report its delta as evidence: settle
        # the ledger so the next call only reports what is still unsettled.
        persist_session(final=True)
        return EvidencePackage(
            status="blocked" if errors else "in_progress", artifacts=artifacts, gate_reports={},
            logs=[str(raw), str(stderr), str(parsed_path), str(config_path),
                  f"OpenCode session: {parser.session_id}", *errors],
            cost=Cost(tokens=unsettled_tokens(),
                      minutes=pending_minutes + (time.monotonic() - started) / 60,
                      model=model or "unconfigured"), unresolved=errors,
            self_check="Fixture pipeline only; no model/tool execution" if replay else "OpenCode candidate; independent gates and Sisi acceptance required",
        )
