"""Official MCP SDK adapter; task execution and approval remain in HandRunner.

No task-supplied root, transport, executor implementation or approval authority
is accepted. A caller may reject an interrupt, but only the operator's existing
CLI can grant it. Importing this module never starts a server or opens a port.
"""

from functools import partial
import json
import os
from pathlib import Path
from threading import Lock
from typing import Any

# hand.__init__ already disables LangGraph/LangSmith tracing before graph imports.
# Also disable an inherited OTel SDK, before importing the MCP SDK.
os.environ["OTEL_SDK_DISABLED"] = "true"

import anyio
from mcp.server import Server, ServerRequestContext
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolRequestParams, CallToolResult, ListToolsResult, PaginatedRequestParams,
    TextContent, Tool, ToolAnnotations,
)
from pydantic import Field, ValidationError, field_validator
import yaml

from .contracts import Contract, EvidencePackage, TaskPackage
from .executors import Executor
from .graph import HandRunner
from .tools import project_path


# Serialize only the saver/schema setup. Concurrent first connections must not
# race PRAGMA journal_mode or CREATE TABLE on a brand-new checkpoint database.
_CHECKPOINT_INIT_LOCK = Lock()


class _SubmitInput(Contract):
    task_text: str = Field(strict=True, description="Task package as YAML or JSON text; never a file path")


class _TaskInput(Contract):
    task_id: str = Field(strict=True, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")

    @field_validator("task_id")
    @classmethod
    def safe_id(cls, value: str) -> str:
        # Also use the runner's full-match check, including trailing newlines.
        HandRunner._config(value)
        return value


class _ResumeInput(_TaskInput):
    approved: bool | None = Field(
        default=None, strict=True,
        description="false rejects a pending approval; true cannot grant operator approval over MCP",
    )


_TOOLS = {
    "hand_submit_task": (
        _SubmitInput,
        "Submit YAML/JSON through the existing task contract and state machine. "
        "Triggered approvals pause before execution; submission never approves them.",
    ),
    "hand_task_status": (
        _TaskInput,
        "Read durable lifecycle status, current stage and latest evidence status/cost/unresolved count.",
    ),
    "hand_task_resume": (
        _ResumeInput,
        "Recover a nonterminal checkpoint or reject a waiting task with approved=false. "
        "MCP cannot grant triggered approvals; an operator must use hand resume <task_id>.",
    ),
    "hand_get_evidence": (_TaskInput, "Read the full schema-validated outbox evidence JSON for a known task."),
}

_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "task_id": {"type": "string"},
        "error": {
            "type": "object",
            "properties": {
                "code": {"type": "string"}, "message": {"type": "string"},
                "fields": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["code", "message"],
        },
    },
    "required": ["ok"],
}


def _error(code: str, message: str, **details: Any) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message, **details}}


def _fields(exc: ValidationError) -> list[dict[str, Any]]:
    # Do not return Pydantic's raw input/ctx: those can contain task text or
    # exception objects and are not necessarily JSON serializable.
    return [{"path": list(item["loc"]), "message": item["msg"], "type": item["type"]}
            for item in exc.errors(include_input=False, include_context=False, include_url=False)]


def _summary(task_id: str, state: dict[str, Any]) -> dict[str, Any]:
    evidence = state.get("evidence") or state.get("candidate")
    summary = None if not evidence else {
        "status": evidence["status"], "cost": evidence["cost"],
        "unresolved_count": len(evidence["unresolved"]),
    }
    pending = state.get("next", [])
    status = state.get("status", "RECEIVED")
    return {
        "ok": True, "task_id": task_id, "status": status,
        "stage": pending[0] if pending else status,
        "next": pending, "history": state.get("history", []),
        "interrupts": state.get("interrupts", []), "evidence_summary": summary,
    }


def _reject_nonfinite(value: str) -> None:
    raise ValueError(f"Non-finite JSON constant: {value}")


def _operate(root: Path, name: str, args: Contract, executor: Executor | None) -> dict[str, Any]:
    """One worker/runner/SQLite connection per call; no shared cancellation state."""
    if name == "hand_submit_task":
        try:
            data = yaml.safe_load(args.task_text)
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            field = {"path": [], "type": "yaml_parse_error",
                     "message": getattr(exc, "problem", None) or "Invalid YAML/JSON"}
            if mark is not None:
                field.update(line=mark.line + 1, column=mark.column + 1)
            return _error("VALIDATION_ERROR", "Task text is not valid YAML/JSON", fields=[field])
        try:
            task = TaskPackage.model_validate(data)
        except ValidationError as exc:
            return _error("VALIDATION_ERROR", "Task package validation failed", fields=_fields(exc))

    with _CHECKPOINT_INIT_LOCK:
        runner = HandRunner(root, executor=executor)
    with runner:
        if name == "hand_submit_task":
            runner.run(task)
            return _summary(task.id, runner.status(task.id))

        state = runner.status(args.task_id)
        if name == "hand_task_status":
            return _summary(args.task_id, state)
        if name == "hand_task_resume":
            if state["interrupts"] and args.approved is not False:
                return _error(
                    "APPROVAL_REQUIRED", "MCP cannot grant triggered approval points; use the operator CLI",
                    task_id=args.task_id, status=state["status"],
                    approval_points=[point for item in state["interrupts"] for point in item["approval_points"]],
                    operator_command=f"hand resume {args.task_id}",
                )
            # Even recovery must pass False: another process may have advanced
            # the checkpoint to an interrupt since status(). Never auto-approve
            # that newly visible pause via HandRunner.resume's CLI default True.
            runner.resume(args.task_id, approved=False)
            return _summary(args.task_id, runner.status(args.task_id))

        path = project_path(root, f"outbox/{args.task_id}/evidence.json")
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return _error("EVIDENCE_NOT_FOUND", "No published evidence exists yet", task_id=args.task_id)
        try:
            EvidencePackage.model_validate_json(raw)
            evidence = json.loads(raw, parse_constant=_reject_nonfinite)
        except ValidationError as exc:
            return _error("INVALID_EVIDENCE", "Published evidence failed contract validation", fields=_fields(exc))
        except ValueError:
            return _error("INVALID_EVIDENCE", "Published evidence is not finite JSON")
        return {"ok": True, "task_id": args.task_id, "evidence": evidence}


def _dispatch(root: Path, name: str, args: Contract, executor: Executor | None) -> dict[str, Any]:
    try:
        return _operate(root, name, args, executor)
    except ValueError as exc:
        message = str(exc)
        if message.startswith("Unknown task:"):
            code = "TASK_NOT_FOUND"
        elif "already exists; use hand status/resume" in message:
            code = "TASK_EXISTS"
        elif message.endswith("is already running"):
            code = "TASK_BUSY"
        elif "; cannot resume" in message:
            code = "TASK_TERMINAL"
        else:
            code = "INVALID_OPERATION"
        return _error(code, message)
    except OSError:
        return _error("IO_ERROR", "Local checkpoint/evidence access failed; inspect task status before retrying")
    except Exception:
        # A failed response is not permission to resubmit or rerun a task.
        # Do not leak raw tracebacks or reinterpret a crash as successful work.
        return _error("INTERNAL_ERROR", "Operation failed; inspect durable task status before retrying")


def _result(payload: dict[str, Any]) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False, allow_nan=False))],
        structured_content=payload, is_error=not payload["ok"],
    )


def create_server(root: Path, *, executor: Executor | None = None) -> Server:
    """Build the SDK server; optional executor injection is for offline tests."""
    root = root.resolve()
    tools = [Tool(
        name=name, description=description, input_schema=model.model_json_schema(),
        output_schema=_OUTPUT_SCHEMA,
        annotations=ToolAnnotations(read_only_hint=name in {"hand_task_status", "hand_get_evidence"}),
    ) for name, (model, description) in _TOOLS.items()]

    async def list_tools(ctx: ServerRequestContext, params: PaginatedRequestParams | None) -> ListToolsResult:
        return ListToolsResult(tools=tools)

    async def call_tool(ctx: ServerRequestContext, params: CallToolRequestParams) -> CallToolResult:
        if params.name not in _TOOLS:
            return _result(_error("UNKNOWN_TOOL", "Unknown HAND tool"))
        model = _TOOLS[params.name][0]
        try:
            args = model.model_validate(params.arguments if params.arguments is not None else {})
        except ValidationError as exc:
            return _result(_error("INVALID_ARGUMENTS", "Tool arguments failed validation", fields=_fields(exc)))
        # Keep the protocol loop responsive during synchronous graph/tool work.
        # Default non-abandoning cancellation keeps the task's file lock held
        # until checkpoint writes finish. Existing runner locks reject overlap.
        payload = await anyio.to_thread.run_sync(partial(_dispatch, root, params.name, args, executor))
        return _result(payload)

    server = Server("talktothehand", version="0.1.0", on_list_tools=list_tools, on_call_tool=call_tool)
    # Pinned SDK 2.3.0 installs only OpenTelemetryMiddleware by default. Remove
    # it entirely so inherited exporters cannot receive task/protocol spans.
    # No other middleware is registered on this newly constructed low-level Server.
    server.middleware.clear()
    return server


async def _serve(root: Path) -> None:
    server = create_server(root)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def serve(root: Path) -> None:
    """Run stdio only, with no banners, HTTP listener, sampling or model calls."""
    anyio.run(_serve, root)
