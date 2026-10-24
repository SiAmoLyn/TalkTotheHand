"""Validated node recipes. No machine identities or connection defaults live here."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


class NodeConfigError(ValueError):
    def __init__(self, code: str, message: str, *, details: Any = None):
        super().__init__(message)
        self.report = {"status": "blocked", "code": code, "error": message}
        if details is not None:
            self.report["details"] = details


class Node(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    target: str
    cwd: str
    ssh_args: list[str] = Field(default_factory=list)
    via: str | None = None

    @field_validator("target")
    @classmethod
    def target_operand(cls, value: str) -> str:
        if not value or value.startswith("-") or not re.fullmatch(r"[A-Za-z0-9_.@:\[\]-]+", value):
            raise ValueError("SSH target must be a host alias or user@HOST_ALIAS without options/whitespace")
        return value

    @field_validator("cwd")
    @classmethod
    def directory_operand(cls, value: str) -> str:
        if not value or "\x00" in value:
            raise ValueError("cwd must be a nonempty remote directory without NUL")
        return value

    @field_validator("ssh_args")
    @classmethod
    def safe_options(cls, value: list[str]) -> list[str]:
        # Recipes may tune transport, but cannot replace our noninteractive
        # policy, add a command, forwarding, or arbitrary local ProxyCommand.
        paired = {"-p", "-i", "-l", "-c"}
        option_keys = {"AddressFamily", "ServerAliveInterval", "ServerAliveCountMax",
                       "ConnectionAttempts", "StrictHostKeyChecking", "UserKnownHostsFile",
                       "IdentityFile", "IdentitiesOnly", "LogLevel"}
        index = 0
        while index < len(value):
            flag = value[index]
            if flag in ("-4", "-6", "-C", "-q"):
                index += 1
                continue
            if flag not in paired | {"-o"} or index + 1 >= len(value):
                raise ValueError("ssh_args contains an unsupported or incomplete option")
            operand = value[index + 1]
            if not operand or operand.startswith("-") or any(c in operand for c in "\x00\n\r"):
                raise ValueError("ssh_args option operand is invalid")
            if flag == "-o":
                key, separator, setting = operand.partition("=")
                if key not in option_keys or not separator or not setting:
                    raise ValueError("ssh_args -o option is not allowed")
            if flag == "-p" and (not operand.isdigit() or not 1 <= int(operand) <= 65535):
                raise ValueError("SSH port must be within 1..65535")
            if flag == "-l" and not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", operand):
                raise ValueError("SSH user must be a simple username operand")
            index += 2
        return value


class NodesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    nodes: dict[str, Node] = Field(min_length=1)

    @model_validator(mode="after")
    def references(self) -> NodesConfig:
        for name in self.nodes:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", name):
                raise ValueError("Node names must be simple nonempty identifiers")
            visited = set()
            current = name
            while current is not None:
                if current not in self.nodes:
                    raise ValueError(f"Jump node not configured: {current}")
                if current in visited:
                    raise ValueError(f"Jump node cycle: {name}")
                visited.add(current)
                current = self.nodes[current].via
            # -J represents jump users/ports directly. Other per-hop options
            # require an operator SSH config; never silently drop a recipe.
            jump = self.nodes[name].via
            if jump and any(self.nodes[jump].ssh_args[i] not in {"-p", "-l"}
                            for i in range(0, len(self.nodes[jump].ssh_args), 2)):
                raise ValueError(f"Jump-node ssh_args require an operator SSH config: {jump}")
        return self


def load_nodes(nodes_file: str | Path | None = None, *, workspace: Path,
               allowed_directories: list[Path] | None = None) -> tuple[NodesConfig, Path]:
    from .tools import fs_guard

    configured = nodes_file if nodes_file is not None else os.environ.get("HAND_NODES_FILE")
    if not configured:
        raise NodeConfigError("nodes_unconfigured", "Nodes not configured (nodes_file / HAND_NODES_FILE)")
    try:
        source = fs_guard(configured, workspace=workspace, allowed_directories=allowed_directories)
        raw = json.loads(source.read_text(encoding="utf-8"))
        config = NodesConfig.model_validate({"nodes": raw})
    except ValidationError as exc:
        raise NodeConfigError("nodes_invalid", "Invalid node configuration",
                              details=json.loads(exc.json(include_url=False))) from exc
    except (OSError, ValueError, TypeError) as exc:
        raise NodeConfigError("nodes_invalid", str(exc)) from exc
    return config, source


def node_argv(node: str, task: dict[str, Any] | list[str], *, workspace: Path,
              nodes_file: Path | None, allowed_directories: list[Path] | None,
              executable: str | None, connect_timeout: float) -> tuple[list[str], Path]:
    from .tools import _number

    config, source = load_nodes(nodes_file, workspace=workspace, allowed_directories=allowed_directories)
    if not isinstance(node, str) or node not in config.nodes:
        raise NodeConfigError("node_unconfigured", f"SSH node not configured: {node}")
    settings = config.nodes[node]
    if isinstance(task, list):
        task = {"argv": task}
    if not isinstance(task, dict) or set(task) - {"argv", "cwd"}:
        raise ValueError("SSH task must be an object with argv and optional cwd")
    command = task.get("argv")
    if (not isinstance(command, list) or not command
            or any(not isinstance(part, str) or "\x00" in part for part in command)
            or not command[0] or command[0].startswith("-")):
        raise ValueError("SSH task argv must be a nonempty string array with an executable")
    cwd = task.get("cwd", settings.cwd)
    if not isinstance(cwd, str) or not cwd or "\x00" in cwd:
        raise ValueError("SSH node/task cwd must be a nonempty remote directory")
    if cwd.startswith("-"):
        cwd = "./" + cwd
    seconds = _number(connect_timeout, "connect_timeout")
    if seconds <= 0 or not seconds.is_integer():
        raise ValueError("connect_timeout must be a positive integer (seconds)")
    jumps = []
    current = settings.via
    while current:
        jump = config.nodes[current]
        user, separator, host = jump.target.rpartition("@")
        if not separator:
            user, host = "", jump.target
        port = None
        for flag, value in zip(jump.ssh_args[::2], jump.ssh_args[1::2]):
            if flag == "-p":
                port = value
            elif flag == "-l":
                user = value
        if ":" in host and not host.startswith("["):
            host = "[" + host + "]"
        jumps.insert(0, (user + "@" if user else "") + host + (":" + port if port else ""))
        current = jump.via
    argv = [executable or "ssh", *settings.ssh_args,
            "-o", "BatchMode=yes", "-o", f"ConnectTimeout={int(seconds)}"]
    if jumps:
        argv.extend(["-J", ",".join(jumps)])
    argv.extend(["--", settings.target, f"cd {shlex.quote(cwd)} && exec {shlex.join(command)}"])
    return argv, source
