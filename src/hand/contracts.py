"""Strict task/evidence contracts and actionable YAML validation errors."""

from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, revalidate_instances="always")


class ToolClass(StrEnum):
    BLENDER = "blender"
    GODOT = "godot"
    CODE = "code"
    IMAGE = "image"
    TEXT = "text"
    RESEARCH = "research"


class EvidenceStatus(StrEnum):
    VERIFIED = "verified"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"


class Budget(Contract):
    max_minutes: float = Field(gt=0)
    max_tokens: int = Field(gt=0, strict=True)


class Deliverable(Contract):
    path: str = Field(min_length=1)
    type: str = Field(default="text", min_length=1)


class ApprovalPoint(Contract):
    id: str = Field(min_length=1)
    reason: str = Field(default="Explicit approval requested", min_length=1)
    triggered: bool = False


class OpenCodeOptions(Contract):
    model: str | None = Field(default=None, pattern=r"^[^/\s]+/[^\s]+$")
    session_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]+$")
    continue_session: bool = False
    fixture: str | None = None


class TaskPackage(Contract):
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
    title: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    tool_class: ToolClass
    context_files: list[str]
    spec: str = Field(min_length=1)
    redlines: list[str]
    gates: list[str]
    budget: Budget
    deliverables: list[Deliverable] = Field(min_length=1)
    executor_hint: str | None = None
    approval_points: list[ApprovalPoint] = Field(default_factory=list)
    # Explicit optional extension for the spec's "declared project-copy directories".
    # These directories must still be inside the installation root.
    allowed_directories: list[str] = Field(default_factory=list)
    opencode: OpenCodeOptions = Field(default_factory=OpenCodeOptions)
    # Gate names remain strings; tool-specific inputs are configured separately.
    gate_options: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @field_validator("deliverables", mode="before")
    @classmethod
    def expand_deliverables(cls, value: Any) -> Any:
        if isinstance(value, list):
            return [{"path": item} if isinstance(item, str) else item for item in value]
        return value

    @field_validator("approval_points", mode="before")
    @classmethod
    def expand_approvals(cls, value: Any) -> Any:
        if isinstance(value, list):
            return [
                {"id": item, "reason": item, "triggered": True}
                if isinstance(item, str) else item for item in value
            ]
        return value

    @field_validator("gates")
    @classmethod
    def unique_gates(cls, value: list[str]) -> list[str]:
        if any(not gate.strip() for gate in value) or len(set(value)) != len(value):
            raise ValueError("gate names must be nonempty and unique")
        return value


class Artifact(Contract):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class Cost(Contract):
    tokens: int = Field(ge=0, strict=True)
    minutes: float = Field(ge=0)
    model: str = Field(min_length=1)


class EvidencePackage(Contract):
    status: EvidenceStatus
    artifacts: list[Artifact]
    gate_reports: dict[str, Any]
    logs: list[str]
    cost: Cost
    unresolved: list[str]
    self_check: str = Field(min_length=1)


class TaskValidationError(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("Task package validation failed:\n" + "\n".join(f"- {e}" for e in errors))


def load_task(path: str | Path) -> TaskPackage:
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise TaskValidationError([str(exc)]) from exc
    try:
        return TaskPackage.model_validate(data)
    except ValidationError as exc:
        raise TaskValidationError([
            f"{'.'.join(str(part) for part in error['loc']) or '<root>'}: {error['msg']}"
            for error in exc.errors()
        ]) from exc
