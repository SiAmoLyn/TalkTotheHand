"""OpenCode JSONL parser and restrictive configuration; no provider SDK."""
from __future__ import annotations

import json
import math
from pathlib import Path


class EventParser:
    def __init__(self):
        self.session_id = None
        self.steps = []
        self.errors = []
        self.tokens = 0
        self.reported_cost = 0.0
        self.finished = False
        self.timestamps = []
        self._accounted = set()

    def consume(self, line: bytes) -> None:
        if not line.strip():
            return
        try:
            def invalid_constant(value):
                raise ValueError(f"Non-finite JSON constant: {value}")
            event = json.loads(line, parse_constant=invalid_constant)
            if not isinstance(event, dict):
                raise ValueError("event must be an object")
            session = event.get("sessionID") or event.get("session_id")
            if session:
                if self.session_id and session != self.session_id:
                    raise ValueError("multiple session ids in one event stream")
                if not isinstance(session, str):
                    raise ValueError("session id must be text")
                self.session_id = session
            timestamp = event.get("timestamp")
            if isinstance(timestamp, (int, float)) and math.isfinite(timestamp) and 0 <= timestamp <= 1e15:
                self.timestamps.append(timestamp)
            kind = event.get("type")
            part = event.get("part") or {}
            if not isinstance(part, dict):
                raise ValueError("part must be an object")
            if kind in ("text", "tool_use", "tool_call", "step_start", "step_finish"):
                self.steps.append({"type": kind, "part": part})
            if kind == "step_finish":
                # Native events repeat totals if replayed; count each part once.
                identity = part.get("id")
                if identity is None or identity not in self._accounted:
                    usage = part.get("tokens") or {}
                    cache = usage.get("cache") or {}
                    values = ([usage["total"]] if "total" in usage else
                              [usage.get(k, 0) for k in ("input", "output", "reasoning")] +
                              [cache.get(k, 0) for k in ("read", "write")])
                    if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in values):
                        raise ValueError("invalid token counters")
                    self.tokens += sum(values)
                    cost = float(part.get("cost", 0))
                    if not math.isfinite(cost) or cost < 0:
                        raise ValueError("invalid cost")
                    if not math.isfinite(self.reported_cost + cost):
                        raise ValueError("cost total overflow")
                    self.reported_cost += cost
                    if identity is not None:
                        self._accounted.add(identity)
                if part.get("reason") in ("stop", "end_turn"):
                    self.finished = True
            elif kind in ("error", "timeout", "cancelled"):
                self.errors.append(json.dumps(event, ensure_ascii=False))
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            self.errors.append(f"Invalid OpenCode event: {exc}; raw={line.decode('utf-8', errors='replace')}")

    def summary(self) -> dict:
        return {"session_id": self.session_id, "steps": self.steps, "tokens": self.tokens,
                "reported_cost": self.reported_cost, "finished": self.finished, "errors": self.errors,
                "event_duration_minutes": ((max(self.timestamps) - min(self.timestamps)) / 60000
                                           if self.timestamps else None)}


def permission_config(workspace: Path, allowed: list[Path]) -> dict:
    roots = [workspace.resolve(), *(p.resolve() for p in allowed)]
    paths = {"*": "deny"}
    for root in roots:
        paths[str(root)] = "allow"
        paths[f"{root}/**"] = "allow"
    # Last match wins. Even an internal copy of a credential remains forbidden.
    for pattern in ("**/.git/**", "**/.hand/**", "**/opencode.json", "**/opencode.jsonc",
                    "**/.opencode/**", "**/.ssh/**", "**/.aws/**", "**/.gnupg/**",
                    "**/Secure Vault/**", "**/MEMORY.md", "**/credentials.json",
                    "**/auth.json", "**/token.json"):
        paths[pattern] = "deny"
    return {"$schema": "https://opencode.ai/config.json", "share": "disabled",
            "permission": {"*": "deny", "read": dict(paths), "edit": dict(paths),
                           "external_directory": dict(paths), "bash": "deny",
                           "webfetch": "deny", "websearch": "deny", "task": "deny"}}
