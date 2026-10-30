from pathlib import Path
from unittest.mock import patch

import pytest

from hand.tools import PathViolation, TOOL_REGISTRY, fs_guard, git_snapshot, project_path


def test_fs_guard_workspace_and_declared_directory(tmp_path):
    work = tmp_path / "work" / "task"
    copy = tmp_path / "copies"
    assert fs_guard("nested/result.txt", workspace=work) == work / "nested/result.txt"
    assert fs_guard(copy / "result.txt", workspace=work, allowed_directories=[copy]) == copy / "result.txt"


@pytest.mark.parametrize("path", ["../other-task/file", "../../outside", ".git/config"])
def test_fs_guard_rejects_escape(tmp_path, path):
    with pytest.raises(PathViolation):
        fs_guard(path, workspace=tmp_path / "work" / "task")


def test_fs_guard_absolute_escape_and_prefix_collision(tmp_path):
    work = tmp_path / "task"
    with pytest.raises(PathViolation):
        fs_guard(tmp_path / "task-evil" / "file", workspace=work)


def test_fs_guard_symlink_escape(tmp_path):
    work = tmp_path / "task"
    work.mkdir()
    outside = tmp_path / "other"
    outside.mkdir()
    (work / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(PathViolation):
        fs_guard("link/file", workspace=work)


def test_managed_path_rejects_internal_symlink(tmp_path):
    (tmp_path / "source").mkdir()
    (tmp_path / "work").symlink_to(tmp_path / "source", target_is_directory=True)
    with pytest.raises(PathViolation):
        project_path(tmp_path, "work/task")


def test_git_snapshot_actual_local_commit(tmp_path):
    (tmp_path / "artifact.txt").write_text("before")
    result = git_snapshot(tmp_path)
    assert result.ok, result.log
    assert (tmp_path / ".git" / "HEAD").is_file()


def test_git_failure_is_recorded_without_raising(tmp_path):
    with patch("hand.tools.subprocess.run", side_effect=FileNotFoundError("git missing")):
        result = git_snapshot(tmp_path)
    assert not result.ok
    assert "git missing" in result.log


def test_tool_registry_membership_and_remaining_stub():
    for name in ("blender_run", "godot_run", "gate_aabb", "gate_clipsim", "gate_vision",
                 "gate_render_review", "mocap_metrics", "ssh_dispatch", "web_fetch"):
        assert name in TOOL_REGISTRY
    # mocap_metrics is still an unimplemented stub. web_fetch was implemented
    # in the 2026-10-30 package: it answers with an honest report, offline.
    with pytest.raises(NotImplementedError):
        TOOL_REGISTRY["mocap_metrics"](Path("capture.json"))
    assert TOOL_REGISTRY["web_fetch"]("ftp://example.invalid")["status"] == "blocked"
