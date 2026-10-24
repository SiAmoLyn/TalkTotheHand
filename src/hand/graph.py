"""Durable task lifecycle. Only schema-valid, gated evidence can be RETURNED."""

from contextlib import contextmanager
import fcntl
import hashlib
import json
import signal
from threading import Event, current_thread, main_thread
from pathlib import Path
import time
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from .checkpoints import SQLiteCheckpointer
from .contracts import Budget, EvidencePackage, EvidenceStatus, TaskPackage
from .executors import Executor, FakeExecutor, FakeLLM, LLM, OpenCodeExecutor
from .tools import TOOL_REGISTRY, fs_guard, git_snapshot, project_path


class HandState(TypedDict, total=False):
    task: dict[str, Any]
    status: str
    history: list[str]
    logs: list[str]
    retries: int
    attempt: int
    reason: str
    plan: str
    candidate: dict[str, Any]
    evidence: dict[str, Any]
    active_minutes: float
    tokens: int
    approvals: list[str]
    terminal_failure: bool
    retry_feedback: str
    cost_model: str


class HandRunner:
    def __init__(self, root: Path, *, executor: Executor | None = None, llm: LLM | None = None):
        self.root = root.resolve()
        self.executor = executor
        self.cancel_event = Event()
        self.llm = llm or FakeLLM()
        self.saver = SQLiteCheckpointer(project_path(self.root, "work/checkpoints.sqlite"))
        builder = StateGraph(HandState)
        for name, node in (
            ("RECEIVED", self._received), ("VALIDATED", self._validated),
            ("PLANNED", self._planned), ("WAITING_APPROVAL", self._approval),
            ("RUNNING", self._running), ("GATING", self._gating),
            ("RETRY", self._retry), ("BLOCKED", self._blocked), ("RETURNED", self._returned),
        ):
            builder.add_node(name, node)
        builder.add_edge(START, "RECEIVED")
        builder.add_edge("RECEIVED", "VALIDATED")
        builder.add_conditional_edges("VALIDATED", lambda s: "BLOCKED" if s.get("reason") else "PLANNED")
        builder.add_conditional_edges("PLANNED", lambda s: "BLOCKED" if s.get("reason") else (
            "WAITING_APPROVAL" if s["status"] == "WAITING_APPROVAL" else "RUNNING"))
        builder.add_conditional_edges("WAITING_APPROVAL", lambda s: "BLOCKED" if s.get("reason") else "RUNNING")
        builder.add_conditional_edges("RUNNING", self._after_attempt)
        builder.add_conditional_edges("GATING", lambda s: self._after_attempt(s) if s.get("reason") else "RETURNED")
        builder.add_edge("RETRY", "RUNNING")
        builder.add_edge("BLOCKED", END)
        builder.add_edge("RETURNED", END)
        self.graph = builder.compile(checkpointer=self.saver)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.saver.close()

    @staticmethod
    def _config(task_id: str) -> dict[str, Any]:
        # Validate before using any caller-controlled task ID as a filesystem path.
        import re
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", task_id):
            raise ValueError("Invalid task id")
        return {"configurable": {"thread_id": task_id}, "recursion_limit": 60}

    @contextmanager
    def _task_lock(self, task_id: str):
        path = project_path(self.root, f"work/.{task_id}.lock")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError(f"Task {task_id} is already running") from exc
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def cancel(self) -> None:
        """Thread-safe cancellation; CLI signals use the same event."""
        self.cancel_event.set()

    @contextmanager
    def _signals(self):
        previous = {}
        if current_thread() is main_thread():
            for number in (signal.SIGINT, signal.SIGTERM):
                previous[number] = signal.signal(number, lambda *_: self.cancel())
        try:
            yield
        finally:
            for number, handler in previous.items():
                signal.signal(number, handler)

    def run(self, task: TaskPackage) -> HandState:
        config = self._config(task.id)
        with self._task_lock(task.id), self._signals():
            if self.saver.get_tuple(config) is not None:
                raise ValueError(f"Task {task.id} already exists; use hand status/resume or a new id")
            # durability="sync": LangGraph's default async checkpoint flush
            # lets node execution outrun persistence, so a concurrent
            # status() can read a stale prefix (initial checkpoint only, or
            # none) while a task is already RUNNING. Sync durability keeps
            # the durable-state contract the status/resume paths rely on.
            return self.graph.invoke({"task": task.model_dump(mode="json"), "history": [],
                                      "logs": [], "attempt": 0, "retries": 0,
                                      "active_minutes": 0.0, "tokens": 0, "reason": ""}, config,
                                     durability="sync")

    def status(self, task_id: str) -> dict[str, Any]:
        snapshot = self.graph.get_state(self._config(task_id))
        if not snapshot.values:
            raise ValueError(f"Unknown task: {task_id}")
        result = dict(snapshot.values)
        result["next"] = list(snapshot.next)
        result["interrupts"] = [item.value for task in snapshot.tasks for item in task.interrupts]
        return result

    def resume(self, task_id: str, *, approved: bool = True) -> HandState:
        config = self._config(task_id)
        with self._task_lock(task_id), self._signals():
            current = self.status(task_id)
            if not current["next"]:
                raise ValueError(f"Task {task_id} is terminal ({current['status']}); cannot resume")
            if current["interrupts"]:
                return self.graph.invoke(Command(resume={"approved": approved}), config,
                                         durability="sync")
            # Recover an interrupted process from its last durable checkpoint.
            return self.graph.invoke(None, config, durability="sync")

    def _paths(self, task: TaskPackage) -> tuple[Path, list[Path]]:
        workspace = project_path(self.root, f"work/{task.id}")
        reserved = {"src", "tests", ".git", ".venv", ".codex", ".agents", ".aws", "outbox", "inbox"}
        allowed = []
        for declared in task.allowed_directories:
            path = project_path(self.root, declared)
            relative = path.relative_to(self.root)
            if not relative.parts or relative.parts[0] in reserved or relative.parts[0] == "work":
                raise ValueError(f"Declared directory is not a project-copy directory: {declared}")
            allowed.append(path)
        return workspace, allowed

    @staticmethod
    def _stage(state: HandState, stage: str, **updates: Any) -> HandState:
        return {"status": stage, "history": [*state["history"], stage], **updates}

    def _received(self, state: HandState) -> HandState:
        return self._stage(state, "RECEIVED")

    def _validated(self, state: HandState) -> HandState:
        try:
            task = TaskPackage.model_validate(state["task"])
            workspace, allowed = self._paths(task)
            targets = [fs_guard(item.path, workspace=workspace, allowed_directories=allowed) for item in task.deliverables]
            if len(set(targets)) != len(targets) or any(target == workspace or target in allowed for target in targets):
                raise ValueError("Deliverables must have distinct file paths, not workspace/directory roots")
            for context in task.context_files:
                path = fs_guard(context, workspace=workspace, allowed_directories=allowed)
                if not path.is_file():
                    raise ValueError(f"Context file does not exist: {context}")
            if task.executor_hint not in (None, "fake", "opencode", "opencode-fixture"):
                raise ValueError(f"Unknown executor_hint: {task.executor_hint}")
            workspace.mkdir(parents=True, exist_ok=True)
            return self._stage(state, "VALIDATED", reason="")
        except Exception as exc:
            return self._stage(state, "VALIDATED", reason=f"Validation: {exc}")

    def _planned(self, state: HandState) -> HandState:
        try:
            task = TaskPackage.model_validate(state["task"])
            plan = self.llm.plan(task)
            update = self._stage(state, "PLANNED", plan=plan)
            if any(point.triggered for point in task.approval_points):
                update["status"] = "WAITING_APPROVAL"
                update["history"].append("WAITING_APPROVAL")
            return update
        except Exception as exc:
            return self._stage(state, "PLANNED", reason=f"Planning: {exc}")

    def _approval(self, state: HandState) -> HandState:
        task = TaskPackage.model_validate(state["task"])
        points = [point.model_dump() for point in task.approval_points if point.triggered]
        # Keep interrupt outside exception handlers. It deliberately suspends execution.
        response = interrupt({"task_id": task.id, "approval_points": points,
                              "message": "hand resume <task_id> explicitly approves these triggered points"})
        if not isinstance(response, dict) or response.get("approved") is not True:
            return {"reason": "Approval denied; executor was not called"}
        return {"approvals": [point["id"] for point in points], "reason": ""}

    @staticmethod
    def _after_attempt(state: HandState) -> str:
        if state.get("reason"):
            return "RETRY" if state["retries"] < 2 and not state.get("terminal_failure") else "BLOCKED"
        return "GATING"

    def _running(self, state: HandState) -> HandState:
        task = TaskPackage.model_validate(state["task"])
        workspace, allowed = self._paths(task)
        logs = list(state["logs"])
        if state["attempt"] == 0:
            logs.append(git_snapshot(workspace).log)
        attempt = state["attempt"] + 1
        start = time.monotonic()
        tokens = state["tokens"]
        minutes = state["active_minutes"]
        candidate = {}
        terminal = False
        cost_model = state.get("cost_model", "unconfigured")
        try:
            if state["active_minutes"] >= task.budget.max_minutes or tokens >= task.budget.max_tokens:
                raise ValueError("Task budget exhausted before execution")
            if self.cancel_event.is_set():
                terminal = True
                raise ValueError("cancelled")
            executor = self.executor or (OpenCodeExecutor(cancel=self.cancel_event)
                                         if task.executor_hint in ("opencode", "opencode-fixture") else FakeExecutor())
            execution_task = task
            if isinstance(executor, OpenCodeExecutor):
                executor.cancel_event = self.cancel_event
                executor.retry_feedback = state.get("retry_feedback", "")
                if task.executor_hint == "opencode-fixture" and not (task.opencode.fixture or executor.fixture):
                    terminal = True
                    raise ValueError("opencode-fixture requires a declared in-workspace fixture")
                execution_task = task.model_copy(update={"budget": Budget(
                    max_minutes=task.budget.max_minutes - minutes, max_tokens=task.budget.max_tokens - tokens)})
            raw = executor.execute(execution_task, workspace, attempt=attempt, allowed_directories=allowed)
            evidence = EvidencePackage.model_validate(raw)
            tokens += evidence.cost.tokens
            cost_model = evidence.cost.model
            logs.extend(evidence.logs)
            candidate = evidence.model_dump(mode="json")
            minutes = state["active_minutes"] + max(evidence.cost.minutes, (time.monotonic() - start) / 60)
            reason = ""
            if evidence.status == EvidenceStatus.BLOCKED:
                terminal = True
                reason = "Executor blocked: " + "; ".join(evidence.unresolved)
            elif tokens > task.budget.max_tokens or minutes > task.budget.max_minutes:
                raise ValueError("Task budget exceeded")
        except Exception as exc:
            minutes = max(minutes, state["active_minutes"] + (time.monotonic() - start) / 60)
            reason = f"Executor attempt {attempt}: {exc}"
        return self._stage(state, "RUNNING", attempt=attempt, logs=logs,
                           candidate=candidate, tokens=tokens, active_minutes=minutes, reason=reason, cost_model=cost_model,
                           terminal_failure=terminal or self.cancel_event.is_set() or
                           minutes >= task.budget.max_minutes or tokens >= task.budget.max_tokens)

    def _gating(self, state: HandState, *, run_tools: bool = True) -> HandState:
        updates = {}
        started = time.monotonic()
        try:
            task = TaskPackage.model_validate(state["task"])
            evidence = EvidencePackage.model_validate(state["candidate"])
            workspace, allowed = self._paths(task)
            if self.cancel_event.is_set():
                raise ValueError("cancelled")
            if run_tools:
                real_tools = {"gate_aabb", "gate_clipsim", "godot_run", "blender_run"}
                for name in task.gates:
                    if self.cancel_event.is_set():
                        raise ValueError("cancelled")
                    if name in real_tools:
                        options = dict(task.gate_options.get(name, {}))
                        remaining = (task.budget.max_minutes - state["active_minutes"]) * 60 - (time.monotonic() - started)
                        if remaining <= 0:
                            raise ValueError("Task budget exhausted during GATING")
                        report_path = fs_guard(f".hand/gates/attempt-{state['attempt']}/{name}.json", workspace=workspace)
                        try:
                            if {"workspace", "allowed_directories", "result_path", "cancel"}.intersection(options):
                                raise ValueError(f"Reserved gate options: {name}")
                            if name != "gate_aabb":
                                options["timeout"] = min(float(options.get("timeout", remaining)), remaining)
                                options["cancel"] = self.cancel_event
                            report = TOOL_REGISTRY[name](workspace=workspace, allowed_directories=allowed,
                                                         result_path=report_path, **options)
                        except (TypeError, ValueError, OSError) as exc:
                            report = {"status": "blocked", "passed": False, "error": str(exc),
                                      "report_path": str(report_path)}
                            report_path.parent.mkdir(parents=True, exist_ok=True)
                            report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
                        evidence.gate_reports[name] = report
                        evidence.logs.append(str(report_path))
                    elif task.executor_hint in ("opencode", "opencode-fixture") or isinstance(self.executor, OpenCodeExecutor):
                        evidence.gate_reports[name] = {"status": "blocked", "passed": False,
                                                       "error": f"Gate not implemented: {name}"}
                    updates["candidate"] = evidence.model_dump(mode="json")
                    updates["logs"] = [*state["logs"], *[str(r["report_path"]) for r in evidence.gate_reports.values()
                                                          if isinstance(r, dict) and "report_path" in r]]
            expected = {fs_guard(item.path, workspace=workspace, allowed_directories=allowed) for item in task.deliverables}
            seen = set()
            for artifact in evidence.artifacts:
                path = fs_guard(artifact.path, workspace=workspace, allowed_directories=allowed)
                if path in seen:
                    raise ValueError(f"Duplicate artifact: {path}")
                seen.add(path)
                if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != artifact.sha256:
                    raise ValueError(f"Missing artifact or sha256 mismatch: {path}")
            if seen != expected:
                raise ValueError("Artifacts do not exactly match declared deliverables")
            failed = [gate for gate in task.gates if not isinstance(evidence.gate_reports.get(gate), dict)
                      or evidence.gate_reports[gate].get("passed") is not True]
            if failed:
                raise ValueError("Gates failed/missing: " + "; ".join(
                    f"{name}: {json.dumps(evidence.gate_reports.get(name), ensure_ascii=False)}" for name in failed))
            if evidence.unresolved or evidence.status == "blocked":
                raise ValueError("Executor reported unresolved/blocked work: " + "; ".join(evidence.unresolved))
            elapsed = (time.monotonic() - started) / 60 if run_tools else 0
            if state["active_minutes"] + elapsed > task.budget.max_minutes:
                raise ValueError("Task budget exceeded during GATING")
            return self._stage(state, "GATING", reason="", active_minutes=state["active_minutes"] + elapsed, **updates)
        except Exception as exc:
            return self._stage(state, "GATING", reason=f"Gating attempt {state['attempt']}: {exc}",
                               active_minutes=state["active_minutes"] + ((time.monotonic() - started) / 60 if run_tools else 0),
                               terminal_failure=self.cancel_event.is_set() or "budget" in str(exc).lower() or
                               state.get("terminal_failure", False), **updates)

    def _retry(self, state: HandState) -> HandState:
        return self._stage(state, "RETRY", retries=state["retries"] + 1,
                           logs=[*state["logs"], f"RETRY {state['retries'] + 1}/2: {state['reason']}"],
                           retry_feedback=state["reason"], reason="")

    def _write_evidence(self, task_id: str, evidence: EvidencePackage) -> dict[str, Any]:
        target = project_path(self.root, f"outbox/{task_id}/evidence.json")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = project_path(self.root, f"outbox/{task_id}/.evidence.tmp")
        temporary.write_text(evidence.model_dump_json(indent=2) + "\n", encoding="utf-8")
        temporary.replace(target)
        return evidence.model_dump(mode="json")

    def _returned(self, state: HandState) -> HandState:
        # Revalidate the candidate and gates immediately before publishing evidence.
        gated = self._gating(state, run_tools=False)
        if gated.get("reason"):
            return self._blocked({**state, "reason": gated["reason"]})
        evidence = EvidencePackage.model_validate(state["candidate"])
        evidence.status = EvidenceStatus.VERIFIED
        evidence.logs = [*state["logs"], "Selected automatic gates passed; Sisi retains human acceptance authority"]
        evidence.cost.tokens = state["tokens"]
        evidence.cost.minutes = state["active_minutes"]
        evidence = EvidencePackage.model_validate(evidence.model_dump())
        data = self._write_evidence(state["task"]["id"], evidence)
        return self._stage(state, "RETURNED", evidence=data)

    def _blocked(self, state: HandState) -> HandState:
        candidate = state.get("candidate") or {}
        # A fresh, schema-valid blocker report never relabels invalid evidence as delivery.
        evidence = EvidencePackage(
            status="blocked", artifacts=[], gate_reports=candidate.get("gate_reports", {}),
            logs=[*state["logs"], state["reason"]],
            cost={"tokens": state["tokens"], "minutes": state["active_minutes"], "model": state.get("cost_model", candidate.get("cost", {}).get("model", "unconfigured"))},
            unresolved=[state["reason"]], self_check="Blocked: no verified delivery. See unresolved and retry logs.",
        )
        data = self._write_evidence(state["task"]["id"], evidence)
        return self._stage(state, "BLOCKED", evidence=data)
