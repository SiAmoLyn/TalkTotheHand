import hashlib
import json
from pathlib import Path
import sys

import pytest

from hand.tools import blender_run, godot_run, gate_aabb, gate_clipsim

FIXTURES = Path(__file__).parent / "fixtures"


def fake_binary(tmp_path, body):
    path = tmp_path / "fake-tool"
    path.write_text(f"#!{sys.executable}\n" + body)
    path.chmod(0o700)
    return str(path)


def locked_clip(tmp_path):
    (tmp_path / "image.png").write_bytes(b"fixture image")
    (tmp_path / "reference.png").write_bytes(b"locked fixture reference")
    return {"artifact": Path("image.png"), "reference": Path("reference.png"),
            "reference_sha256": hashlib.sha256(b"locked fixture reference").hexdigest(),
            "reference_band": [0.7, 0.9], "workspace": tmp_path}


def assert_persisted(result):
    assert json.loads(Path(result["report_path"]).read_text()) == result


def test_blender_structured_output_and_artifact(tmp_path):
    (tmp_path / "build.py").write_text("# fixture")
    binary = fake_binary(tmp_path, "from pathlib import Path\nimport sys\n"
                         "assert '--background' in sys.argv and '--factory-startup' in sys.argv\n"
                         "assert '-noaudio' in sys.argv and '--python' in sys.argv\n"
                         "Path('result.glb').write_bytes(b'fixture, not real GLB')\nprint('rendered')\n")
    result = blender_run(Path("build.py"), workspace=tmp_path, executable=binary)
    assert result["passed"] and result["exit_code"] == 0
    assert any(Path(a["path"]).name == "result.glb" for a in result["artifacts"])
    assert Path(result["stdout_path"]).read_text() == "rendered\n"
    assert_persisted(result)


@pytest.mark.parametrize("output,passed,failed,status", [("PASS=3 FAIL=0", 3, 0, "passed"),
                                                        ("PASS: 2 FAIL: 1", 2, 1, "failed"),
                                                        ("PASS case1\nPASS case2\nFAIL case3", 2, 1, "failed"),
                                                        ("no test evidence", 0, 0, "blocked")])
def test_godot_counts(tmp_path, output, passed, failed, status):
    (tmp_path / "project.godot").write_text("config_version=5")
    binary = fake_binary(tmp_path, f"print({output!r})\n")
    result = godot_run(Path("."), workspace=tmp_path, executable=binary)
    assert result["pass_count"] == passed
    assert result["fail_count"] == failed
    assert result["status"] == status
    assert_persisted(result)


def test_tool_error_retains_stderr_and_exit(tmp_path):
    (tmp_path / "build.py").write_text("# fixture")
    binary = fake_binary(tmp_path, "import sys\nprint('original tool error',file=sys.stderr)\nsys.exit(7)\n")
    result = blender_run(Path("build.py"), workspace=tmp_path, executable=binary)
    assert result["exit_code"] == 7
    assert "original tool error" in result["error"]
    assert_persisted(result)


def test_tool_timeout(tmp_path):
    (tmp_path / "build.py").write_text("# fixture")
    binary = fake_binary(tmp_path, "import time\ntime.sleep(30)\n")
    result = blender_run(Path("build.py"), workspace=tmp_path, executable=binary, timeout=0.1)
    assert result["status"] == "blocked" and result["reason"] == "timeout"


def test_aabb_numeric_report(tmp_path):
    (tmp_path / "bounds.json").write_bytes((FIXTURES / "aabb.json").read_bytes())
    result = gate_aabb(Path("bounds.json"), workspace=tmp_path,
                       expected_dimensions={"part": [1, 2, 3], "other": [1, 2, 3]})
    assert result["passed"] and result["dimensions_checked"] == 2
    assert {c["check"] for c in result["checks"]} == {"ground_contact", "intersection", "dimension"}
    assert all("measured" in c and "threshold" in c and c["passed"] for c in result["checks"])
    assert_persisted(result)


def test_aabb_reports_floating_intersection_and_bad_scale(tmp_path):
    data = {"objects": [{"name": "a", "min": [0, 0, 0.5], "max": [2, 2, 2.5]},
                        {"name": "b", "min": [1, 1, 0], "max": [3, 3, 2]}]}
    (tmp_path / "bounds.json").write_text(json.dumps(data))
    result = gate_aabb(Path("bounds.json"), workspace=tmp_path, ground_tolerance=0.1,
                       overlap_volume_threshold=0.25, expected_dimensions={"a": [1, 2, 2]})
    assert result["status"] == "failed"
    assert next(c for c in result["checks"] if c["check"] == "intersection")["measured"] == 1.5
    assert {c["check"] for c in result["checks"] if not c["passed"]} == {"ground_contact", "intersection", "dimension"}


@pytest.mark.parametrize("payload", ['{"objects":[]}', '{"objects":[{"name":"a","min":[0,0,0],"max":[1,1,NaN]}]}',
                                     '{"objects":[{"name":"a","min":[1,0,0],"max":[0,1,1]}]}'])
def test_invalid_aabb_blocks(tmp_path, payload):
    (tmp_path / "bounds.json").write_text(payload)
    result = gate_aabb(Path("bounds.json"), workspace=tmp_path)
    assert result["status"] == "blocked" and "error" in result
    assert_persisted(result)


@pytest.mark.parametrize("score,status", [(0.8, "passed"), (0.3, "failed")])
def test_clip_score_and_reference_band(tmp_path, score, status):
    options = locked_clip(tmp_path)
    (tmp_path / "sim.py").write_text(f"print('{{\"score\":{score}}}')\n")
    result = gate_clipsim(**options, script=Path("sim.py"))
    assert result["score"] == score and result["status"] == status
    assert result["reference_band"] == [0.7, 0.9]
    assert_persisted(result)


def test_clip_missing_script_and_model_never_invent_score(tmp_path):
    options = locked_clip(tmp_path)
    result = gate_clipsim(**options, script=Path("missing.py"))
    assert result["status"] == "blocked" and "score" not in result
    (tmp_path / "sim.py").write_text("raise RuntimeError('CLIP model unavailable')\n")
    result = gate_clipsim(**options, script=Path("sim.py"))
    assert result["status"] == "blocked" and "score" not in result
    assert "CLIP model unavailable" in result["error"]


def test_clip_changed_reference_blocks(tmp_path):
    options = locked_clip(tmp_path)
    (tmp_path / "sim.py").write_text("print('{\"score\":0.8}')\n")
    (tmp_path / "reference.png").write_bytes(b"changed")
    result = gate_clipsim(**options, script=Path("sim.py"))
    assert result["status"] == "blocked" and "sha256 mismatch" in result["error"]


@pytest.mark.parametrize("tool", ["blender_run", "godot_run", "gate_aabb", "gate_clipsim"])
def test_all_four_tools_reject_paths_and_persist_error(tmp_path, tool):
    if tool == "blender_run":
        result = blender_run(Path("../escape.py"), workspace=tmp_path)
    elif tool == "godot_run":
        result = godot_run(Path("../escape"), workspace=tmp_path)
    elif tool == "gate_aabb":
        result = gate_aabb(Path("../escape.json"), workspace=tmp_path)
    else:
        result = gate_clipsim(**{**locked_clip(tmp_path), "artifact": Path("../escape.png")})
    assert result["status"] == "blocked" and "whitelist" in result["error"]
    assert_persisted(result)


def test_symlink_log_escape_is_rejected_before_execution(tmp_path):
    (tmp_path / "script.py").write_text("# fixture")
    other = tmp_path / "other"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    other.mkdir()
    (workspace / "run.stdout.log").symlink_to(other / "protected")
    (workspace / "script.py").write_text("# fixture")
    result = blender_run(Path("script.py"), workspace=workspace, result_path=Path("run.json"))
    assert result["status"] == "blocked"
    assert not (other / "protected").exists()


def test_godot_manifest_symlink_escape_blocks(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (tmp_path / "outside.godot").write_text("config_version=5")
    (project / "project.godot").symlink_to(tmp_path / "outside.godot")
    result = godot_run(Path("."), workspace=project)
    assert result["status"] == "blocked" and "whitelist" in result["error"]


def test_godot_failure_line_cannot_be_hidden_by_zero_summary(tmp_path):
    (tmp_path / "project.godot").write_text("config_version=5")
    binary = fake_binary(tmp_path, "print('FAIL important case\\nPASS=3 FAIL=0')\n")
    result = godot_run(Path("."), workspace=tmp_path, executable=binary)
    assert result["fail_count"] == 1 and not result["passed"]


def test_missing_clip_configuration_is_structured_blocker(tmp_path, monkeypatch):
    monkeypatch.delenv("HAND_CLIPSIM_SCRIPT", raising=False)
    result = gate_clipsim(Path("image.png"), Path("reference.png"), workspace=tmp_path)
    assert result["status"] == "blocked" and "not configured" in result["error"]
