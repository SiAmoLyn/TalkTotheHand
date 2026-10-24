"""One durable watchdog tick. Only explicit, configured argv can cause actions."""
from __future__ import annotations

from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import re
import time
from typing import Any, Callable
import uuid
from zoneinfo import ZoneInfo


PHASES = {"idle", "detected", "action1", "wait", "recheck", "action2", "recovered", "action3"}


def _finite(value: Any, name: str, minimum: float = 0) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < minimum:
        raise ValueError(f"{name} must be finite and >= {minimum}")
    return float(value)


def load_config(config_file: Path, *, root: Path, log_dir: Path | None = None,
                state_file: Path | None = None) -> dict[str, Any]:
    from .tools import project_path

    source = project_path(root, config_file)
    config = json.loads(source.read_text(encoding="utf-8"))
    required = {"target", "log_dir", "state_file"}
    allowed = required | {"recv_zero_threshold", "wait_seconds", "action2_interval_seconds",
                          "min_action_interval_seconds", "actions", "log_timezone", "max_log_files", "action_timeout"}
    if not isinstance(config, dict) or not required <= config.keys() or config.keys() - allowed:
        raise ValueError("Monitor config requires target/log_dir/state_file and only documented fields")
    if not isinstance(config["target"], str) or not config["target"].strip():
        raise ValueError("Monitor target must be a nonempty string")
    for name, default, minimum in (("recv_zero_threshold", 4, 4), ("wait_seconds", 300, 300),
                                    ("action2_interval_seconds", 1800, 1800),
                                    ("min_action_interval_seconds", 600, 600),
                                    ("max_log_files", 20, 1), ("action_timeout", 60, 0.001)):
        config[name] = config.get(name, default)
        _finite(config[name], name, minimum)
        if name in ("recv_zero_threshold", "max_log_files") and not isinstance(config[name], int):
            raise ValueError(f"{name} must be an integer")
    config["log_timezone"] = config.get("log_timezone", "UTC")
    ZoneInfo(config["log_timezone"])
    for name, override in (("log_dir", log_dir), ("state_file", state_file)):
        value = override if override is not None else config[name]
        if not isinstance(value, (str, Path)) or not str(value):
            raise ValueError(f"{name} must be a nonempty path")
        config[name] = project_path(root, source.parent / value)
    state = config["state_file"]
    config["events_file"] = project_path(root, state.with_name(state.name + ".events.jsonl"))
    config["lock_file"] = project_path(root, state.with_name(state.name + ".lock"))
    managed = [state, config["events_file"], config["lock_file"]]
    if source in managed or config["log_dir"] in managed or any(config["log_dir"] in p.parents for p in managed):
        raise ValueError("Monitor state/events/lock must be separate from its config and input log directory")
    actions = config.get("actions", {})
    if not isinstance(actions, dict) or actions.keys() - {"action1", "action2", "action3"}:
        raise ValueError("actions must map action1/action2/action3 to enabled/argv")
    config["actions"] = {}
    for name in ("action1", "action2", "action3"):
        setting = actions.get(name, {"enabled": False, "argv": []})
        if not isinstance(setting, dict) or setting.keys() - {"enabled", "argv"}:
            raise ValueError(f"Invalid action setting: {name}")
        enabled, argv = setting.get("enabled", False), setting.get("argv", [])
        if (not isinstance(enabled, bool) or not isinstance(argv, list)
                or any(not isinstance(arg, str) or "\x00" in arg for arg in argv)
                or (enabled and (not argv or not argv[0] or argv[0].startswith("-")))):
            raise ValueError(f"{name} requires a boolean switch and a valid argv array")
        config["actions"][name] = {"enabled": enabled, "argv": argv}
    return config


def read_state(path: Path, *, target: str | None = None) -> dict[str, Any]:
    if not path.exists():
        return {"phase": "idle", "last_action_at": None, "target": target, "closure_done": False}
    state = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(state, dict) or state.get("phase") not in PHASES:
        raise ValueError("Invalid monitor state phase")
    if target is not None and state.get("target") != target:
        raise ValueError("Monitor state target differs from config; use a separate state file")
    for key in ("last_action_at", "phase_at", "detected_at", "action1_at", "last_action2_at", "updated_at"):
        if state.get(key) is not None:
            _finite(state[key], key)
    if not isinstance(state.get("closure_done", False), bool):
        raise ValueError("Invalid monitor closure_done flag")
    if state["phase"] != "idle" and state.get("detected_at") is None:
        raise ValueError("Active monitor state requires detected_at")
    if state["phase"] in {"action1", "wait", "recheck", "action2", "action3"} and state.get("last_action_at") is None:
        raise ValueError("Action state requires last_action_at")
    return state


def _timestamp(value: Any, zone: ZoneInfo) -> float:
    if isinstance(value, (float, int)) and not isinstance(value, bool):
        return _finite(value, "log timestamp")
    if not isinstance(value, str):
        raise ValueError("Missing log timestamp")
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=zone)
    return stamp.timestamp()


def _record(line: str, zone: ZoneInfo, *, configured_target: str) -> tuple[float, str, int, bool, dict[str, str]] | None:
    """Return None for another Day8 destination before validating its counters."""
    line = line.strip()
    diagnostics: dict[str, str] = {}
    if line.startswith("{"):
        record = json.loads(line)
        stamp = record.get("timestamp", record.get("ts"))
        target = record.get("target")
        received = record.get("recv")
        success = record.get("success", False)
        if not isinstance(success, bool):
            raise ValueError("success must be boolean")
    else:
        match = re.match(r"^\[?(\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)?)\]?\s+(.*)$", line)
        if not match:
            raise ValueError("Missing timestamp")
        stamp, rest = match.groups()
        # Real Day8 service logs carry two record shapes: conn{...} and
        # tcp{...} (the tun-mode shape that bears the blackhole signature).
        connection = re.search(r"\b(?:conn|tcp)\{([^{}]*)\}", rest)
        if connection:
            fields = dict(re.findall(r"(?:^|\s)(\w+)=([^\s]+)", connection.group(1)))
            # dst may be host:port or host@resolved-ip:port (tcp shape).
            destination = re.match(r"([^:@\s]+)", fields.get("dst", ""))
            if not destination:
                raise ValueError("Missing/invalid dst host")
            if configured_target not in destination.group(1):
                return None
            target = configured_target
            received = int(fields["recv"])
            # Day8 success is determined exclusively by recv, including when
            # diagnostic/status text elsewhere on the line says otherwise.
            success = False
            diagnostics = {key: fields[key] for key in ("sent", "dur") if key in fields}
        else:
            fields = dict(re.findall(r"\b(target|recv|status|success)=([^\s]+)", rest))
            target = fields.get("target")
            received = int(fields["recv"])
            success = fields.get("status", "").lower() in {"ok", "success"} or fields.get("success", "").lower() == "true"
    if (not isinstance(target, str) or not target or isinstance(received, bool)
            or not isinstance(received, int) or received < 0):
        raise ValueError("Missing/invalid target or recv")
    return _timestamp(stamp, zone), target, received, success or received > 0, diagnostics


def detect(config: dict[str, Any], *, now: float) -> dict[str, Any]:
    from .tools import project_path

    directory = config["log_dir"]
    if not directory.is_dir():
        raise FileNotFoundError("Monitor log directory is missing")
    files = sorted((p for p in directory.iterdir() if p.is_file() and not p.is_symlink()
                    and p.suffix in {".log", ".jsonl", ".txt"}), key=lambda p: p.stat().st_mtime, reverse=True)
    result = {"recv_zero": 0, "success": 0, "bad_lines": 0, "ignored_lines": 0,
              "files_read": 0, "files_skipped": max(0, len(files) - config["max_log_files"]),
              "latest_success_at": None, "blackhole": False, "window_seconds": 600,
              "diagnostics": {"sent_bytes": 0, "duration_seconds": 0.0}}
    zone = ZoneInfo(config["log_timezone"])
    for source in files[:config["max_log_files"]]:
        source = project_path(directory, source)
        with source.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    record = _record(line, zone, configured_target=config["target"])
                    if record is None:
                        result["ignored_lines"] += 1
                        continue
                    stamp, target, received, success, diagnostics = record
                except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
                    result["bad_lines"] += 1
                    continue
                if target != config["target"] or not now - 600 <= stamp <= now:
                    result["ignored_lines"] += 1
                    continue
                if success:
                    result["success"] += 1
                    result["latest_success_at"] = max(result["latest_success_at"] or stamp, stamp)
                elif received == 0:
                    result["recv_zero"] += 1
                # Optional diagnostics never decide whether a row is valid,
                # successful, or evidence of a blackhole.
                sent = diagnostics.get("sent", "")
                if re.fullmatch(r"\d+", sent):
                    result["diagnostics"]["sent_bytes"] += int(sent)
                duration = diagnostics.get("dur", "")
                if re.fullmatch(r"\d+(?:\.\d+)?s", duration):
                    total = result["diagnostics"]["duration_seconds"] + float(duration[:-1])
                    if math.isfinite(total):
                        result["diagnostics"]["duration_seconds"] = total
        result["files_read"] += 1
    result["blackhole"] = result["recv_zero"] >= config["recv_zero_threshold"] and result["success"] == 0
    return result


def _save(path: Path, state: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(state, allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _advance(state: dict[str, Any], observation: dict[str, Any], config: dict[str, Any], now: float) -> tuple[str | None, str]:
    phase = state["phase"]
    success = observation["latest_success_at"]
    recovered = success is not None and success >= max(state.get("detected_at") or 0, state.get("last_action_at") or 0)
    blackhole = observation["blackhole"]

    def move(next_phase: str) -> None:
        state.update(phase=next_phase, phase_at=now)

    def action(name: str) -> tuple[str | None, str]:
        if not config["actions"][name]["enabled"]:
            return None, name + "_disabled"
        last = state.get("last_action_at")
        if last is not None and now - last < config["min_action_interval_seconds"]:
            return None, "minimum_action_interval"
        if name == "action2":
            anchor = state.get("last_action2_at")
            if anchor is None:
                anchor = state.get("action1_at")
            if anchor is None:
                anchor = last
            if anchor is None or now - anchor < config["action2_interval_seconds"]:
                return None, "action2_interval"
        move(name)
        state["last_action_at"] = now
        if name == "action1":
            state["action1_at"] = now
        if name == "action2":
            state["last_action2_at"] = now
        if name == "action3":
            state["closure_done"] = True
        return name, "action_reserved"

    if phase == "idle":
        if blackhole:
            move("detected")
            state.update(detected_at=now, closure_done=False, action1_at=None, last_action2_at=None)
    elif phase in {"detected", "recheck"} and recovered:
        move("recovered")
    elif phase == "detected" and blackhole:
        return action("action1")
    elif phase in {"action1", "action2"}:
        move("wait")
    elif phase == "wait":
        if now - state["last_action_at"] >= config["wait_seconds"]:
            move("recheck")
        else:
            return None, "recheck_wait"
    elif phase == "recheck" and blackhole:
        return action("action2")
    elif phase == "recovered":
        if blackhole:
            move("detected")
            state.update(detected_at=now, closure_done=False, action1_at=None, last_action2_at=None)
        elif config["actions"]["action3"]["enabled"] and not state.get("closure_done"):
            if not recovered:
                return None, "awaiting_recovery_confirmation"
            return action("action3")
        else:
            move("idle")
    elif phase == "action3":
        move("recovered")
    return None, "transition" if phase != state["phase"] else "no_fresh_evidence"


def monitor_once(config_file: Path, *, root: Path, log_dir: Path | None = None,
                 state_file: Path | None = None, dry_run: bool = False,
                 clock: Callable[[], float] = time.time,
                 executor: Callable[[list[str]], dict[str, Any]] | None = None) -> dict[str, Any]:
    """Persist an action reservation *before* launch; uncertain crashes never retry it early.

    A dry run has no filesystem or executor side effects. An enabled action's
    failure still consumes its interval. Empty/stale/bad-only logs never recover.
    """
    result: dict[str, Any] = {"tool": "monitor", "status": "blocked", "passed": False, "dry_run": dry_run, "action": None}
    config = None
    try:
        config = load_config(config_file, root=root, log_dir=log_dir, state_file=state_file)
        now = _finite(clock(), "clock")
        result["timestamp"] = now
        if not dry_run:
            config["state_file"].parent.mkdir(parents=True, exist_ok=True)
        with (nullcontext(None) if dry_run else config["lock_file"].open("a")) as lock:
            if lock is not None:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise ValueError("Monitor tick already running") from exc
            state = read_state(config["state_file"], target=config["target"])
            original = deepcopy(state)
            observation = detect(config, now=now)
            action, reason = _advance(state, observation, config, now)
            state["updated_at"] = now
            result.update(status="passed", passed=True, before=original["phase"],
                          state=original if dry_run else state, observation=observation, reason=reason,
                          state_file=str(config["state_file"]), events_file=str(config["events_file"]))
            if dry_run:
                result.update(proposed_state=state, planned_action=action)
                return result
            if action:
                # An OS crash between reservation and launch leaves an honest
                # uncertain attempt and its cooldown, rather than double firing.
                state["last_action_result"] = {"action": action, "status": "reserved", "at": now}
                _save(config["state_file"], state)
                result["action"] = action
                try:
                    if executor is None:
                        from .processes import run_process
                        from .tools import project_path
                        stem = project_path(root, config["state_file"].parent / ("monitor-action-" + uuid.uuid4().hex))
                        receipt = run_process(config["actions"][action]["argv"], cwd=config["state_file"].parent,
                                              stdout=stem.with_suffix(".stdout.log"), stderr=stem.with_suffix(".stderr.log"),
                                              timeout=config["action_timeout"])
                    else:
                        receipt = executor(list(config["actions"][action]["argv"]))
                    if not isinstance(receipt, dict) or "exit_code" not in receipt:
                        raise ValueError("Action executor must return an exit_code receipt")
                    json.dumps(receipt, allow_nan=False)
                    ok = receipt["exit_code"] == 0 and not receipt.get("reason")
                    state["last_action_result"] = {"action": action, "status": "passed" if ok else "blocked", "at": now, "receipt": receipt}
                    if not ok:
                        result.update(status="blocked", passed=False, error="Monitor action failed; cooldown retained")
                except Exception as exc:
                    state["last_action_result"] = {"action": action, "status": "blocked", "at": now, "error": str(exc)}
                    result.update(status="blocked", passed=False, error=str(exc))
            if reason.endswith("_disabled"):
                result.update(status="blocked", passed=False, error="Required monitor action is disabled")
            _save(config["state_file"], state)
            with config["events_file"].open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(result, allow_nan=False) + "\n")
        return result
    except (OSError, ValueError, TypeError, KeyError) as exc:
        result.update(status="blocked", passed=False, error=f"{type(exc).__name__}: {exc}")
        return result
