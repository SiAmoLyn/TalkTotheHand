"""Guarded task tools. Structured reports retain errors and original logs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import base64
import subprocess
import hashlib
from html.parser import HTMLParser
import json
import math
import os
import re
import sys
from threading import Event
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .processes import run_process
from typing import Any


class PathViolation(ValueError):
    pass


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def project_path(root: Path, path: str | Path) -> Path:
    """Application-managed paths must remain inside the project, including symlinks."""
    root = root.resolve()
    lexical = root / path
    candidate = lexical.resolve()
    if not _inside(candidate, root):
        raise PathViolation(f"Path outside project: {path}")
    # Managed directories (checkpoint DB, inbox, outbox and workspaces) must not
    # redirect into other project files via an internal symlink either.
    for part in (lexical, *lexical.parents):
        if part == root:
            break
        if part.is_symlink():
            raise PathViolation(f"Symlink in managed project path: {path}")
    return candidate


def fs_guard(path: str | Path, *, workspace: Path, allowed_directories: list[Path] | None = None) -> Path:
    """Resolve before checking: absolute paths, '..', and symlinks cannot escape."""
    workspace = workspace.resolve()
    candidate = (workspace / path).resolve()
    roots = [workspace, *(directory.resolve() for directory in (allowed_directories or []))]
    if not any(_inside(candidate, root) for root in roots):
        raise PathViolation(f"Path outside task whitelist: {path}")
    # Never allow a deliverable/context path to modify a repository's internals.
    # Compare case-folded: on case-insensitive filesystems (default APFS) a
    # differently-cased spelling names the same file (audit A06/G1).
    folded = {part.casefold() for part in candidate.parts}
    if ".git" in folded:
        raise PathViolation(f"Git internals are reserved: {path}")
    forbidden = {".ssh", ".aws", ".gnupg", "secure vault", "memory.md", "credentials.json", "auth.json", "token.json"}
    if forbidden.intersection(folded):
        raise PathViolation(f"Sensitive path is forbidden: {path}")
    return candidate


@dataclass
class SnapshotResult:
    ok: bool
    log: str


def git_snapshot(workspace: Path) -> SnapshotResult:
    """Snapshot only this directory, without global config, hooks, signing or remotes."""
    try:
        git_dir = workspace / ".git"
        if git_dir.exists() or git_dir.is_symlink():
            return SnapshotResult(False, "git snapshot skipped: pre-existing repository metadata is not trusted")
        for args in (["init", "--template="], ["add", "--all"], ["commit", "--allow-empty", "-m", "HAND task snapshot"]):
            result = subprocess.run(
                ["git", "-c", "user.name=HAND", "-c", "user.email=hand@localhost",
                 "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args],
                cwd=workspace, capture_output=True, text=True, timeout=15,
                env=_git_environment(),
            )
            if result.returncode:
                return SnapshotResult(False, f"git snapshot failed: {result.stderr.strip() or result.stdout.strip()}")
        return SnapshotResult(True, "git snapshot created (local only)")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return SnapshotResult(False, f"git snapshot failed: {exc}")


def _git_environment() -> dict[str, str]:
    import os
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null", GIT_TERMINAL_PROMPT="0")
    return env


def _report(tool: str, workspace: Path, allowed_directories: list[Path] | None,
            result_path: Path | None, action: Any) -> dict[str, Any]:
    # Reports always live in the task whitelist, including rejected input reports.
    default = fs_guard(f".hand/tools/{tool}-{uuid.uuid4().hex}.json", workspace=workspace)
    target = default
    result = {"tool": tool, "status": "blocked", "passed": False}
    try:
        if result_path is not None:
            target = fs_guard(result_path, workspace=workspace, allowed_directories=allowed_directories)
        target.parent.mkdir(parents=True, exist_ok=True)
        result.update(action(target))
        json.dumps(result, allow_nan=False)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        result = {"tool": tool, "status": "blocked", "passed": False, "error": f"{type(exc).__name__}: {exc}"}
    for key, suffix in (("stdout_path", ".stdout.log"), ("stderr_path", ".stderr.log")):
        try:
            path = fs_guard(target.with_suffix(suffix), workspace=workspace, allowed_directories=allowed_directories)
            if path.is_file():
                result[key] = str(path)
        except PathViolation as exc:
            result.update(status="blocked", passed=False, error=str(exc))
    result["report_path"] = str(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return result


def _command_report(argv: list[str], *, workspace: Path, target: Path,
                    timeout: float, cancel: Event | None, allowed_directories: list[Path] | None = None,
                    env: dict[str, str] | None = None) -> dict:
    stdout = fs_guard(target.with_suffix(".stdout.log"), workspace=workspace, allowed_directories=allowed_directories)
    stderr = fs_guard(target.with_suffix(".stderr.log"), workspace=workspace, allowed_directories=allowed_directories)
    result = run_process(argv, cwd=workspace, stdout=stdout,
                         stderr=stderr, timeout=timeout, cancel=cancel, env=env)
    if result["reason"] or result["exit_code"] != 0:
        stderr = Path(result["stderr_path"]).read_text(encoding="utf-8", errors="replace")
        result.update(status="blocked", passed=False,
                      error=result["reason"] or stderr or f"Process exit {result['exit_code']}")
    else:
        result.update(status="passed", passed=True)
    return result


def _files(workspace: Path, allowed: list[Path] | None) -> dict[Path, str]:
    result = {}
    for root in [workspace, *(allowed or [])]:
        for directory, folders, files in os.walk(root, followlinks=False):
            folders[:] = [name for name in folders if name not in (".git", ".hand")]
            for name in files:
                guarded = fs_guard(Path(directory) / name, workspace=workspace, allowed_directories=allowed)
                if guarded.is_file():
                    result[guarded] = hashlib.sha256(guarded.read_bytes()).hexdigest()
    return result


def blender_run(script: Path, *, workspace: Path, allowed_directories: list[Path] | None = None,
                executable: str | None = None, timeout: float = 300,
                result_path: Path | None = None, cancel: Event | None = None) -> dict[str, Any]:
    def action(target: Path) -> dict:
        guarded = fs_guard(script, workspace=workspace, allowed_directories=allowed_directories)
        if not guarded.is_file():
            raise FileNotFoundError(f"Blender script missing: {guarded}")
        before = _files(workspace, allowed_directories)
        binary = executable or os.environ.get("HAND_BLENDER", str(Path.home() / "workspace/tools/bin/blender"))
        # --python-exit-code: a script exception must fail the run; Blender
        # otherwise prints the traceback and still exits 0 (audit A01/G4).
        result = _command_report([binary, "--background", "--factory-startup", "-noaudio",
                                  "--python-exit-code", "1", "--python", str(guarded)],
                                 workspace=workspace, target=target, timeout=timeout, cancel=cancel, allowed_directories=allowed_directories)
        result["artifacts"] = [{"path": str(path), "sha256": digest}
                               for path, digest in _files(workspace, allowed_directories).items()
                               if before.get(path) != digest]
        return result
    return _pkg4_report("blender_run", workspace, allowed_directories, result_path, [script], action)


def _test_counts(output: str) -> tuple[int, int]:
    counts = {}
    for word in ("PASS", "FAIL"):
        totals = re.findall(rf"\b{word}\s*[:=]\s*(\d+)\b", output, re.IGNORECASE)
        details = len(re.findall(rf"^\s*{word}\b(?!\s*[:=])", output, re.MULTILINE | re.IGNORECASE))
        counts[word] = max(sum(map(int, totals)), details) if totals else details
    return counts["PASS"], counts["FAIL"]


def godot_run(project: Path, *, workspace: Path, allowed_directories: list[Path] | None = None,
              executable: str | None = None, script: Path | None = None, timeout: float = 300,
              result_path: Path | None = None, cancel: Event | None = None) -> dict[str, Any]:
    def action(target: Path) -> dict:
        guarded = fs_guard(project, workspace=workspace, allowed_directories=allowed_directories)
        manifest = fs_guard(guarded / "project.godot", workspace=workspace, allowed_directories=allowed_directories)
        if not manifest.is_file():
            raise FileNotFoundError(f"Godot project missing: {manifest}")
        binary = executable or os.environ.get("HAND_GODOT", str(Path.home() / "workspace/tools/bin/godot"))
        argv = [binary, "--headless", "--path", str(guarded)]
        if script:
            verified = fs_guard(script, workspace=workspace, allowed_directories=allowed_directories)
            if not verified.is_file():
                raise FileNotFoundError(f"Godot verify script missing: {verified}")
            argv.extend(["--script", str(verified)])
        result = _command_report(argv, workspace=workspace, target=target, timeout=timeout, cancel=cancel, allowed_directories=allowed_directories)
        output = "\n".join(Path(result[key]).read_text(encoding="utf-8", errors="replace")
                           for key in ("stdout_path", "stderr_path"))
        passed, failed = _test_counts(output)
        result.update(pass_count=passed, fail_count=failed)
        if result["passed"] and (failed or not passed):
            result.update(status="failed" if failed else "blocked", passed=False,
                          error=f"Godot verification FAIL={failed}, PASS={passed}; explicit PASS evidence required")
        return result
    # Secondary inputs count as inputs: the report and its logs must never
    # overwrite the project manifest or the verify script either (R2-06).
    inputs = [project, Path(project) / "project.godot"]
    if script:
        inputs.append(script)
    return _pkg4_report("godot_run", workspace, allowed_directories, result_path, inputs, action)


def _number(value: Any, label: str, *, nonnegative: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number")
    if nonnegative and value < 0:
        raise ValueError(f"{label} must be nonnegative")
    return float(value)


def gate_aabb(artifact: Path, *, workspace: Path, allowed_directories: list[Path] | None = None,
              ground_z: float = 0, ground_tolerance: float = 0.01,
              overlap_volume_threshold: float = 0, dimension_tolerance_ratio: float = 0.05,
              expected_dimensions: dict[str, list[float]] | None = None,
              result_path: Path | None = None) -> dict[str, Any]:
    """World-space Z-up bounds; tolerances are task policy, never guessed from mesh.

    Input: {"objects": [{"name": "part", "min": [x,y,z], "max": [x,y,z]}]}.
    Dimension expectations come from the task's gate_options, not the exporter.
    """
    def action(target: Path) -> dict:
        source = fs_guard(artifact, workspace=workspace, allowed_directories=allowed_directories)
        data = json.loads(source.read_text(encoding="utf-8"))
        ground = _number(ground_z, "ground_z")
        tolerance = _number(ground_tolerance, "ground_tolerance", nonnegative=True)
        overlap = _number(overlap_volume_threshold, "overlap_volume_threshold", nonnegative=True)
        ratio = _number(dimension_tolerance_ratio, "dimension_tolerance_ratio", nonnegative=True)
        objects = data["objects"]
        if not isinstance(objects, list) or not objects:
            raise ValueError("objects must be a nonempty list")
        bounds, checks = {}, []
        for obj in objects:
            name = obj["name"]
            if not isinstance(name, str) or not name or name in bounds:
                raise ValueError("Object names must be nonempty and unique")
            lo, hi = obj["min"], obj["max"]
            if len(lo) != 3 or len(hi) != 3:
                raise ValueError(f"{name}: min/max require three coordinates")
            lo = [_number(v, f"{name}.min") for v in lo]
            hi = [_number(v, f"{name}.max") for v in hi]
            if any(a >= b for a, b in zip(lo, hi)):
                raise ValueError(f"{name}: bounds must have positive dimensions")
            bounds[name] = (lo, hi)
            distance = lo[2] - ground
            checks.append({"check": "ground_contact", "object": name, "bottom_z": lo[2],
                           "ground_z": ground, "measured": distance, "threshold": tolerance,
                           "passed": abs(distance) <= tolerance})
        for index, (name, (lo, hi)) in enumerate(bounds.items()):
            for other, (olo, ohi) in list(bounds.items())[index + 1:]:
                extents = [max(0, min(b, d) - max(a, c)) for a, b, c, d in zip(lo, hi, olo, ohi)]
                volume = math.prod(extents)
                checks.append({"check": "intersection", "objects": [name, other],
                               "overlap_extents": extents, "measured": volume,
                               "threshold": overlap, "passed": volume <= overlap})
        for name, dimensions in (expected_dimensions or {}).items():
            if name not in bounds or len(dimensions) != 3:
                raise ValueError(f"Missing object or invalid expected dimensions: {name}")
            lo, hi = bounds[name]
            for axis, actual, expected in zip("xyz", [b - a for a, b in zip(lo, hi)], dimensions):
                expected = _number(expected, f"{name}.expected.{axis}")
                if expected <= 0:
                    raise ValueError("Expected dimensions must be positive")
                error = abs(actual - expected) / expected
                checks.append({"check": "dimension", "object": name, "axis": axis,
                               "actual": actual, "expected": expected, "measured": error,
                               "threshold": ratio, "passed": error <= ratio})
        passed = all(check["passed"] for check in checks)
        return {"status": "passed" if passed else "failed", "passed": passed, "checks": checks,
                "source_path": str(source), "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "dimensions_checked": len(expected_dimensions or {})}
    return _pkg4_report("gate_aabb", workspace, allowed_directories, result_path, [artifact], action)


def gate_clipsim(artifact: Path, reference: Path, *, workspace: Path,
                 allowed_directories: list[Path] | None = None, script: Path | None = None,
                 reference_sha256: str | None = None, reference_band: list[float] | None = None, timeout: float = 300,
                 result_path: Path | None = None, cancel: Event | None = None) -> dict[str, Any]:
    """Adapter protocol: sim.py --image PATH --reference PATH -> {"score": float}.

    A local adapter can wrap an installed CLIP script with a different CLI.
    No model download, network request or invented fallback score is performed.
    """
    def action(target: Path) -> dict:
        image = fs_guard(artifact, workspace=workspace, allowed_directories=allowed_directories)
        locked = fs_guard(reference, workspace=workspace, allowed_directories=allowed_directories)
        configured = script or os.environ.get("HAND_CLIPSIM_SCRIPT")
        if not configured:
            raise FileNotFoundError("CLIP script not configured (script / HAND_CLIPSIM_SCRIPT)")
        adapter = fs_guard(configured, workspace=workspace, allowed_directories=allowed_directories)
        for path in (image, locked, adapter):
            if not path.is_file():
                raise FileNotFoundError(f"CLIP input/script missing: {path}")
        if not reference_sha256 or reference_band is None:
            raise ValueError("Locked reference_sha256 and reference_band must be configured")
        if hashlib.sha256(locked.read_bytes()).hexdigest() != reference_sha256:
            raise ValueError("Locked reference sha256 mismatch")
        if len(reference_band) != 2:
            raise ValueError("reference_band requires [min, max]")
        low, high = [_number(v, "reference_band") for v in reference_band]
        if not -1 <= low <= high <= 1:
            raise ValueError("reference_band must be ordered within [-1, 1]")
        result = _command_report([sys.executable, str(adapter), "--image", str(image), "--reference", str(locked)],
                                 workspace=workspace, target=target, timeout=timeout, cancel=cancel, allowed_directories=allowed_directories,
                                 env={**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
                                      "HF_HUB_DISABLE_TELEMETRY": "1"})
        if not result["passed"]:
            return result
        payload = json.loads(Path(result["stdout_path"]).read_text(encoding="utf-8"))
        score = _number(payload["score"], "CLIP score")
        if not -1 <= score <= 1:
            raise ValueError("CLIP score outside [-1, 1]")
        passed = low <= score <= high
        result.update(score=score, reference_band=[low, high], reference_sha256=reference_sha256,
                      passed=passed, status="passed" if passed else "failed")
        return result
    # The resolved adapter script is an input too: the report and its logs
    # must never overwrite it (R2-06).
    inputs = [artifact, reference]
    configured = script or os.environ.get("HAND_CLIPSIM_SCRIPT")
    if configured:
        inputs.append(configured)
    return _pkg4_report("gate_clipsim", workspace, allowed_directories, result_path, inputs, action)


_VISION_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".webp": "image/webp"}

_DEFAULT_VISION_RUBRIC = (
    "请评审这张游戏美术渲染图，只按以下三项给一个 0-100 的整数总分："
    "1) 构图完整性：主体完整入画、关键部位没有被裁切；"
    "2) 明显穿模/破面：部件互相穿插、破洞、悬空、比例崩坏都要扣分并列出；"
    "3) 与低多边形（low-poly）项目画风的贴合度：块面感、配色与整体风格是否一致。"
)


def _openai_api_key() -> str | None:
    """Operator-provisioned key: OPENAI_API_KEY first, then OpenCode's auth store."""
    key = os.environ.get("OPENAI_API_KEY")
    if key:
        return key
    try:
        auth = json.loads((Path.home() / ".local/share/opencode/auth.json").read_text(encoding="utf-8"))
        value = auth.get("openai", {}).get("key") if isinstance(auth, dict) else None
    except (OSError, ValueError, AttributeError):
        return None
    return value if isinstance(value, str) and value else None


def gate_vision(artifact: Path, *, workspace: Path,
                allowed_directories: list[Path] | None = None,
                rubric: str | None = None, model: str = "gpt-6.1-sol",
                threshold: float = 70, timeout: float = 300,
                result_path: Path | None = None, cancel: Event | None = None) -> dict[str, Any]:
    """Vision-model review gate: one image scored against a written rubric.

    The image goes to the OpenAI chat/completions API (stdlib urllib, no SDK)
    as a base64 data URL. The model must answer with strict JSON
    {"score": int 0-100, "verdict": str, "issues": [str]}; anything else is a
    failed gate and the raw reply is kept as evidence. No key or no network
    is blocked, never a fabricated pass.
    """
    def action(target: Path) -> dict:
        image = fs_guard(artifact, workspace=workspace, allowed_directories=allowed_directories)
        if not image.is_file():
            raise FileNotFoundError(f"Vision input missing: {image}")
        mime = _VISION_MIME.get(image.suffix.lower())
        if mime is None:
            raise ValueError(f"Unsupported image type for vision gate: {image.suffix}")
        limit = _number(threshold, "threshold")
        if not 0 <= limit <= 100:
            raise ValueError("threshold must be within [0, 100]")
        seconds = _number(timeout, "timeout")
        if seconds <= 0:
            raise ValueError("timeout must be positive")
        data = image.read_bytes()
        base = {"model": model, "threshold": limit, "source_path": str(image),
                "source_sha256": hashlib.sha256(data).hexdigest()}
        if cancel is not None and cancel.is_set():
            return {**base, "status": "blocked", "passed": False, "error": "cancelled"}
        key = _openai_api_key()
        if not key:
            return {**base, "status": "blocked", "passed": False,
                    "error": "OpenAI API key not configured "
                             "(OPENAI_API_KEY or ~/.local/share/opencode/auth.json)"}
        prompt = (rubric or _DEFAULT_VISION_RUBRIC) + (
            '\n只输出一个 JSON 对象，不要输出任何其他文字：{"score": 0到100的整数, '
            '"verdict": "一句话结论", "issues": ["发现的问题，没有则空数组"]}')
        payload = {"model": model,
                   "messages": [{"role": "user", "content": [
                       {"type": "text", "text": prompt},
                       {"type": "image_url", "image_url": {
                           "url": f"data:{mime};base64," + base64.b64encode(data).decode("ascii")}}]}],
                   "response_format": {"type": "json_object"}}
        request = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"), method="POST",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=seconds) as response:
                reply = json.loads(response.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                detail = ""
            return {**base, "status": "blocked", "passed": False, "http_status": exc.code,
                    "error": f"HTTPError: {exc.reason}" + (f": {detail}" if detail else "")}
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            return {**base, "status": "blocked", "passed": False,
                    "error": f"{type(exc).__name__}: {exc}"}
        usage = reply.get("usage") if isinstance(reply, dict) else None
        content: Any = None
        try:
            content = reply["choices"][0]["message"]["content"]
            verdict_payload = json.loads(content)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            excerpt = content if isinstance(content, str) else json.dumps(reply, ensure_ascii=False)
            return {**base, "status": "failed", "passed": False, "usage": usage,
                    "error": f"Vision reply is not the required JSON: {type(exc).__name__}: {exc}",
                    "raw_excerpt": excerpt[:500]}
        problems = []
        score = verdict_payload.get("score") if isinstance(verdict_payload, dict) else None
        if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 100:
            problems.append("score must be an int within [0, 100]")
        verdict = verdict_payload.get("verdict") if isinstance(verdict_payload, dict) else None
        if not isinstance(verdict, str):
            problems.append("verdict must be a string")
        issues = verdict_payload.get("issues") if isinstance(verdict_payload, dict) else None
        if not isinstance(issues, list) or not all(isinstance(item, str) for item in issues):
            problems.append("issues must be a list of strings")
        if problems:
            return {**base, "status": "failed", "passed": False, "usage": usage,
                    "error": "Vision reply failed validation: " + "; ".join(problems),
                    "raw_excerpt": content[:500]}
        passed = score >= limit
        return {**base, "status": "passed" if passed else "failed", "passed": passed,
                "score": score, "verdict": verdict, "issues": issues, "usage": usage}
    return _pkg4_report("gate_vision", workspace, allowed_directories, result_path, [artifact], action)


def _pkg4_report(tool: str, workspace: Path, allowed: list[Path] | None,
                 result_path: Path | None, inputs: list[Any], action: Any) -> dict[str, Any]:
    """Reject output/input aliases before _report creates a caller's directory."""
    if result_path is not None:
        try:
            target = fs_guard(result_path, workspace=workspace, allowed_directories=allowed)
            outputs = {target}
            outputs.update(fs_guard(target.with_suffix(suffix), workspace=workspace, allowed_directories=allowed)
                           for suffix in (".stdout.log", ".stderr.log"))
            sources = {fs_guard(path, workspace=workspace, allowed_directories=allowed) for path in inputs}
            if outputs.intersection(sources):
                raise ValueError("Report/log paths must not overwrite inputs")
            if tool == "gate_render_review" and _inside(target, (workspace / "review_bundle").resolve()):
                raise ValueError("Review report must be outside review_bundle")
        except (OSError, ValueError, TypeError) as exc:
            error = f"{type(exc).__name__}: {exc}"
            return _report(tool, workspace, allowed, None,
                           lambda target: {"status": "blocked", "passed": False, "error": error})
    return _report(tool, workspace, allowed, result_path, action)


def _review_dimensions(data: bytes) -> list[int] | None:
    """Read common image headers, without decoding or claiming visual validation."""
    import struct

    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24 and data[12:16] == b"IHDR":
        dimensions = list(struct.unpack(">II", data[16:24]))
    elif data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        dimensions = list(struct.unpack("<HH", data[6:10]))
    elif data.startswith(b"\xff\xd8"):
        offset = 2
        while offset < len(data):
            if data[offset] != 0xff:
                return None
            while offset < len(data) and data[offset] == 0xff:
                offset += 1
            if offset >= len(data):
                return None
            marker = data[offset]
            offset += 1
            if marker in (0xd9, 0xda):
                return None
            if marker in (0x01, 0xd8, *range(0xd0, 0xd8)):
                continue
            if offset + 2 > len(data):
                return None
            length = int.from_bytes(data[offset:offset + 2], "big")
            if length < 2 or offset + length > len(data):
                return None
            if marker in (0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7,
                          0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf) and length >= 8:
                height, width = struct.unpack(">HH", data[offset + 3:offset + 7])
                return [width, height] if width and height else None
            offset += length
        return None
    else:
        return None
    return dimensions if all(dimensions) else None


def _review_entry(path: Path, *, relative: str, role: str,
                  source: Path | None = None, locked_sha256: str | None = None) -> dict:
    data = path.read_bytes()
    entry = {"path": relative, "role": role, "source_path": str(source) if source else None,
             "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data),
             "dimensions": _review_dimensions(data) if role in ("artifact", "reference") else None,
             "storage": "copy" if source else "generated"}
    if role == "reference":
        entry["locked_sha256"] = locked_sha256
    return entry


def gate_render_review(artifacts: list[Path], references: list[Path], *, workspace: Path,
                       task_id: str, gate_reports: list[Path] | None = None,
                       reference_sha256: dict[str, str] | None = None,
                       allowed_directories: list[Path] | None = None,
                       result_path: Path | None = None) -> dict[str, Any]:
    """Package evidence only. `passed` means packaging, never visual acceptance.

    Locks map reference source paths (relative to workspace or absolute) to SHA256.
    gates.json retains the JSON objects in input order; original report bytes are
    also copied. Existing bundles are preserved and require a fresh workspace.
    """
    def action(target: Path) -> dict:
        import shutil
        import tempfile

        result = {"status": "failed", "passed": False, "packaging_only": True,
                  "review_status": "pending", "quality_passed": None}
        try:
            if not isinstance(task_id, str) or not task_id.strip():
                raise ValueError("task_id must be a nonempty string")
            if not isinstance(artifacts, list) or not artifacts:
                return {**result, "error": "Artifact list is empty", "missing": ["artifacts"]}
            if not isinstance(references, list) or (gate_reports is not None and not isinstance(gate_reports, list)):
                raise ValueError("references and gate_reports must be lists of paths")
            bundle = project_path(workspace, "review_bundle")
            inputs, missing = [], []
            for role, paths in (("artifact", artifacts), ("reference", references), ("gate_report", gate_reports or [])):
                for index, path in enumerate(paths, 1):
                    source = fs_guard(path, workspace=workspace, allowed_directories=allowed_directories)
                    if source == target:
                        raise ValueError("Report path must not overwrite an input")
                    if not source.is_file():
                        missing.append({"role": role, "path": str(source), "reason": "missing or not a file"})
                        continue
                    data = source.read_bytes()
                    if not data:
                        missing.append({"role": role, "path": str(source), "reason": "empty file"})
                        continue
                    name = re.sub(r"[^A-Za-z0-9._-]", "_", source.name)
                    folder = {"artifact": "artifacts", "reference": "references", "gate_report": "gates"}[role]
                    inputs.append((role, source, data, f"{folder}/{index:04d}-{name}"))
            if missing:
                return {**result, "error": "Missing or empty review inputs", "missing": missing}
            if bundle.exists():
                raise FileExistsError("review_bundle already exists; existing bundle preserved, no new package created")
            reference_sources = {source for role, source, _, _ in inputs if role == "reference"}
            locks = {}
            if reference_sha256 is not None and not isinstance(reference_sha256, dict):
                raise ValueError("reference_sha256 must map reference paths to SHA256 strings")
            for path, digest in (reference_sha256 or {}).items():
                source = fs_guard(path, workspace=workspace, allowed_directories=allowed_directories)
                if source not in reference_sources:
                    raise ValueError(f"Locked reference is absent from references: {path}")
                if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
                    raise ValueError(f"Invalid locked reference sha256: {path}")
                digest = digest.lower()
                if source in locks and locks[source] != digest:
                    raise ValueError(f"Conflicting reference locks: {path}")
                locks[source] = digest
            gates = []

            def reject_nonfinite(value: str) -> Any:
                raise ValueError(f"Gate report contains a nonfinite JSON constant: {value}")

            for role, source, data, _ in inputs:
                if role == "reference" and source in locks:
                    actual = hashlib.sha256(data).hexdigest()
                    if actual != locks[source]:
                        return {**result, "error": "Locked reference sha256 mismatch", "source_path": str(source),
                                "expected_sha256": locks[source], "actual_sha256": actual}
                if role == "gate_report":
                    text = data.decode("utf-8-sig")
                    # Validate without converting numeric tokens through binary
                    # floats; gates.json must retain their original precision.
                    payload = json.loads(text, parse_float=str, parse_int=str,
                                         parse_constant=reject_nonfinite)
                    if not isinstance(payload, dict):
                        raise ValueError(f"Gate report must be a JSON object: {source}")
                    gates.append(text.strip())
            # Validate every input and lock before creating any bundle files.
            staging = Path(tempfile.mkdtemp(prefix=".review_bundle-", dir=workspace.resolve()))
            try:
                entries = []
                for role, source, data, relative in inputs:
                    destination = staging / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(data)
                    entries.append(_review_entry(destination, relative=relative, role=role,
                                                 source=source, locked_sha256=locks.get(source)))
                gates_path = staging / "gates.json"
                gates_path.write_text("[\n" + ",\n".join(gates) + "\n]\n", encoding="utf-8")
                entries.append(_review_entry(gates_path, relative="gates.json", role="gate_data"))
                readme = ["Packaging only; visual review and grading are pending.",
                          "Copies, not hard links. SHA256 covers stored bytes; dimensions are header metadata only.",
                          "manifest.json indexes all other files; its own SHA256 is in the tool report.",
                          f"Task: {json.dumps(task_id, ensure_ascii=True)}",
                          "manifest.json | generated manifest",
                          "README.txt | generated human-readable index"]
                readme.extend(f"{entry['path']} | {entry['role']} | source={json.dumps(entry['source_path'], ensure_ascii=True)}"
                              for entry in entries)
                index_path = staging / "README.txt"
                index_path.write_text("\n".join(readme) + "\n", encoding="utf-8")
                entries.append(_review_entry(index_path, relative="README.txt", role="index"))
                manifest = {"version": 1, "task_id": task_id, "packaging_only": True,
                            "review_status": "pending", "files": entries}
                manifest_path = staging / "manifest.json"
                manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=True, allow_nan=False) + "\n", encoding="utf-8")
                manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
                # Recheck managed output immediately before publication; never follow a symlink.
                bundle = project_path(workspace, "review_bundle")
                if bundle.exists():
                    raise FileExistsError("review_bundle appeared during packaging; existing bundle preserved")
                staging.rename(bundle)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
            return {**result, "status": "passed", "passed": True, "task_id": task_id,
                    "bundle_path": str(bundle), "manifest_path": str(bundle / "manifest.json"),
                    "manifest_sha256": manifest_sha256, "gates_path": str(bundle / "gates.json"),
                    "index_path": str(bundle / "README.txt"), "file_count": len(entries) + 1}
        except PathViolation as exc:
            return {**result, "status": "blocked", "error": f"PathViolation: {exc}"}
        except (OSError, ValueError, TypeError, KeyError) as exc:
            return {**result, "error": f"{type(exc).__name__}: {exc}"}
    inputs = [path for paths in (artifacts, references, gate_reports) if isinstance(paths, list) for path in paths]
    return _pkg4_report("gate_render_review", workspace, allowed_directories, result_path, inputs, action)


def mocap_metrics(artifact: Path, **options: Any) -> dict[str, Any]:
    raise NotImplementedError("Package 2+: mocap metrics")


def _ssh_argv(node: str, task: dict[str, Any] | list[str], *, workspace: Path,
              nodes_file: Path | None, allowed_directories: list[Path] | None,
              executable: str | None, connect_timeout: float) -> tuple[list[str], Path]:
    try:
        from .nodes import node_argv
    except ImportError as exc:
        raise ValueError("Node configuration requires the installed project Pydantic dependency") from exc
    return node_argv(node, task, workspace=workspace, nodes_file=nodes_file,
                     allowed_directories=allowed_directories, executable=executable,
                     connect_timeout=connect_timeout)


def ssh_dispatch(node: str, task: dict[str, Any] | list[str], *, workspace: Path,
                 nodes_file: Path | None = None, allowed_directories: list[Path] | None = None,
                 executable: str | None = None, connect_timeout: float = 10, timeout: float = 300,
                 result_path: Path | None = None, cancel: Event | None = None) -> dict[str, Any]:
    """Dispatch task={argv: [executable, ...], cwd?: remote directory} via SSH.

    A command array also accepts the node's default cwd. Configuration is
    Pydantic-validated, with optional ssh_args and via. No local shell runs;
    the remote login shell must support POSIX quoting and cd/exec.
    Timeout kills the local SSH process group; it cannot attest remote cleanup.
    """
    def action(target: Path) -> dict:
        result = {"status": "blocked", "passed": False, "node": node,
                  "exit_code": None, "timed_out": False}
        try:
            argv, source = _ssh_argv(node, task, workspace=workspace, nodes_file=nodes_file,
                                     allowed_directories=allowed_directories, executable=executable,
                                     connect_timeout=connect_timeout)
            seconds = _number(timeout, "timeout")
            if seconds <= 0:
                raise ValueError("timeout must be positive")
            evidence_paths = [target, target.with_suffix(".stdout.log"), target.with_suffix(".stderr.log")]
            if source in evidence_paths:
                raise ValueError("SSH report/log paths must not overwrite node configuration")
            command_result = _command_report(argv, workspace=workspace, target=target, timeout=seconds,
                                             cancel=cancel, allowed_directories=allowed_directories)
            result.update(command_result, nodes_file=str(source), timeout=seconds,
                          connect_timeout=connect_timeout, timed_out=command_result.get("reason") == "timeout")
            if not result["passed"] and not result["reason"] and result["exit_code"] != 255:
                result["status"] = "failed"
            if result.get("reason") and result["reason"] not in ("timeout", "cancelled"):
                # Spawn failures have no child stderr; retain the original OS diagnostic.
                with Path(result["stderr_path"]).open("a", encoding="utf-8") as log:
                    log.write(result["reason"] + "\n")
            return result
        except (OSError, ValueError, TypeError, KeyError) as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
            if hasattr(exc, "report"):
                result["config_error"] = exc.report
            for key, suffix in (("stdout_path", ".stdout.log"), ("stderr_path", ".stderr.log")):
                log = fs_guard(target.with_suffix(suffix), workspace=workspace, allowed_directories=allowed_directories)
                log.write_text(result["error"] + "\n" if key == "stderr_path" else "", encoding="utf-8")
                result[key] = str(log)
            return result
    configured = nodes_file or os.environ.get("HAND_NODES_FILE")
    return _pkg4_report("ssh_dispatch", workspace, allowed_directories, result_path,
                        [configured] if configured else [], action)


class _TextExtractor(HTMLParser):
    """Readable-text extraction: page text without script/style, plus title."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._title_parts: list[str] = []
        self._skipped = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in ("script", "style"):
            self._skipped += 1
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skipped:
            self._skipped -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._skipped:
            return
        if self._in_title:
            self._title_parts.append(data)
        elif data.strip():
            self._parts.append(data.strip())

    @property
    def title(self) -> str | None:
        title = " ".join("".join(self._title_parts).split())
        return title or None

    @property
    def text(self) -> str:
        return " ".join(" ".join(self._parts).split())


def web_fetch(url: str, *, timeout: float = 15, max_bytes: int = 2 * 1024 * 1024,
              max_chars: int = 20000) -> dict[str, Any]:
    """Fetch one http(s) page as readable text, standard library only.

    Text-class content types only; the body is capped at max_bytes and the
    extracted text at max_chars, with truncation reported, never hidden.
    Every failure is an honest blocked/failed report: nothing raises and no
    content is invented.
    """
    result: dict[str, Any] = {"tool": "web_fetch", "status": "blocked", "passed": False,
                              "url": url, "final_url": None, "content_type": None,
                              "http_status": None, "bytes": 0, "truncated": False,
                              "title": None, "text": ""}
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        result["error"] = f"Only http/https URLs with a host are allowed: {url!r}"
        return result
    try:
        seconds = _number(timeout, "timeout")
        if seconds <= 0:
            raise ValueError("timeout must be positive")
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive int")
        if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 0:
            raise ValueError("max_chars must be a nonnegative int")
    except ValueError as exc:
        result["error"] = f"ValueError: {exc}"
        return result
    request = urllib.request.Request(url, headers={"User-Agent": "talktothehand web_fetch"})
    try:
        with urllib.request.urlopen(request, timeout=seconds) as response:
            result["http_status"] = response.status
            result["final_url"] = response.geturl()
            content_type = response.headers.get_content_type()
            result["content_type"] = content_type
            if not (content_type.startswith("text/") or
                    content_type in ("application/json", "application/xhtml+xml")):
                result["error"] = f"Content type is not textual: {content_type}"
                return result
            body = response.read(max_bytes + 1)
            charset = response.headers.get_content_charset() or "utf-8"
    except urllib.error.HTTPError as exc:
        result.update(status="failed", http_status=exc.code, error=f"HTTPError: {exc.reason}")
        return result
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        result.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        return result
    if len(body) > max_bytes:
        result["truncated"] = True
        body = body[:max_bytes]
    result["bytes"] = len(body)
    try:
        text = body.decode(charset, errors="replace")
    except LookupError:
        text = body.decode("utf-8", errors="replace")
    if content_type == "text/html":
        extractor = _TextExtractor()
        extractor.feed(text)
        extractor.close()
        result["title"] = extractor.title
        text = extractor.text
    if len(text) > max_chars:
        result["truncated"] = True
        text = text[:max_chars]
    result["text"] = text
    result.update(status="passed", passed=True)
    return result


from .gui_tools import gui_screenshot, gui_ax_tree, gui_click, gui_type, gui_pick_file


TOOL_REGISTRY = {function.__name__: function for function in (
    fs_guard, git_snapshot, blender_run, godot_run, gate_aabb, gate_clipsim, gate_vision,
    gate_render_review, mocap_metrics, ssh_dispatch, web_fetch,
    gui_screenshot, gui_ax_tree, gui_click, gui_type, gui_pick_file,
)}
