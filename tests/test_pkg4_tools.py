"""Offline SSH/bundle regressions; also runnable with stdlib unittest on the Air."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import signal
import struct
import sys
from tempfile import TemporaryDirectory
from threading import Event
import time
import unittest
from unittest.mock import patch
import zlib

from hand.tools import gate_render_review, ssh_dispatch


ROOT = Path(__file__).resolve().parents[1]


def png(width=2, height=3):
    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress((b"\x00" + b"\x50\xa0\xf0" * width) * height))
            + chunk(b"IEND", b""))


class WorkspaceTest(unittest.TestCase):
    def setUp(self):
        directory = ROOT / "work/pkg4-tests"
        directory.mkdir(parents=True, exist_ok=True)
        self.temp = TemporaryDirectory(dir=directory)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root / "task"
        self.work.mkdir()

    def assert_report(self, report, status):
        self.assertEqual(report["status"], status, report)
        self.assertIs(report["passed"], status == "passed")
        path = Path(report["report_path"])
        self.assertTrue(self.work.resolve() in path.parents)
        self.assertEqual(json.loads(path.read_text()), report)


class SSHDispatchTests(WorkspaceTest):
    def setUp(self):
        super().setUp()
        self.config = self.work / "nodes.json"
        self.settings = {"render-node": {"target": "user@HOST_ALIAS", "cwd": "/REMOTE/WORKSPACE"}}
        self.config.write_text(json.dumps(self.settings))
        self.fake = self.work / "fake-ssh"
        self.program("import os\nos.write(1, b'fixed stdout\\x00\\xff')\nos.write(2, b'fixed stderr\\xfe')\n")

    def program(self, body):
        self.fake.write_text(f"#!{sys.executable}\nimport json, sys\nfrom pathlib import Path\n"
                             "Path('ssh-argv.json').write_text(json.dumps(sys.argv[1:]))\n" + body)
        self.fake.chmod(0o700)

    def dispatch(self, **options):
        defaults = {"workspace": self.work, "nodes_file": self.config, "executable": str(self.fake)}
        defaults.update(options)
        return ssh_dispatch("render-node", {"argv": ["worker", "--render", "scene with spaces"]}, **defaults)

    def test_success_raw_bytes_argv_and_default_remote_cwd(self):
        report = self.dispatch(connect_timeout=7, timeout=2)
        self.assert_report(report, "passed")
        self.assertEqual(report["exit_code"], 0)
        self.assertFalse(report["timed_out"])
        self.assertEqual(Path(report["stdout_path"]).read_bytes(), b"fixed stdout\x00\xff")
        self.assertEqual(Path(report["stderr_path"]).read_bytes(), b"fixed stderr\xfe")
        args = json.loads((self.work / "ssh-argv.json").read_text())
        self.assertEqual(args[:6], ["-o", "BatchMode=yes", "-o", "ConnectTimeout=7", "--", "user@HOST_ALIAS"])
        self.assertEqual(args[-1], "cd /REMOTE/WORKSPACE && exec worker --render 'scene with spaces'")
        self.assertEqual(report["command"], [str(self.fake), *args])

    def test_environment_config_and_explicit_override(self):
        with patch.dict(os.environ, {"HAND_NODES_FILE": "nodes.json"}):
            self.assert_report(self.dispatch(nodes_file=None), "passed")
        with patch.dict(os.environ, {"HAND_NODES_FILE": "absent.json"}):
            self.assert_report(self.dispatch(), "passed")

    def test_nonzero_exit_and_original_diagnostic(self):
        self.program("print('partial output')\nprint('remote worker failed', file=sys.stderr)\nsys.exit(23)\n")
        report = self.dispatch()
        self.assert_report(report, "failed")
        self.assertEqual(report["exit_code"], 23)
        self.assertEqual(Path(report["stderr_path"]).read_text(), "remote worker failed\n")
        self.assertIn("remote worker failed", report["error"])

    def test_unreachable_is_blocked_with_original_stderr(self):
        self.program("print('ssh: connection refused', file=sys.stderr)\nsys.exit(255)\n")
        report = self.dispatch()
        self.assert_report(report, "blocked")
        self.assertEqual(report["exit_code"], 255)
        self.assertEqual(Path(report["stderr_path"]).read_text(), "ssh: connection refused\n")

    def test_timeout_kills_fake_ssh_and_retains_unterminated_logs(self):
        self.program("import os, time\nPath('ssh.pid').write_text(str(os.getpid()))\n"
                     "os.write(1, b'partial stdout')\nos.write(2, b'partial stderr')\n"
                     "Path('written').write_text('both pipes written')\ntime.sleep(30)\n")
        started = time.monotonic()
        report = self.dispatch(timeout=1)
        self.assertLess(time.monotonic() - started, 3)
        self.assert_report(report, "blocked")
        self.assertTrue(report["timed_out"])
        self.assertEqual(report["reason"], "timeout")
        self.assertEqual(report["exit_code"], -signal.SIGKILL)
        self.assertTrue((self.work / "written").is_file(), "fake SSH must write before the deadline")
        self.assertEqual(Path(report["stdout_path"]).read_bytes(), b"partial stdout")
        self.assertEqual(Path(report["stderr_path"]).read_bytes(), b"partial stderr")
        with self.assertRaises(ProcessLookupError):
            os.kill(int((self.work / "ssh.pid").read_text()), 0)

    def test_cancelled_before_launch(self):
        cancel = Event()
        cancel.set()
        report = self.dispatch(cancel=cancel)
        self.assert_report(report, "blocked")
        self.assertEqual(report["reason"], "cancelled")
        self.assertIsNone(report["exit_code"])
        self.assertFalse((self.work / "ssh-argv.json").exists())

    def test_missing_executable_is_blocked_with_os_error_log(self):
        report = self.dispatch(executable=str(self.work / "missing-ssh"))
        self.assert_report(report, "blocked")
        self.assertIsNone(report["exit_code"])
        self.assertIn("missing-ssh", report["error"])
        self.assertIn("missing-ssh", Path(report["stderr_path"]).read_text())

    def test_missing_configuration_and_unknown_node(self):
        with patch.dict(os.environ, {}, clear=True):
            report = self.dispatch(nodes_file=None)
        self.assert_report(report, "blocked")
        self.assertIn("Nodes not configured", report["error"])
        self.assertIn("Nodes not configured", Path(report["stderr_path"]).read_text())
        report = ssh_dispatch("unknown-node", {"argv": ["worker"]}, workspace=self.work,
                              nodes_file=self.config, executable=str(self.fake))
        self.assert_report(report, "blocked")
        self.assertIn("not configured: unknown-node", report["error"])
        self.assertIn("unknown-node", Path(report["stderr_path"]).read_text())
        self.assertFalse((self.work / "ssh-argv.json").exists())

    def test_missing_malformed_and_outside_configurations(self):
        for value in ("{bad json", "[]", '{"render-node": null}'):
            with self.subTest(value=value):
                self.config.write_text(value)
                self.assert_report(self.dispatch(), "blocked")
        self.assert_report(self.dispatch(nodes_file=Path("missing.json")), "blocked")
        outside = self.root / "outside.json"
        outside.write_text(json.dumps(self.settings))
        self.assert_report(self.dispatch(nodes_file=outside), "blocked")
        self.assertFalse((self.work / "ssh-argv.json").exists())

    def test_invalid_targets_are_rejected_before_launch(self):
        for target in ("-oProxyCommand=BAD", "host;touch BAD", "user@HOST_ALIAS\nBAD", "host name", ""):
            with self.subTest(target=target):
                self.settings["render-node"]["target"] = target
                self.config.write_text(json.dumps(self.settings))
                self.assert_report(self.dispatch(), "blocked")
        self.assertFalse((self.work / "ssh-argv.json").exists())

    def test_invalid_command_and_timeout_options(self):
        for argv in ("worker --render", [], [""], ["-option"], ["worker", 3], ["worker", "bad\x00arg"]):
            with self.subTest(argv=argv):
                report = ssh_dispatch("render-node", {"argv": argv}, workspace=self.work,
                                      nodes_file=self.config, executable=str(self.fake))
                self.assert_report(report, "blocked")
        for options in ({"connect_timeout": 0}, {"connect_timeout": 1.5}, {"connect_timeout": True},
                        {"timeout": 0}, {"timeout": float("inf")}, {"timeout": True}):
            with self.subTest(options=options):
                self.assert_report(self.dispatch(**options), "blocked")
        self.assertFalse((self.work / "ssh-argv.json").exists())

    def test_remote_posix_shell_receives_literal_arguments_and_cwd(self):
        # Fake SSH evaluates only the remote operand locally. No SSH client or
        # network connection runs, and a real shell checks the quoting semantics.
        self.program("import subprocess\nsys.exit(subprocess.call(['/bin/sh', '-c', sys.argv[-1]]))\n")
        directory = self.work / "cwd ' ; $(touch CWD_INJECTED)"
        directory.mkdir()
        values = ["", "a b", "a'b\"c", "; touch ARG_INJECTED", "$(touch SUB_INJECTED)",
                  "`touch BACKTICK_INJECTED`", "line\nbreak", "*", "--option"]
        task = {"argv": [sys.executable, "-B", "-c", "import json,os,sys; print(json.dumps([os.getcwd(), sys.argv[1:]]))", *values],
                "cwd": str(directory)}
        report = ssh_dispatch("render-node", task, workspace=self.work, nodes_file=self.config,
                              executable=str(self.fake))
        self.assert_report(report, "passed")
        self.assertEqual(json.loads(Path(report["stdout_path"]).read_text()), [str(directory.resolve()), values])
        for parent in (self.work, directory):
            for marker in ("CWD_INJECTED", "ARG_INJECTED", "SUB_INJECTED", "BACKTICK_INJECTED"):
                self.assertFalse((parent / marker).exists())

    def test_unsafe_report_path_is_blocked(self):
        report = self.dispatch(result_path=self.root / "outside-report.json")
        self.assert_report(report, "blocked")
        self.assertFalse((self.root / "outside-report.json").exists())
        self.assertFalse((self.work / "ssh-argv.json").exists())

    def test_report_and_log_paths_cannot_overwrite_configuration(self):
        original = self.config.read_bytes()
        for target in (self.config, self.work / "nodes.stdout.json"):
            with self.subTest(target=target):
                # The second case aliases the stderr log of a chosen report.
                if target != self.config:
                    log_config = target.with_suffix(".stderr.log")
                    log_config.write_bytes(original)
                else:
                    log_config = self.config
                report = self.dispatch(nodes_file=log_config, result_path=target)
                self.assert_report(report, "blocked")
                self.assertEqual(log_config.read_bytes(), original)
        self.assertFalse((self.work / "ssh-argv.json").exists())


class RenderReviewTests(WorkspaceTest):
    def setUp(self):
        super().setUp()
        self.image = self.work / "render.png"
        self.reference = self.work / "reference.png"
        self.image.write_bytes(png())
        self.reference.write_bytes(png(1, 1))
        self.reference_digest = hashlib.sha256(self.reference.read_bytes()).hexdigest()
        self.gate = self.work / "numeric.json"
        self.raw_gate = b'{ "tool": "gate_aabb", "passed": false, "score": 0.125, "checks": [{"measured": 1.25e-4}] }\n'
        self.gate.write_bytes(self.raw_gate)

    def bundle(self, **options):
        defaults = {"artifacts": [Path("render.png")], "references": [Path("reference.png")],
                    "workspace": self.work, "task_id": "review-task", "gate_reports": [Path("numeric.json")],
                    "reference_sha256": {"reference.png": self.reference_digest}}
        defaults.update(options)
        return gate_render_review(**defaults)

    def assert_no_bundle(self):
        self.assertFalse((self.work / "review_bundle").exists())
        self.assertEqual(list(self.work.glob(".review_bundle-*")), [])

    def test_complete_bundle_manifest_hashes_sizes_dimensions_and_original_gates(self):
        report = self.bundle()
        self.assert_report(report, "passed")
        self.assertTrue(report["packaging_only"])
        self.assertEqual(report["review_status"], "pending")
        self.assertIsNone(report["quality_passed"])
        bundle = Path(report["bundle_path"])
        self.assertEqual(bundle, self.work.resolve() / "review_bundle")
        manifest_bytes = Path(report["manifest_path"]).read_bytes()
        self.assertEqual(hashlib.sha256(manifest_bytes).hexdigest(), report["manifest_sha256"])
        manifest = json.loads(manifest_bytes)
        self.assertEqual(manifest["task_id"], "review-task")
        files = {path.relative_to(bundle).as_posix() for path in bundle.rglob("*") if path.is_file()}
        self.assertEqual(files, {"manifest.json", *(entry["path"] for entry in manifest["files"])})
        self.assertEqual(report["file_count"], len(files))
        self.assertEqual(len(files), 6)
        readme = Path(report["index_path"]).read_text()
        for name in files:
            self.assertEqual(sum(line.startswith(name + " | ") for line in readme.splitlines()), 1)
        for entry in manifest["files"]:
            data = (bundle / entry["path"]).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), entry["sha256"])
            self.assertEqual(len(data), entry["size_bytes"])
            if entry["source_path"]:
                source = Path(entry["source_path"])
                self.assertEqual(data, source.read_bytes())
                self.assertEqual(entry["storage"], "copy")
                self.assertNotEqual(source.stat().st_ino, (bundle / entry["path"]).stat().st_ino)
            if entry["role"] in ("artifact", "reference"):
                self.assertEqual(entry["dimensions"], [2, 3] if entry["role"] == "artifact" else [1, 1])
            if entry["role"] == "reference":
                self.assertEqual(entry["locked_sha256"], entry["sha256"])
        self.assertEqual(json.loads(Path(report["gates_path"]).read_text()), [json.loads(self.raw_gate)])
        # A failed numeric gate is retained unchanged; packaging confers no grade.
        self.assertFalse(json.loads(Path(report["gates_path"]).read_text())[0]["passed"])
        copied = bundle / "artifacts/0001-render.png"
        self.image.write_bytes(b"changed after packaging")
        self.assertEqual(copied.read_bytes(), png())

    def test_multiple_reports_preserve_order_and_raw_bytes(self):
        other = self.work / "other.json"
        precise_number = "0.12345678901234567890123456789"
        other.write_text('{"score":' + precise_number + ',"passed":true,"nested":{"threshold":0.5}}\n')
        report = self.bundle(gate_reports=[self.gate, other])
        self.assert_report(report, "passed")
        self.assertEqual(json.loads(Path(report["gates_path"]).read_text()),
                         [json.loads(self.raw_gate), json.loads(other.read_text())])
        self.assertIn(precise_number, Path(report["gates_path"]).read_text())
        gates = Path(report["bundle_path"]) / "gates"
        self.assertEqual((gates / "0001-numeric.json").read_bytes(), self.raw_gate)
        self.assertEqual((gates / "0002-other.json").read_bytes(), other.read_bytes())

    def test_missing_and_empty_artifacts_report_every_missing_item(self):
        (self.work / "empty.png").touch()
        report = self.bundle(artifacts=[Path("missing.png"), Path("empty.png")])
        self.assert_report(report, "failed")
        self.assertEqual(len(report["missing"]), 2)
        self.assertEqual({item["reason"] for item in report["missing"]}, {"missing or not a file", "empty file"})
        self.assert_no_bundle()

    def test_empty_artifact_list_fails(self):
        report = self.bundle(artifacts=[])
        self.assert_report(report, "failed")
        self.assertIn("empty", report["error"])
        self.assert_no_bundle()

    def test_missing_or_empty_reference_and_gate_report_fail(self):
        for source in (self.reference, self.gate):
            original = source.read_bytes()
            for empty in (False, True):
                with self.subTest(source=source.name, empty=empty):
                    if empty:
                        source.write_bytes(b"")
                    else:
                        source.unlink()
                    report = self.bundle()
                    self.assert_report(report, "failed")
                    self.assertTrue(report["missing"])
                    self.assert_no_bundle()
                    source.write_bytes(original)

    def test_reference_hash_mismatch_fails_before_publishing(self):
        report = self.bundle(reference_sha256={"reference.png": "0" * 64})
        self.assert_report(report, "failed")
        self.assertIn("sha256 mismatch", report["error"])
        self.assertEqual(report["actual_sha256"], hashlib.sha256(self.reference.read_bytes()).hexdigest())
        self.assert_no_bundle()

    def test_absolute_uppercase_lock_is_checked(self):
        report = self.bundle(reference_sha256={str(self.reference.resolve()): hashlib.sha256(self.reference.read_bytes()).hexdigest().upper()})
        self.assert_report(report, "passed")

    def test_unknown_malformed_and_conflicting_locks_fail(self):
        digest = hashlib.sha256(self.reference.read_bytes()).hexdigest()
        for locks in ({"typo.png": digest}, {"reference.png": "bad"},
                      {"reference.png": digest, str(self.reference): "0" * 64}):
            with self.subTest(locks=locks):
                self.assert_report(self.bundle(reference_sha256=locks), "failed")
                self.assert_no_bundle()

    def test_reference_and_artifact_symlink_escape_is_blocked(self):
        outside = self.root / "outside.png"
        outside.write_bytes(png())
        for source in (self.reference, self.image):
            original = source.read_bytes()
            source.unlink()
            source.symlink_to(outside)
            with self.subTest(source=source.name):
                self.assert_report(self.bundle(reference_sha256=None), "blocked")
                self.assert_no_bundle()
            source.unlink()
            source.write_bytes(original)

    def test_sensitive_reference_is_blocked(self):
        sensitive = self.work / ".ssh"
        sensitive.mkdir()
        (sensitive / "reference.png").write_bytes(png())
        report = self.bundle(references=[Path(".ssh/reference.png")], reference_sha256=None)
        self.assert_report(report, "blocked")
        self.assert_no_bundle()

    def test_declared_copy_directory_is_allowed_for_inputs(self):
        copy = self.root / "declared-copy"
        copy.mkdir()
        reference = copy / "reference.png"
        reference.write_bytes(png())
        report = self.bundle(references=[reference], allowed_directories=[copy],
                             reference_sha256={str(reference): hashlib.sha256(reference.read_bytes()).hexdigest()})
        self.assert_report(report, "passed")
        self.assertTrue(self.work.resolve() in Path(report["bundle_path"]).parents)

    def test_output_bundle_must_not_follow_internal_symlink(self):
        redirected = self.work / "redirected"
        redirected.mkdir()
        (self.work / "review_bundle").symlink_to(redirected, target_is_directory=True)
        self.assert_report(self.bundle(), "blocked")
        self.assertEqual(list(redirected.iterdir()), [])

    def test_existing_bundle_is_preserved_and_not_reported_as_new(self):
        first = self.bundle()
        self.assert_report(first, "passed")
        manifest_path = Path(first["manifest_path"])
        before = manifest_path.read_bytes()
        self.reference.write_bytes(png(2, 2))
        second = self.bundle()
        self.assert_report(second, "failed")
        self.assertIn("existing bundle preserved", second["error"])
        self.assertNotIn("bundle_path", second)
        self.assertEqual(manifest_path.read_bytes(), before)
        missing = self.bundle(artifacts=[Path("missing.png")])
        self.assert_report(missing, "failed")
        self.assertEqual(missing["missing"][0]["role"], "artifact")
        self.assertEqual(manifest_path.read_bytes(), before)

    def test_bad_report_json_does_not_publish_bundle(self):
        for content in ("{bad json", "[]", '{"score": NaN}'):
            with self.subTest(content=content):
                self.gate.write_text(content)
                self.assert_report(self.bundle(), "failed")
                self.assert_no_bundle()

    def test_staging_is_removed_after_copy_failure(self):
        with patch("hand.tools._review_entry", side_effect=OSError("simulated disk error")):
            report = self.bundle()
        self.assert_report(report, "failed")
        self.assertIn("simulated disk error", report["error"])
        self.assert_no_bundle()

    def test_duplicate_basenames_do_not_collide(self):
        other = self.work / "other"
        other.mkdir()
        (other / "render.png").write_bytes(png(1, 1))
        report = self.bundle(artifacts=[self.image, other / "render.png"])
        self.assert_report(report, "passed")
        bundle = Path(report["bundle_path"])
        self.assertEqual((bundle / "artifacts/0001-render.png").read_bytes(), png())
        self.assertEqual((bundle / "artifacts/0002-render.png").read_bytes(), png(1, 1))

    def test_no_locks_or_reports_does_not_invent_evidence(self):
        report = self.bundle(reference_sha256=None, gate_reports=[])
        self.assert_report(report, "passed")
        manifest = json.loads(Path(report["manifest_path"]).read_text())
        self.assertEqual(json.loads(Path(report["gates_path"]).read_text()), [])
        reference = next(entry for entry in manifest["files"] if entry["role"] == "reference")
        self.assertIsNone(reference["locked_sha256"])

    def test_unsafe_report_path_is_blocked(self):
        report = self.bundle(result_path=self.root / "outside-report.json")
        self.assert_report(report, "blocked")
        self.assert_no_bundle()

    def test_report_cannot_overwrite_inputs_or_live_inside_bundle(self):
        original = self.image.read_bytes()
        for target in (self.image, self.work / "review_bundle/report.json"):
            with self.subTest(target=target):
                report = self.bundle(result_path=target)
                self.assert_report(report, "blocked")
                self.assertEqual(self.image.read_bytes(), original)
                self.assert_no_bundle()


if __name__ == "__main__":
    unittest.main(verbosity=2)
