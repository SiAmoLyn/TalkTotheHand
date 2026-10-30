"""2026-10-30 package: web_fetch, gate_vision, OpenCode model config fallback.

Fully offline: every urllib.request.urlopen is monkeypatched, and the
executor tests drive a fake opencode binary, exactly like test_opencode.py.
"""
import hashlib
import io
import json
from email.message import Message
from pathlib import Path
import sys
import urllib.error
import urllib.request

import pytest

from hand.contracts import TaskPackage
from hand.executors import OpenCodeExecutor, _opencode_config_model
from hand.graph import HandRunner
from hand.executors import FakeExecutor
from hand.tools import TOOL_REGISTRY, gate_vision, web_fetch

FIXTURES = Path(__file__).parent / "fixtures"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake-png-payload"


class FakeResponse:
    """Minimal stand-in for the object urllib.request.urlopen returns."""

    def __init__(self, body: bytes, *, content_type: str = "text/html",
                 url: str = "https://example.test/", status: int = 200):
        self._stream = io.BytesIO(body)
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        self._url = url
        self.status = status

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def geturl(self) -> str:
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def fake_urlopen(monkeypatch, response=None, error=None, capture=None):
    def _open(request, timeout=None):
        if capture is not None:
            capture.append(request)
        if error is not None:
            raise error
        return response
    monkeypatch.setattr(urllib.request, "urlopen", _open)


# ---------------------------------------------------------------- web_fetch

def test_web_fetch_html_extracts_text_and_title(monkeypatch):
    html = (b"<html><head><title>Hi Page</title><style>.a{color:red}</style></head>"
            b"<body><h1>Hello</h1><script>var secret = 1;</script><p>World</p></body></html>")
    fake_urlopen(monkeypatch, FakeResponse(html, url="https://example.test/final"))
    result = web_fetch("https://example.test/start")
    assert result["status"] == "passed" and result["passed"] is True
    assert result["title"] == "Hi Page"
    assert "Hello" in result["text"] and "World" in result["text"]
    assert "secret" not in result["text"] and "color" not in result["text"]
    assert result["final_url"] == "https://example.test/final"
    assert result["content_type"] == "text/html" and result["http_status"] == 200
    assert result["bytes"] == len(html) and result["truncated"] is False


def test_web_fetch_json_and_plain_text_pass_through(monkeypatch):
    fake_urlopen(monkeypatch, FakeResponse(b'{"a": 1}', content_type="application/json"))
    result = web_fetch("https://example.test/data")
    assert result["passed"] and result["text"] == '{"a": 1}' and result["title"] is None
    fake_urlopen(monkeypatch, FakeResponse(b"plain body", content_type="text/plain; charset=utf-8"))
    result = web_fetch("https://example.test/note")
    assert result["passed"] and result["text"] == "plain body"


def test_web_fetch_rejects_non_http_schemes_without_network(monkeypatch):
    def bomb(request, timeout=None):
        raise AssertionError("network must not be touched")
    monkeypatch.setattr(urllib.request, "urlopen", bomb)
    for url in ("ftp://example.test/file", "file:///etc/passwd", "example.test/no-scheme"):
        result = web_fetch(url)
        assert result["status"] == "blocked" and result["passed"] is False
        assert "http" in result["error"]


def test_web_fetch_blocks_non_text_content_type(monkeypatch):
    fake_urlopen(monkeypatch, FakeResponse(b"\x89PNG", content_type="image/png"))
    result = web_fetch("https://example.test/pic.png")
    assert result["status"] == "blocked" and result["passed"] is False
    assert result["content_type"] == "image/png" and "not textual" in result["error"]


def test_web_fetch_reports_byte_and_char_truncation(monkeypatch):
    fake_urlopen(monkeypatch, FakeResponse(b"x" * 100, content_type="text/plain"))
    result = web_fetch("https://example.test/big", max_bytes=10)
    assert result["passed"] and result["bytes"] == 10 and result["truncated"] is True
    assert result["text"] == "x" * 10
    fake_urlopen(monkeypatch, FakeResponse(b"abcdefghij", content_type="text/plain"))
    result = web_fetch("https://example.test/text", max_chars=5)
    assert result["text"] == "abcde" and result["truncated"] is True


def test_web_fetch_http_and_network_errors_are_failed_not_raised(monkeypatch):
    fake_urlopen(monkeypatch, error=urllib.error.HTTPError(
        "https://example.test/", 404, "Not Found", None, None))
    result = web_fetch("https://example.test/missing")
    assert result["status"] == "failed" and result["http_status"] == 404
    fake_urlopen(monkeypatch, error=urllib.error.URLError("connection refused"))
    result = web_fetch("https://example.test/down")
    assert result["status"] == "failed" and "URLError" in result["error"]


# -------------------------------------------------------------- gate_vision

def vision_reply(score=85, verdict="构图完整，无明显问题", issues=(), usage=None):
    content = json.dumps({"score": score, "verdict": verdict, "issues": list(issues)})
    payload = {"choices": [{"message": {"content": content}}],
               "usage": usage if usage is not None else {"total_tokens": 42}}
    return FakeResponse(json.dumps(payload).encode(), content_type="application/json",
                        url="https://api.openai.com/v1/chat/completions")


def stage_image(tmp_path):
    image = tmp_path / "shot.png"
    image.write_bytes(PNG_BYTES)
    return image


def test_gate_vision_passes_and_reports_model_usage_and_request(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    captured = []
    fake_urlopen(monkeypatch, vision_reply(score=85, issues=["右下角轻微裁切"]), capture=captured)
    result = gate_vision(stage_image(tmp_path), workspace=tmp_path)
    assert result["status"] == "passed" and result["passed"] is True
    assert result["score"] == 85 and result["issues"] == ["右下角轻微裁切"]
    assert result["model"] == "gpt-6.1-sol" and result["usage"] == {"total_tokens": 42}
    assert Path(result["report_path"]).is_file()
    (request,) = captured
    assert request.full_url == "https://api.openai.com/v1/chat/completions"
    assert request.get_header("Authorization") == "Bearer test-key"
    body = json.loads(request.data)
    assert body["model"] == "gpt-6.1-sol"
    content = body["messages"][0]["content"]
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_gate_vision_threshold_boundary(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    fake_urlopen(monkeypatch, vision_reply(score=70))
    result = gate_vision(stage_image(tmp_path), workspace=tmp_path, threshold=70)
    assert result["passed"] is True and result["status"] == "passed"
    fake_urlopen(monkeypatch, vision_reply(score=69))
    result = gate_vision(stage_image(tmp_path), workspace=tmp_path, threshold=70)
    assert result["passed"] is False and result["status"] == "failed" and result["score"] == 69


def test_gate_vision_bad_replies_fail_with_raw_excerpt(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    not_json = FakeResponse(json.dumps(
        {"choices": [{"message": {"content": "looks fine to me"}}]}).encode(),
        content_type="application/json")
    fake_urlopen(monkeypatch, not_json)
    result = gate_vision(stage_image(tmp_path), workspace=tmp_path)
    assert result["status"] == "failed" and result["passed"] is False
    assert result["raw_excerpt"] == "looks fine to me"
    fake_urlopen(monkeypatch, vision_reply(score=150))
    result = gate_vision(stage_image(tmp_path), workspace=tmp_path)
    assert result["status"] == "failed" and "score" in result["error"]
    missing = FakeResponse(json.dumps(
        {"choices": [{"message": {"content": json.dumps({"score": 50})}}]}).encode(),
        content_type="application/json")
    fake_urlopen(monkeypatch, missing)
    result = gate_vision(stage_image(tmp_path), workspace=tmp_path)
    assert result["status"] == "failed" and "verdict" in result["error"]


def test_gate_vision_without_key_is_blocked_and_offline(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "no-home"))
    def bomb(request, timeout=None):
        raise AssertionError("network must not be touched")
    monkeypatch.setattr(urllib.request, "urlopen", bomb)
    result = gate_vision(stage_image(tmp_path), workspace=tmp_path)
    assert result["status"] == "blocked" and result["passed"] is False
    assert "not configured" in result["error"]


def test_gate_vision_reads_key_from_opencode_auth_store(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    home = tmp_path / "home"
    store = home / ".local/share/opencode"
    store.mkdir(parents=True)
    (store / "auth.json").write_text(json.dumps({"openai": {"type": "api", "key": "auth-file-key"}}))
    monkeypatch.setenv("HOME", str(home))
    captured = []
    fake_urlopen(monkeypatch, vision_reply(score=90), capture=captured)
    result = gate_vision(stage_image(tmp_path), workspace=tmp_path)
    assert result["passed"] is True
    assert captured[0].get_header("Authorization") == "Bearer auth-file-key"


def test_gate_vision_http_error_is_blocked(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    fake_urlopen(monkeypatch, error=urllib.error.HTTPError(
        "https://api.openai.com/v1/chat/completions", 401, "Unauthorized", None,
        io.BytesIO(b'{"error": "bad key"}')))
    result = gate_vision(stage_image(tmp_path), workspace=tmp_path)
    assert result["status"] == "blocked" and result["http_status"] == 401


def test_registry_contains_new_tools():
    assert TOOL_REGISTRY["web_fetch"] is web_fetch
    assert TOOL_REGISTRY["gate_vision"] is gate_vision


def test_graph_runs_gate_vision_as_real_gate(monkeypatch, tmp_path, task_data):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    fake_urlopen(monkeypatch, vision_reply(score=90))
    task_data.update(deliverables=["render.png"], gates=["gate_vision"],
                     gate_options={"gate_vision": {"artifact": "render.png"}})

    class PngExecutor(FakeExecutor):
        def execute(self, task, workspace, **kwargs):
            result = super().execute(task, workspace, **kwargs)
            target = Path(result.artifacts[0].path)
            target.write_bytes(PNG_BYTES)
            result.artifacts[0].sha256 = hashlib.sha256(PNG_BYTES).hexdigest()
            return result

    with HandRunner(tmp_path, executor=PngExecutor()) as runner:
        state = runner.run(TaskPackage.model_validate(task_data))
    assert state["status"] == "RETURNED"
    report = state["evidence"]["gate_reports"]["gate_vision"]
    assert report["passed"] is True and report["score"] == 90


# ------------------------------------------- OpenCode model config fallback

def write_config(root: Path, payload) -> Path:
    directory = root / "opencode"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "opencode.json"
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload))
    return path


def test_config_model_xdg_beats_home_and_malformed_is_skipped(tmp_path, monkeypatch):
    xdg, home = tmp_path / "xdg", tmp_path / "home"
    write_config(xdg, {"model": "xdg/model"})
    write_config(home / ".config", {"model": "home/model"})
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.setenv("HOME", str(home))
    assert _opencode_config_model() == "xdg/model"
    write_config(xdg, "{not json")
    assert _opencode_config_model() == "home/model"
    write_config(home / ".config", {"model": "  "})
    assert _opencode_config_model() is None


def fake_binary(tmp_path) -> str:
    content = (FIXTURES / "opencode-normal.jsonl").read_text()
    path = tmp_path / "fake-opencode"
    path.write_text(f"#!{sys.executable}\n"
                    "import json, sys\nfrom pathlib import Path\n"
                    "Path('args.json').write_text(json.dumps(sys.argv[1:]))\n"
                    f"print({content!r}, end='')\n")
    path.chmod(0o700)
    return str(path)


def run_live(tmp_path, task_data, monkeypatch, *, env_model=None, config_model=None):
    """Drive the real (non-fixture) executor path against the fake binary."""
    (tmp_path / "demo.txt").write_text("pre-staged deliverable")
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    if config_model is not None:
        xdg = tmp_path / "xdg"
        write_config(xdg, {"model": config_model})
        monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    else:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-empty"))
    if env_model is None:
        monkeypatch.delenv("HAND_OPENCODE_MODEL", raising=False)
    else:
        monkeypatch.setenv("HAND_OPENCODE_MODEL", env_model)
    task = TaskPackage.model_validate(task_data)
    result = OpenCodeExecutor(executable=fake_binary(tmp_path)).execute(
        task, tmp_path, attempt=1, allowed_directories=[])
    return result


def chosen_model(tmp_path, result) -> str:
    args = json.loads((tmp_path / "args.json").read_text())
    return args[args.index("-m") + 1]


def test_executor_falls_back_to_global_config_model(tmp_path, task_data, monkeypatch):
    result = run_live(tmp_path, task_data, monkeypatch, config_model="cfg/fallback-model")
    assert result.status == "in_progress"
    assert result.cost.model == "cfg/fallback-model"
    assert chosen_model(tmp_path, result) == "cfg/fallback-model"


def test_executor_env_model_beats_global_config(tmp_path, task_data, monkeypatch):
    result = run_live(tmp_path, task_data, monkeypatch,
                      env_model="env/model", config_model="cfg/fallback-model")
    assert result.cost.model == "env/model"
    assert chosen_model(tmp_path, result) == "env/model"


def test_executor_task_model_beats_env_and_config(tmp_path, task_data, monkeypatch):
    task_data["opencode"] = {"model": "task/model"}
    result = run_live(tmp_path, task_data, monkeypatch,
                      env_model="env/model", config_model="cfg/fallback-model")
    assert result.cost.model == "task/model"


def test_executor_without_any_model_source_still_blocks(tmp_path, task_data, monkeypatch):
    result = run_live(tmp_path, task_data, monkeypatch)
    assert result.status == "blocked"
    assert any("model not configured" in e for e in result.unresolved)
