"""Optional real binary checks. OpenCode is queried for version ONLY, never a model."""
import os
from pathlib import Path
import shutil

import pytest

from hand.processes import run_process
from hand.tools import blender_run, godot_run


def binary(name):
    configured = os.environ.get(f"HAND_{name.upper()}")
    wrapper = Path.home() / "workspace" / "tools" / "bin" / name
    candidate = configured or (str(wrapper) if wrapper.is_file() else shutil.which(name))
    if not candidate or not shutil.which(candidate):
        pytest.skip(f"{name} binary unavailable; set HAND_{name.upper()} or install its wrapper")
    return candidate


def test_real_opencode_version_only(tmp_path):
    result = run_process([binary("opencode"), "--version"], cwd=tmp_path,
                         stdout=tmp_path / "version.stdout", stderr=tmp_path / "version.stderr", timeout=15)
    assert result["exit_code"] == 0, result
    assert (tmp_path / "version.stdout").read_text().strip()


def test_real_blender_background(tmp_path):
    program = binary("blender")
    (tmp_path / "build.py").write_text("import bpy\nfrom pathlib import Path\n"
                                      "bpy.ops.mesh.primitive_cube_add()\n"
                                      "bpy.ops.wm.save_as_mainfile(filepath=str(Path.cwd() / 'cube.blend'))\n")
    result = blender_run(Path("build.py"), workspace=tmp_path, executable=program, timeout=60)
    assert result["passed"], result
    assert (tmp_path / "cube.blend").is_file()


def test_real_godot_headless_verify(tmp_path):
    program = binary("godot")
    (tmp_path / "project.godot").write_text('config_version=5\n[application]\nconfig/name="HAND verify"\n')
    (tmp_path / "verify.gd").write_text('extends SceneTree\n\nfunc _initialize() -> void:\n\tprint("PASS=1 FAIL=0")\n\tquit(0)\n')
    result = godot_run(Path("."), workspace=tmp_path, executable=program, script=Path("verify.gd"), timeout=30)
    assert result["passed"], result
    assert result["pass_count"] == 1 and result["fail_count"] == 0


def test_real_local_clip_adapter_if_provisioned(tmp_path):
    import base64
    import hashlib
    from hand.tools import gate_clipsim

    configured = os.environ.get("HAND_CLIPSIM_SCRIPT")
    if not configured:
        pytest.skip("Local CLIP adapter/model not provisioned; set HAND_CLIPSIM_SCRIPT to an in-project adapter")
    script = Path(configured).resolve()
    root = Path(__file__).resolve().parents[1]
    if root not in script.parents or not script.is_file():
        pytest.skip("CLIP adapter must be a real file inside this project; no outside-project access")
    # A real PNG, compared with its identical locked reference; no generated media.
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aA1cAAAAASUVORK5CYII=")
    (tmp_path / "image.png").write_bytes(png)
    (tmp_path / "reference.png").write_bytes(png)
    result = gate_clipsim(Path("image.png"), Path("reference.png"), workspace=tmp_path,
                          script=script, allowed_directories=[script.parent],
                          reference_sha256=hashlib.sha256(png).hexdigest(), reference_band=[0.99, 1.0], timeout=60)
    assert result["passed"], result
    assert result["score"] >= 0.99
