"""Only a local fake Cua executable is ever launched by these tests."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch

from hand.gui_tools import gui_screenshot, gui_ax_tree, gui_click, gui_type, gui_pick_file
from hand.tools import TOOL_REGISTRY


ROOT = Path(__file__).resolve().parents[1]


class GuiTests(unittest.TestCase):
    def setUp(self):
        directory = ROOT / "work/pkg5-gui-tests"
        directory.mkdir(parents=True, exist_ok=True)
        self.temp = TemporaryDirectory(dir=directory)
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.fake = self.work / "fake-cua"
        self.program("""if op in ('get_desktop_state', 'get_window_state'):
    Path(args[args.index('--screenshot-out-file') + 1]).write_bytes(b'\\x89PNG\\r\\n\\x1a\\nfixed screenshot bytes')
    print('{"success": true}')
elif op == 'get_accessibility_tree':
    print('{"role":"window","children":[{"element_token":"ELEMENT_ID"}]}')
else:
    print('{"success": true, "source": "fake"}')
""")
        environment = patch.dict(os.environ, {"HAND_CUA_BIN": str(self.fake), "PYTHONDONTWRITEBYTECODE": "1"})
        environment.start()
        self.addCleanup(environment.stop)

    def program(self, body):
        self.fake.write_text(f"#!{sys.executable}\nimport sys, json\nfrom pathlib import Path\n"
                             "args = sys.argv[1:]\n"
                             "Path('cua-argv.json').write_text(json.dumps(args))\n"
                             # The unchanged prewarm block runs the script without arguments.
                             "if args:\n"
                             "    assert len(args) >= 3 and args[0] == 'call', args\n"
                             "    op = args[1]\n"
                             "    payload = json.loads(args[2])\n"
                             "    assert isinstance(payload, dict), payload\n"
                             "    schemas = {'get_desktop_state': set(), 'get_window_state': {'pid', 'window_id', 'tree_format'},\n"
                             "               'get_accessibility_tree': {'pid'}, 'click': {'pid', 'x', 'y', 'element_token'},\n"
                             "               'type_text': {'pid', 'element_token', 'text'}, 'run_script': {'session', 'timeout_ms', 'script'}}\n"
                             "    assert op in schemas and payload.keys() <= schemas[op], (op, payload)\n"
                             "    if op in ('get_desktop_state', 'get_window_state'):\n"
                             "        assert len(args) == 5 and args[3] == '--screenshot-out-file', args\n"
                             "    else:\n"
                             "        assert len(args) == 3, args\n"
                             "    if op == 'click':\n"
                             "        assert 'element_token' in payload or {'pid', 'x', 'y'} <= payload.keys(), payload\n"
                             "    if op == 'type_text':\n"
                             "        assert isinstance(payload.get('text'), str), payload\n" + body)
        self.fake.chmod(0o700)

    def check(self, result, status="passed"):
        self.assertEqual(result["status"], status, result)
        self.assertEqual(json.loads(Path(result["report_path"]).read_text()), result)
        self.assertTrue(self.work.resolve() in Path(result["report_path"]).parents)
        return result

    def argv(self, tool, arguments, *, screenshot=None):
        args = json.loads((self.work / "cua-argv.json").read_text())
        self.assertEqual(args[:2], ["call", tool])
        self.assertEqual(json.loads(args[2]), arguments)
        self.assertEqual(args[3:], [] if screenshot is None else ["--screenshot-out-file", screenshot])

    def test_screenshot_new_file_hash_and_artifact_registration(self):
        report = self.check(gui_screenshot(workspace=self.work))
        path = Path(report["path"])
        self.assertEqual(path.read_bytes(), b"\x89PNG\r\n\x1a\nfixed screenshot bytes")
        self.assertEqual(report["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(report["artifacts"], [{"path": str(path), "sha256": report["sha256"]}])
        self.assertEqual(path.parent, self.work / ".hand/gui")
        self.argv("get_desktop_state", {}, screenshot=str(path))
        again = self.check(gui_screenshot(workspace=self.work))
        self.assertNotEqual(report["path"], again["path"])

    def test_window_screenshot_uses_supplied_pid_and_window(self):
        report = self.check(gui_screenshot(workspace=self.work, pid=123, window_id=456))
        self.argv("get_window_state", {"pid": 123, "window_id": 456, "tree_format": "elements"},
                  screenshot=report["path"])

    def test_screenshot_rejects_non_png_bytes(self):
        # Audit A11: a hash alone does not prove the artifact is an image.
        self.program("if op in ('get_desktop_state', 'get_window_state'):\n"
                     "    Path(args[args.index('--screenshot-out-file') + 1]).write_bytes(b'not a PNG')\n"
                     "    print('{\"success\": true}')\n")
        self.check(gui_screenshot(workspace=self.work), "blocked")

    def test_screenshot_rejects_string_false_receipt(self):
        # Audit G5: a string "false" success field is not a success receipt.
        self.program("if op in ('get_desktop_state', 'get_window_state'):\n"
                     "    Path(args[args.index('--screenshot-out-file') + 1]).write_bytes(bytes([137]) + b'PNG' + bytes([13, 10, 26, 10]) + b'x')\n"
                     "    print('{\"success\": \"false\"}')\n")
        self.check(gui_screenshot(workspace=self.work), "blocked")

    def test_accessibility_tree_is_json_with_hash(self):
        report = self.check(gui_ax_tree(workspace=self.work))
        self.assertEqual(json.loads(Path(report["path"]).read_text())["children"][0]["element_token"], "ELEMENT_ID")
        self.assertEqual(report["sha256"], hashlib.sha256(Path(report["path"]).read_bytes()).hexdigest())
        self.assertEqual(report["artifacts"], [{"path": report["path"], "sha256": report["sha256"]}])
        self.argv("get_accessibility_tree", {})
        self.check(gui_ax_tree(workspace=self.work, pid=123))
        self.argv("get_accessibility_tree", {"pid": 123})

    def test_accessibility_tree_array_is_supported(self):
        self.program("print('[{\"role\":\"button\",\"element_token\":\"TOKEN\"}]')\n")
        report = self.check(gui_ax_tree(workspace=self.work))
        self.assertEqual(json.loads(Path(report["path"]).read_text())[0]["element_token"], "TOKEN")

    def test_click_by_element_or_coordinates_and_type_literal_text(self):
        self.check(gui_click(workspace=self.work, element="ELEMENT_ID"))
        self.argv("click", {"element_token": "ELEMENT_ID"})
        self.check(gui_click(workspace=self.work, x=10, y=20, pid=123))
        self.argv("click", {"pid": 123, "x": 10, "y": 20})
        text = "line 1\n'; $(touch INJECTED); `touch ALSO_INJECTED`"
        self.check(gui_type(text, workspace=self.work, element="ELEMENT_ID"))
        self.argv("type_text", {"element_token": "ELEMENT_ID", "text": text})
        self.check(gui_type("text", workspace=self.work))
        self.argv("type_text", {"text": "text"})
        self.check(gui_type("text", workspace=self.work, pid=123))
        self.argv("type_text", {"pid": 123, "text": "text"})
        token = "token 中文 '; $(touch INJECTED); `touch ALSO_INJECTED`\n--pid=9"
        self.check(gui_click(workspace=self.work, element=token))
        self.argv("click", {"element_token": token})
        self.assertFalse((self.work / "INJECTED").exists())
        self.assertFalse((self.work / "ALSO_INJECTED").exists())

    def test_click_coordinates_without_pid_are_blocked_before_launch(self):
        report = self.check(gui_click(workspace=self.work, x=10, y=20), "blocked")
        self.assertEqual(report["reason"], "pid_required")
        self.assertEqual(report["missing_fields"], ["pid"])
        self.assertIn("pid", report["error"])
        self.assertFalse((self.work / "cua-argv.json").exists())

    def test_coordinate_type_is_blocked_without_composite_execution(self):
        report = self.check(gui_type("text", workspace=self.work, x=1, y=2), "blocked")
        self.assertEqual(report["reason"], "pid_required")
        report = self.check(gui_type("text", workspace=self.work, x=1, y=2, pid=123), "blocked")
        self.assertEqual(report["reason"], "coordinate_typing_not_supported")
        self.assertFalse((self.work / "cua-argv.json").exists())

    def test_file_picker_validates_then_requires_pid(self):
        source = self.work / "file with spaces.txt"
        source.write_text("synthetic")
        for binary in (str(self.fake), ""):
            with patch.dict(os.environ, {"HAND_CUA_BIN": binary}):
                report = self.check(gui_pick_file(source, workspace=self.work), "blocked")
            self.assertEqual(report["reason"], "pid_required")
            self.assertEqual(report["missing_fields"], ["pid"])
            self.assertEqual(report["artifacts"], [])
            self.assertNotIn("selected_path", report)
            self.assertNotIn("receipt", report)
        self.assertFalse((self.work / "cua-argv.json").exists())

    def test_file_picker_with_pid_blocks_when_binary_unconfigured(self):
        source = self.work / "input.txt"
        source.write_text("synthetic")
        with patch.dict(os.environ, {"HAND_CUA_BIN": ""}):
            report = self.check(gui_pick_file(source, workspace=self.work, pid=4242), "blocked")
        self.assertEqual(report["code"], "cua_unconfigured")
        self.assertFalse((self.work / "cua-argv.json").exists())

    def test_file_picker_maps_run_script_stages(self):
        source = self.work / "input.txt"
        source.write_text("synthetic")
        cases = (
            ('{"stage": "not_found"}', "panel_not_found"),
            ('{"stage": "not_drivable", "detail": "ax empty"}', "panel_not_drivable"),
            ('{"stage": "unconfirmed"}', "pick_failed"),
        )
        for result_json, reason in cases:
            payload = json.dumps({"ok": True, "value": json.loads(result_json)})
            self.program("if op == 'run_script':\n"
                         f"    print({payload!r})\n"
                         "else:\n"
                         "    print('{\"success\": true}')\n")
            report = self.check(gui_pick_file(source, workspace=self.work, pid=4242), "blocked")
            self.assertEqual(report["reason"], reason, report)
            self.assertNotIn("selected_path", report)
            args = json.loads((self.work / "cua-argv.json").read_text())
            self.assertEqual(args[:2], ["call", "run_script"])
            script_args = json.loads(args[2])
            self.assertEqual(script_args["session"], "hand-pick-file")
            self.assertIn(str(source), script_args["script"])
            self.assertIn("4242", script_args["script"])

    def test_file_picker_passes_only_when_panel_closes(self):
        source = self.work / "input.txt"
        source.write_text("synthetic")
        self.program("if op == 'run_script':\n"
                     "    print('{\"ok\": true, \"value\": {\"stage\": \"opened\", \"opened\": true}}')\n"
                     "else:\n"
                     "    print('{\"success\": true}')\n")
        report = self.check(gui_pick_file(source, workspace=self.work, pid=4242))
        self.assertEqual(report["selected_path"], str(source))
        self.assertEqual(report["receipt"]["stage"], "opened")

    def test_file_picker_garbage_script_result_is_blocked(self):
        source = self.work / "input.txt"
        source.write_text("synthetic")
        self.program("print('{\"ok\": true}')\n")
        report = self.check(gui_pick_file(source, workspace=self.work, pid=4242), "blocked")
        self.assertEqual(report["reason"], "pick_failed")

    def test_all_five_tools_are_registered(self):
        for tool in (gui_screenshot, gui_ax_tree, gui_click, gui_type, gui_pick_file):
            self.assertIs(TOOL_REGISTRY[tool.__name__], tool)

    def test_driver_operations_block_when_binary_unconfigured_or_missing(self):
        for binary in ("", str(self.work / "missing-cua")):
            with patch.dict(os.environ, {"HAND_CUA_BIN": binary}):
                for function, options in ((gui_screenshot, {}), (gui_ax_tree, {}),
                                          (gui_click, {"element": "ELEMENT_ID"}),
                                          (gui_type, {"text": "text"})):
                    report = self.check(function(workspace=self.work, **options), "blocked")
                    self.assertEqual(report["code"], "cua_unconfigured")
        self.assertFalse((self.work / "cua-argv.json").exists())

    def test_driver_operations_preserve_nonzero_failure_stderr(self):
        self.program("print('fake driver failure', file=sys.stderr)\nsys.exit(9)\n")
        for function, options in ((gui_screenshot, {}), (gui_ax_tree, {}), (gui_click, {"x": 1, "y": 2, "pid": 123}),
                                  (gui_type, {"text": "text"})):
            report = self.check(function(workspace=self.work, **options), "blocked")
            self.assertEqual(report["exit_code"], 9)
            self.assertIn("fake driver failure", Path(report["stderr_path"]).read_text())
            self.assertEqual(report["artifacts"], [])

    def test_exit_zero_without_screenshot_is_blocked(self):
        self.program("print('{\"ok\":true}')\n")
        self.check(gui_screenshot(workspace=self.work), "blocked")

    def test_empty_screenshot_or_invalid_daemon_json_is_blocked(self):
        self.program("Path(args[4]).write_bytes(b'')\nprint('{\"success\":true}')\n")
        self.check(gui_screenshot(workspace=self.work), "blocked")
        for output in ("garbage", '{"success":false}', '{"error":"denied"}', '{"value":NaN}'):
            self.program(f"Path(args[4]).write_bytes(b'bytes')\nprint({output!r})\n")
            self.check(gui_screenshot(workspace=self.work), "blocked")

    def test_invalid_json_and_negative_receipt_are_blocked(self):
        for output in ("garbage", '{"ok":false}', '{"success":false}', '{}', 'null',
                       '{"ok":true,"success":false}', '{"success":"true"}'):
            self.program(f"print({output!r})\n")
            self.check(gui_click(workspace=self.work, element="ELEMENT_ID"), "blocked")
            self.check(gui_type("text", workspace=self.work), "blocked")

    def test_explicit_ok_and_success_receipts_are_supported(self):
        for output in ('{"ok":true}', '{"success":true}'):
            self.program(f"print({output!r})\n")
            for result in (gui_click(workspace=self.work, element="ELEMENT_ID"), gui_type("text", workspace=self.work)):
                self.assertEqual(self.check(result)["receipt"], json.loads(output))

    def test_invalid_tree_and_nan_are_blocked(self):
        for output in ("garbage", 'null', '{"ok":false}', '{"error":"denied"}', '{"value":NaN}'):
            self.program(f"print({output!r})\n")
            self.check(gui_ax_tree(workspace=self.work), "blocked")

    def test_picker_missing_input_directory_and_escape_fail_validation(self):
        for path in (Path("missing.txt"), Path("../outside.txt"), self.work):
            report = self.check(gui_pick_file(path, workspace=self.work), "blocked")
            self.assertNotEqual(report.get("reason"), "pid_required")
        self.assertFalse((self.work / "cua-argv.json").exists())

    def test_picker_honors_explicit_input_whitelist(self):
        source = self.work.parent / (self.work.name + "-allowed.txt")
        source.write_text("synthetic")
        self.addCleanup(source.unlink)
        rejected = self.check(gui_pick_file(source, workspace=self.work), "blocked")
        self.assertNotEqual(rejected.get("reason"), "pid_required")
        allowed = self.check(gui_pick_file(source, workspace=self.work,
                                          allowed_directories=[self.work.parent]), "blocked")
        self.assertEqual(allowed["reason"], "pid_required")
        self.assertFalse((self.work / "cua-argv.json").exists())

    def test_report_and_logs_never_overwrite_file_picker_input(self):
        for name in ("report.json", "report.stdout.log", "report.stderr.log"):
            source = self.work / name
            source.write_text("preserve")
            self.check(gui_pick_file(source, workspace=self.work, result_path=self.work / "report.json"), "blocked")
            self.assertEqual(source.read_text(), "preserve")

    def test_invalid_element_coordinates_and_timeout_do_not_launch(self):
        for options in ({}, {"x": 1}, {"x": -1, "y": 2}, {"x": float("nan"), "y": 1},
                        {"x": True, "y": 1}, {"x": 1, "y": float("inf")},
                        {"element": "ELEMENT_ID", "x": 1, "y": 1}, {"element": ""}, {"element": "  "}):
            self.check(gui_click(workspace=self.work, **options), "blocked")
        self.check(gui_type("bad\x00text", workspace=self.work), "blocked")
        self.check(gui_screenshot(workspace=self.work, timeout=0), "blocked")
        self.assertFalse((self.work / "cua-argv.json").exists())

    def test_invalid_pid_and_window_id_do_not_launch(self):
        for pid in (0, -1, True, 1.5, "123"):
            self.check(gui_click(workspace=self.work, pid=pid, x=1, y=2), "blocked")
            self.check(gui_type("text", workspace=self.work, pid=pid), "blocked")
            self.check(gui_ax_tree(workspace=self.work, pid=pid), "blocked")
            self.check(gui_screenshot(workspace=self.work, pid=pid), "blocked")
        for window_id in (-1, True, "456"):
            self.check(gui_screenshot(workspace=self.work, pid=123, window_id=window_id), "blocked")
        report = self.check(gui_screenshot(workspace=self.work, window_id=456), "blocked")
        self.assertEqual(report["reason"], "pid_required")
        self.assertFalse((self.work / "cua-argv.json").exists())

    def test_cancelled_and_timeout_only_target_fake_process(self):
        cancel = Event()
        cancel.set()
        self.check(gui_screenshot(workspace=self.work, cancel=cancel), "blocked")
        self.assertFalse((self.work / "cua-argv.json").exists())
        # macOS first-launch evaluation of a fresh script path can eat most of
        # the 0.5s budget before the child prints (same platform behaviour as
        # the PKG2 opencode timeout test). Warm the path with one completed
        # run first; the timed assertions below are unchanged.
        self.program("print('warm', flush=True)\n")
        import subprocess
        subprocess.run([str(self.fake)], cwd=self.work, timeout=10, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.program("import time\nprint('partial', flush=True)\ntime.sleep(30)\n")
        report = self.check(gui_screenshot(workspace=self.work, timeout=0.5), "blocked")
        self.assertEqual(report["reason"], "timeout")
        self.assertIn("partial", Path(report["stdout_path"]).read_text())

    def test_output_path_escape_is_blocked_before_driver_launch(self):
        self.check(gui_screenshot(workspace=self.work, result_path=Path("../outside.json")), "blocked")
        self.assertFalse((self.work / "cua-argv.json").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
