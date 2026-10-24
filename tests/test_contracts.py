import copy

import pytest
from pydantic import ValidationError
import yaml

from hand.contracts import EvidencePackage, TaskPackage, TaskValidationError, load_task


def test_valid_yaml_and_short_forms(tmp_path, task_data):
    task_data["deliverables"] = ["result.txt"]
    task_data["approval_points"] = ["explicit permission"]
    path = tmp_path / "task.yaml"
    path.write_text(yaml.safe_dump(task_data))
    task = load_task(path)
    assert task.deliverables[0].path == "result.txt"
    assert task.approval_points[0].triggered is True


@pytest.mark.parametrize("field,value", [
    ("id", "../escape"), ("tool_class", "shell"), ("title", ""),
    ("budget", {"max_minutes": -1, "max_tokens": 1}),
    ("budget", {"max_minutes": 1, "max_tokens": True}),
    ("budget", {"max_minutes": float("inf"), "max_tokens": 1}),
    ("deliverables", []), ("gates", ["same", "same"]), ("gates", [""]),
])
def test_invalid_schema(task_data, field, value):
    task_data[field] = value
    with pytest.raises(ValidationError):
        TaskPackage.model_validate(task_data)


def test_yaml_errors_are_field_list(tmp_path, task_data):
    del task_data["goal"]
    task_data["tool_class"] = "invalid"
    task_data["unexpected"] = True
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(task_data))
    with pytest.raises(TaskValidationError) as error:
        load_task(path)
    assert len(error.value.errors) == 3
    assert all(name in str(error.value) for name in ("goal", "tool_class", "unexpected"))


@pytest.mark.parametrize("contents", ["broken: [", "- not a task", "!!python/object:builtins.object {}", ""])
def test_invalid_yaml(tmp_path, contents):
    path = tmp_path / "bad.yaml"
    path.write_text(contents)
    with pytest.raises(TaskValidationError):
        load_task(path)


def test_required_fields_cannot_be_omitted(task_data):
    for field in ("id", "title", "goal", "tool_class", "context_files", "spec", "redlines", "gates", "budget", "deliverables"):
        data = copy.deepcopy(task_data)
        del data[field]
        with pytest.raises(ValidationError):
            TaskPackage.model_validate(data)


def test_evidence_validation():
    valid = dict(status="in_progress", artifacts=[], gate_reports={}, logs=[],
                 cost={"tokens": 0, "minutes": 0, "model": "FakeLLM"}, unresolved=[], self_check="Fake")
    assert EvidencePackage.model_validate(valid).status == "in_progress"
    for change in ({"status": "done"}, {"artifacts": [{"path": "x", "sha256": "wrong"}]},
                   {"cost": {"tokens": -1, "minutes": 0, "model": "fake"}}):
        with pytest.raises(ValidationError):
            EvidencePackage.model_validate({**valid, **change})
