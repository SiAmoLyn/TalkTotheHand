"""cua-driver call adapter; element tokens belong to the current daemon session.

Get tokens from a preceding accessibility-tree/window-state call on that daemon.
File picking is a gated composite: it drives the app's already-open NSOpenPanel
through ONE run_script call (a single driver session) and only reports success
when the panel is gone afterwards. When the panel cannot be located or its
accessibility surface does not resolve — the common state when the owner app
is not frontmost — it blocks with a precise reason instead of guessing.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import shutil
from threading import Event
from typing import Any
import uuid


def _require_success_receipt(payload: Any) -> dict:
    """A driver receipt only counts when it explicitly confirms success.

    The payload must be an object, at least one of ok/success must be
    present and truly True, and no declared status field may contradict
    it — {"ok": true, "success": "false"} is a failure, not a pass, and
    an empty receipt confirms nothing (R2-07).
    """
    if not isinstance(payload, dict):
        raise ValueError("Cua receipt is not a JSON object")
    if payload.get("error"):
        raise ValueError("Cua receipt carries an error")
    confirmed = False
    for key in ("ok", "success"):
        if key in payload:
            if payload[key] is not True:
                raise ValueError(f"Cua receipt field {key!r} does not confirm success")
            confirmed = True
    if not confirmed:
        raise ValueError("Cua receipt lacks an explicit ok=true/success=true confirmation")
    return payload


def _gui(operation: str, *, workspace: Path, allowed_directories: list[Path] | None,
         result_path: Path | None, timeout: float, cancel: Event | None,
         options: dict[str, Any]) -> dict[str, Any]:
    from .tools import _command_report, _number, _report, fs_guard, project_path

    def action(target: Path) -> dict:
        seconds = _number(timeout, "timeout")
        if seconds <= 0:
            raise ValueError("timeout must be positive")
        if operation == "pick-file":
            source = fs_guard(options["path"], workspace=workspace, allowed_directories=allowed_directories)
            if not source.is_file():
                raise FileNotFoundError("File-picker input is not a file")
            if source in {target, target.with_suffix(".stdout.log"), target.with_suffix(".stderr.log")}:
                raise ValueError("GUI report/log paths must not overwrite file-picker input")
            pid = options.get("pid")
            if pid is None:
                return {"status": "blocked", "passed": False, "reason": "pid_required",
                        "missing_fields": ["pid"],
                        "error": "File picking requires the pid of the app showing the open panel",
                        "artifacts": []}
            if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
                raise ValueError("pid must be a positive integer")
            binary = os.environ.get("HAND_CUA_BIN")
            if not binary or not shutil.which(binary):
                return {"status": "blocked", "passed": False, "code": "cua_unconfigured",
                        "error": "Cua binary missing or not configured (HAND_CUA_BIN)", "artifacts": []}
            script = _pick_script(pid, source)
            arguments = {"session": "hand-pick-file",
                         "timeout_ms": int(min(seconds, 120) * 1000), "script": script}
            argv = [binary, "call", "run_script",
                    json.dumps(arguments, ensure_ascii=False, allow_nan=False)]
            result = _command_report(argv, workspace=workspace, target=target, timeout=seconds,
                                     cancel=cancel, allowed_directories=allowed_directories)
            result["artifacts"] = []
            if not result["passed"]:
                return result
            payload = json.loads(Path(result["stdout_path"]).read_text(encoding="utf-8"))
            # cua-driver run_script returns the script's value under "value".
            outcome = (payload.get("result") or payload.get("value")) if isinstance(payload, dict) else None
            if not isinstance(outcome, dict) or payload.get("ok") is not True:
                return {**result, "status": "blocked", "passed": False, "reason": "pick_failed",
                        "error": "cua run_script returned no structured pick outcome"}
            stage = outcome.get("stage")
            if stage == "opened" and outcome.get("opened") is True:
                result["receipt"] = outcome
                result["selected_path"] = str(source)
                return result
            reasons = {"not_found": "panel_not_found", "not_drivable": "panel_not_drivable"}
            return {**result, "status": "blocked", "passed": False,
                    "reason": reasons.get(stage, "pick_failed"),
                    "error": str(outcome.get("detail") or f"pick-file ended at stage: {stage}"),
                    "receipt": outcome}

        pid = options.get("pid")
        if pid is not None and (isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0):
            raise ValueError("pid must be a positive integer")
        arguments: dict[str, Any] = {} if pid is None else {"pid": pid}
        artifact = None
        if operation in ("screenshot", "ax-tree"):
            if operation == "screenshot":
                window_id = options.get("window_id")
                if window_id is not None:
                    if isinstance(window_id, bool) or not isinstance(window_id, int) or window_id < 0:
                        raise ValueError("window_id must be a nonnegative integer")
                    if pid is None:
                        return {"status": "blocked", "passed": False, "reason": "pid_required",
                                "missing_fields": ["pid"], "error": "Window screenshot requires pid", "artifacts": []}
                    arguments["window_id"] = window_id
                tool = "get_desktop_state" if pid is None else "get_window_state"
                if pid is not None:
                    arguments["tree_format"] = "elements"
            else:
                tool = "get_accessibility_tree"
            suffix = ".png" if operation == "screenshot" else ".json"
            artifact = project_path(workspace, f".hand/gui/{operation}-{uuid.uuid4().hex}{suffix}")
            artifact.parent.mkdir(parents=True, exist_ok=True)
        elif operation in ("click", "type"):
            tool = "click" if operation == "click" else "type_text"
            element, x, y = (options.get(key) for key in ("element", "x", "y"))
            if element is not None:
                if not isinstance(element, str) or not element.strip() or "\x00" in element or x is not None or y is not None:
                    raise ValueError("Supply either an element or both coordinates")
                arguments["element_token"] = element
            elif x is not None or y is not None:
                for value in (x, y):
                    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                        raise ValueError("Coordinates must be finite nonnegative numbers")
                if pid is None:
                    return {"status": "blocked", "passed": False, "reason": "pid_required",
                            "missing_fields": ["pid"], "error": "Coordinate targeting requires pid", "artifacts": []}
                if operation == "type":
                    return {"status": "blocked", "passed": False, "reason": "coordinate_typing_not_supported",
                            "error": "type_text accepts element_token/pid or current focus; click coordinates first",
                            "artifacts": []}
                arguments.update(x=x, y=y)
            elif operation == "click":
                raise ValueError("Click requires an element or both coordinates")
            if operation == "type":
                text = options.get("text")
                if not isinstance(text, str) or "\x00" in text:
                    raise ValueError("text must be a string without NUL")
                # No locator means {} for focus selection; text remains the
                # required type_text argument. Never guess a PID or click.
                arguments["text"] = text
        else:
            raise ValueError("Unknown GUI operation")
        binary = os.environ.get("HAND_CUA_BIN")
        if not binary or not shutil.which(binary):
            return {"status": "blocked", "passed": False, "code": "cua_unconfigured",
                    "error": "Cua binary missing or not configured (HAND_CUA_BIN)", "artifacts": []}
        argv = [binary, "call", tool, json.dumps(arguments, ensure_ascii=False, allow_nan=False)]
        if operation == "screenshot":
            argv.extend(["--screenshot-out-file", str(artifact)])
        result = _command_report(argv, workspace=workspace, target=target, timeout=seconds,
                                 cancel=cancel, allowed_directories=allowed_directories)
        result["artifacts"] = []
        if not result["passed"]:
            return result
        payload = json.loads(Path(result["stdout_path"]).read_text(encoding="utf-8"))
        json.dumps(payload, allow_nan=False)
        if not isinstance(payload, (dict, list)) or (isinstance(payload, dict) and
                (payload.get("ok") is False or payload.get("success") is False or payload.get("error"))):
            raise ValueError("Cua returned an invalid or failed JSON receipt")
        if operation == "screenshot":
            # The receipt must explicitly confirm success: an empty object
            # or a bare list confirms nothing, and any declared status
            # field must be truly True (audit G5, R2-07).
            _require_success_receipt(payload)
            if not artifact.is_file() or artifact.is_symlink() or not artifact.stat().st_size:
                raise ValueError("Cua exited successfully without a new screenshot file")
            # A hash proves byte identity, not that the bytes are an image;
            # require the PNG signature before registering it (audit A11).
            with artifact.open("rb") as stream:
                if stream.read(8) != b"\x89PNG\r\n\x1a\n":
                    raise ValueError("Cua screenshot artifact is not a PNG image")
        else:
            if operation == "ax-tree":
                artifact.write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
            else:
                result["receipt"] = _require_success_receipt(payload)
        if artifact is not None:
            artifact = project_path(workspace, artifact)
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
            result.update(path=str(artifact), sha256=digest,
                          artifacts=[{"path": str(artifact), "sha256": digest}])
        return result

    # Reject input/output aliases before _report can write even a blocker report.
    if operation == "pick-file" and result_path is not None:
        try:
            source = fs_guard(options["path"], workspace=workspace, allowed_directories=allowed_directories)
            target = fs_guard(result_path, workspace=workspace)
            if source in {target, target.with_suffix(".stdout.log"), target.with_suffix(".stderr.log")}:
                raise ValueError("GUI report/log paths must not overwrite file-picker input")
        except (OSError, ValueError, TypeError) as exc:
            message = str(exc)
            return _report("gui_pick_file", workspace, None, None,
                           lambda target: {"status": "blocked", "passed": False, "error": message})
    # GUI reports, logs and generated artifacts always belong to this task.
    return _report("gui_" + operation.replace("-", "_"), workspace, None, result_path, action)


def gui_screenshot(*, workspace: Path, pid: int | None = None, window_id: int | None = None,
                   allowed_directories: list[Path] | None = None,
                   result_path: Path | None = None, timeout: float = 30, cancel: Event | None = None) -> dict[str, Any]:
    return _gui("screenshot", workspace=workspace, allowed_directories=allowed_directories,
                result_path=result_path, timeout=timeout, cancel=cancel, options={"pid": pid, "window_id": window_id})


def gui_ax_tree(*, workspace: Path, pid: int | None = None, allowed_directories: list[Path] | None = None,
                result_path: Path | None = None, timeout: float = 30, cancel: Event | None = None) -> dict[str, Any]:
    return _gui("ax-tree", workspace=workspace, allowed_directories=allowed_directories,
                result_path=result_path, timeout=timeout, cancel=cancel, options={"pid": pid})


def gui_click(*, workspace: Path, element: str | None = None, x: float | None = None,
              y: float | None = None, pid: int | None = None, allowed_directories: list[Path] | None = None,
              result_path: Path | None = None, timeout: float = 30, cancel: Event | None = None) -> dict[str, Any]:
    return _gui("click", workspace=workspace, allowed_directories=allowed_directories,
                result_path=result_path, timeout=timeout, cancel=cancel,
                options={"element": element, "x": x, "y": y, "pid": pid})


def gui_type(text: str, *, workspace: Path, element: str | None = None, x: float | None = None,
             y: float | None = None, pid: int | None = None, allowed_directories: list[Path] | None = None,
             result_path: Path | None = None, timeout: float = 30, cancel: Event | None = None) -> dict[str, Any]:
    return _gui("type", workspace=workspace, allowed_directories=allowed_directories,
                result_path=result_path, timeout=timeout, cancel=cancel,
                options={"text": text, "element": element, "x": x, "y": y, "pid": pid})


def _pick_script(pid: int, source: Path) -> str:
    """One-session driver script for the NSOpenPanel pick composite.

    Locates the app's file-open panel, probes that its accessibility surface
    actually resolves (refuses to drive blind), then: Cmd+Shift+G, type the
    absolute path, Return to navigate/select, Return to confirm. Success is
    only reported when the panel window is gone afterwards.
    """
    return (
        f"const PID = {pid}, PATH = {json.dumps(str(source))}, NAME = {json.dumps(source.name)};\n"
        "const _wins = r => (Array.isArray(r) ? r : (r && r.windows) || []);\n"
        "const wins = _wins(await cua.listWindows({pid: PID}));\n"
        "const panel = wins.find(w => { const t = w.title || ''; return /^Open\\b/.test(t) && !/Save/.test(t); });\n"
        "if (!panel) return {stage: 'not_found', detail: 'no file-open panel window for pid ' + PID};\n"
        "const app = await cua.getApp({pid: PID, windowId: panel.window_id});\n"
        "const st = await app.getState({tree_format: 'elements'});\n"
        "if (!st || !st.returned_element_count)\n"
        "  return {stage: 'not_drivable', detail: (st && st.degraded_reason) || 'panel accessibility tree is empty'};\n"
        "await app.hotkey(['cmd', 'shift', 'g']);\n"
        "await cua.sleep(800);\n"
        "let typed = false;\n"
        "try { await app.setValue({role: 'textfield'}, PATH); typed = true; } catch (e) {}\n"
        "if (!typed) { try { await app.typeText(PATH); typed = true; } catch (e) {} }\n"
        "if (!typed) return {stage: 'type_failed', detail: 'could not enter the path in the Go sheet'};\n"
        "await app.pressKey('return');\n"
        "await cua.sleep(800);\n"
        "let selected = false;\n"
        "try { const q = await app.query({text: NAME}); const qv = q && q.value !== undefined ? q.value : q;\n"
        "  const els = Array.isArray(qv) ? qv : ((qv && qv.elements) || []);\n"
        "  selected = els.some(e => e && e.selected === true); } catch (e) {}\n"
        "if (!selected) return {stage: 'unconfirmed', detail: 'target file row not verifiably selected', opened: false};\n"
        "await app.pressKey('return');\n"
        "await cua.sleep(800);\n"
        "const after = _wins(await cua.listWindows({pid: PID}));\n"
        "const stillOpen = after.some(w => w.window_id === panel.window_id);\n"
        "return {stage: stillOpen ? 'unconfirmed' : 'opened', opened: !stillOpen};\n"
    )


def gui_pick_file(path: Path, *, workspace: Path, pid: int | None = None,
                  allowed_directories: list[Path] | None = None,
                  result_path: Path | None = None, timeout: float = 60, cancel: Event | None = None) -> dict[str, Any]:
    """Pick `path` in the open NSOpenPanel of the app at `pid` (gated composite).

    Blocks with reason pid_required / panel_not_found / panel_not_drivable /
    pick_failed whenever any precondition cannot be verified; only reports
    passed after the panel closes following the pick.
    """
    return _gui("pick-file", workspace=workspace, allowed_directories=allowed_directories,
                result_path=result_path, timeout=timeout, cancel=cancel,
                options={"path": path, "pid": pid})
