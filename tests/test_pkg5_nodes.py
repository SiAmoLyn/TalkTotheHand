"""Pydantic node recipes and CLI overview; all probes use a fake SSH binary."""
from __future__ import annotations

from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from hand.tools import ssh_dispatch


ROOT = Path(__file__).resolve().parents[1]
HAS_PYDANTIC = importlib.util.find_spec("pydantic") is not None
HAS_RUNTIME = HAS_PYDANTIC and all(importlib.util.find_spec(n) is not None for n in ("langgraph", "yaml"))


class Workspace(unittest.TestCase):
    def setUp(self):
        directory = ROOT / "work/pkg5-node-tests"
        directory.mkdir(parents=True, exist_ok=True)
        self.temp = TemporaryDirectory(dir=directory)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "nodes.json"
        self.settings = {"node-a": {"target": "user@HOST_ALIAS", "cwd": "REMOTE_WORKSPACE"}}
        self.write_config()
        self.fake = self.root / "fake-ssh"
        self.fake.write_text(f"#!{sys.executable}\nimport json,sys\nfrom pathlib import Path\n"
                             "Path('probe-argv.json').write_text(json.dumps(sys.argv[1:]))\n"
                             "print('synthetic probe')\n"
                             "sys.exit(255 if 'user@FAIL_ALIAS' in sys.argv else 0)\n")
        self.fake.chmod(0o700)

    def write_config(self):
        self.config.write_text(json.dumps(self.settings))

    def dispatch(self, task=None, **options):
        return ssh_dispatch("node-a", ["worker", "--input", "file with spaces"] if task is None else task,
                            workspace=self.root, nodes_file=self.config, executable=str(self.fake), **options)


@unittest.skipUnless(HAS_PYDANTIC, "Project Pydantic dependency unavailable; no substitute validator")
class NodesTests(Workspace):
    def load(self, **options):
        from hand.nodes import load_nodes
        return load_nodes(self.config, workspace=self.root, **options)

    def test_valid_pydantic_models_and_defaults(self):
        config, path = self.load()
        self.assertEqual(config.nodes["node-a"].target, "user@HOST_ALIAS")
        self.assertEqual(config.nodes["node-a"].ssh_args, [])
        self.assertIsNone(config.nodes["node-a"].via)
        self.assertEqual(path, self.config)

    def test_command_array_dispatch_has_required_transport_policy(self):
        report = self.dispatch(connect_timeout=7)
        self.assertTrue(report["passed"], report)
        self.assertEqual(report["command"][:7], [str(self.fake), "-o", "BatchMode=yes", "-o", "ConnectTimeout=7", "--", "user@HOST_ALIAS"])
        self.assertEqual(report["command"][-1], "cd REMOTE_WORKSPACE && exec worker --input 'file with spaces'")
        self.assertEqual(json.loads(Path(report["report_path"]).read_text()), report)

    def test_pkg4_object_task_and_explicit_cwd_still_work(self):
        report = self.dispatch({"argv": ["worker"], "cwd": "OVERRIDE_WORKSPACE"})
        self.assertTrue(report["passed"], report)
        self.assertEqual(report["command"][-1], "cd OVERRIDE_WORKSPACE && exec worker")

    def test_environment_config_and_explicit_parameter_precedence(self):
        from hand.nodes import load_nodes
        with patch.dict(os.environ, {"HAND_NODES_FILE": "nodes.json"}):
            config, source = load_nodes(workspace=self.root)
            self.assertIn("node-a", config.nodes)
            self.assertEqual(source, self.config)
        with patch.dict(os.environ, {"HAND_NODES_FILE": "missing.json"}):
            self.assertTrue(self.dispatch()["passed"])

    def test_jump_chain_and_extra_args_are_argv_operands(self):
        self.settings["node-a"].update(via="node-b", ssh_args=["-p", "2222", "-o", "ServerAliveInterval=30"])
        self.settings["node-b"] = {"target": "user@JUMP_ALIAS", "cwd": "REMOTE_WORKSPACE", "via": "node-c",
                                   "ssh_args": ["-p", "2223"]}
        self.settings["node-c"] = {"target": "user@OUTER_ALIAS", "cwd": "REMOTE_WORKSPACE"}
        self.write_config()
        report = self.dispatch()
        self.assertTrue(report["passed"], report)
        command = report["command"]
        self.assertEqual(command[1:5], ["-p", "2222", "-o", "ServerAliveInterval=30"])
        self.assertEqual(command[command.index("-J") + 1], "user@OUTER_ALIAS,user@JUMP_ALIAS:2223")

    def test_missing_config_and_unknown_node_errors_are_structured(self):
        from hand.nodes import load_nodes, NodeConfigError
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(NodeConfigError) as caught:
                load_nodes(workspace=self.root)
            self.assertEqual(caught.exception.report["code"], "nodes_unconfigured")
        report = ssh_dispatch("missing-node", ["true"], workspace=self.root, nodes_file=self.config, executable=str(self.fake))
        self.assertEqual(report["config_error"]["code"], "node_unconfigured")
        self.assertFalse((self.root / "probe-argv.json").exists())

    def test_invalid_fields_missing_fields_and_strict_types_are_rejected(self):
        from hand.nodes import NodeConfigError
        for value in ({"cwd": "REMOTE_WORKSPACE"}, {"target": "user@HOST_ALIAS"},
                      {"target": "user@HOST_ALIAS", "cwd": "REMOTE_WORKSPACE", "unknown": True},
                      {"target": "user@HOST_ALIAS", "cwd": 1}, {"target": "user@HOST_ALIAS", "cwd": "REMOTE_WORKSPACE", "ssh_args": "-p 22"}):
            self.settings["node-a"] = value
            self.write_config()
            with self.assertRaises(NodeConfigError) as caught:
                self.load()
            self.assertEqual(caught.exception.report["code"], "nodes_invalid")
            self.assertTrue(caught.exception.report["details"])

    def test_missing_and_cyclic_jump_references_are_rejected(self):
        from hand.nodes import NodeConfigError
        for via in ("missing-node", "node-a"):
            self.settings["node-a"]["via"] = via
            self.write_config()
            with self.assertRaises(NodeConfigError):
                self.load()
        self.settings["node-a"]["via"] = "node-b"
        self.settings["node-b"] = {"target": "user@JUMP_ALIAS", "cwd": "REMOTE_WORKSPACE", "via": "node-a"}
        self.write_config()
        self.assertFalse(self.dispatch()["passed"])

    def test_jump_options_cannot_be_silently_discarded(self):
        self.settings["node-a"]["via"] = "node-b"
        self.settings["node-b"] = {"target": "user@JUMP_ALIAS", "cwd": "REMOTE_WORKSPACE", "ssh_args": ["-i", "IDENTITY_PLACEHOLDER"]}
        self.write_config()
        report = self.dispatch()
        self.assertFalse(report["passed"])
        self.assertIn("Jump-node ssh_args", str(report))

    def test_extra_options_cannot_override_policy_inject_commands_or_forward(self):
        for args in (["-o", "BatchMode=no"], ["-o", "ConnectTimeout=999"], ["-o", "ProxyCommand=BAD"],
                     ["-J", "OTHER_ALIAS"], ["-L", "BAD"], ["--", "OTHER_ALIAS"], ["-p"],
                     ["-p", "0"], ["-p", "65536"], ["-i", "bad\nvalue"], ["-o", "stricthostkeychecking=no"]):
            self.settings["node-a"]["ssh_args"] = args
            self.write_config()
            self.assertFalse(self.dispatch()["passed"], args)
        self.assertFalse((self.root / "probe-argv.json").exists())

    def test_target_injection_and_invalid_node_names_are_rejected(self):
        for target in ("-option", "alias name", "HOST_ALIAS;BAD", "HOST_ALIAS\nBAD", ""):
            self.settings["node-a"]["target"] = target
            self.write_config()
            self.assertFalse(self.dispatch()["passed"])
        self.settings = {"../node": {"target": "user@HOST_ALIAS", "cwd": "REMOTE_WORKSPACE"}}
        self.write_config()
        self.assertFalse(self.dispatch()["passed"])

    def test_remote_quoting_preserves_shell_metacharacters(self):
        argv = ["worker", "", "quotes'\"", "$(touch INJECTED)", "a; b", "line\nbreak"]
        self.settings["node-a"]["cwd"] = "REMOTE_WORKSPACE with ' quotes"
        self.write_config()
        report = self.dispatch(argv)
        self.assertTrue(report["passed"], report)
        self.assertEqual(shlex.split(report["command"][-1]), ["cd", self.settings["node-a"]["cwd"], "&&", "exec", *argv])
        self.assertFalse((self.root / "INJECTED").exists())

    def test_malformed_config_and_symlink_escape_are_structured(self):
        for raw in ("{bad", "[]", '{"node-a":null}'):
            self.config.write_text(raw)
            self.assertEqual(self.dispatch()["config_error"]["code"], "nodes_invalid")
        self.config.unlink()
        self.config.symlink_to(self.root.parent / "not-readable.json")
        self.assertEqual(self.dispatch()["config_error"]["code"], "nodes_invalid")

    def test_invalid_argv_never_launches(self):
        for task in ([], [""], ["-option"], ["worker", 3], ["worker", "bad\x00arg"], {"argv": ["worker"], "typo": 1}):
            self.assertFalse(self.dispatch(task)["passed"])
        self.assertFalse((self.root / "probe-argv.json").exists())

    def test_reports_and_logs_cannot_overwrite_configuration(self):
        before = self.config.read_bytes()
        report = self.dispatch(result_path=self.config)
        self.assertFalse(report["passed"])
        self.assertEqual(self.config.read_bytes(), before)


@unittest.skipUnless(HAS_RUNTIME, "Python 3.12 project runtime unavailable; CLI needs existing dependencies")
class StatusAllTests(Workspace):
    def overview(self, **options):
        from hand.cli import status_all
        return status_all(self.root, nodes_file=self.config, ssh_executable=str(self.fake), **options)

    def test_ok_and_failed_nodes_do_not_hide_checkpoint_and_monitor(self):
        self.settings["node-b"] = {"target": "user@FAIL_ALIAS", "cwd": "REMOTE_WORKSPACE"}
        self.write_config()
        state = self.root / "state.json"
        state.write_text(json.dumps({"phase": "idle", "last_action_at": None}))
        result = self.overview(monitor_state=state)
        self.assertEqual(result["nodes"]["items"]["node-a"]["status"], "ok")
        self.assertEqual(result["nodes"]["items"]["node-b"]["status"], "fail")
        self.assertEqual(result["checkpoints"]["status"], "unconfigured")
        self.assertEqual(result["monitor"]["status"], "ok")
        self.assertEqual(result["status"], "partial")

    def test_bad_node_config_corrupt_database_and_bad_monitor_are_separate_failures(self):
        self.config.write_text("{bad")
        (self.root / "work").mkdir()
        (self.root / "work/checkpoints.sqlite").write_text("not a database")
        state = self.root / "state.json"
        state.write_text("{bad")
        result = self.overview(monitor_state=state)
        for key in ("nodes", "checkpoints", "monitor"):
            self.assertEqual(result[key]["status"], "fail")

    def test_missing_configs_report_unconfigured_without_network(self):
        from hand.cli import status_all
        with patch.dict(os.environ, {}, clear=True):
            result = status_all(self.root)
        for key in ("nodes", "checkpoints", "monitor"):
            self.assertEqual(result[key]["status"], "unconfigured")
        self.assertFalse((self.root / "probe-argv.json").exists())

    def test_latest_checkpoint_count_is_per_task_not_per_history_row(self):
        from hand.cli import demo_task
        from hand.contracts import TaskPackage
        from hand.graph import HandRunner
        with HandRunner(self.root) as runner:
            done = demo_task("done-task")
            done["approval_points"] = []
            self.assertEqual(runner.run(TaskPackage.model_validate(done))["status"], "RETURNED")
            runner.run(TaskPackage.model_validate(demo_task("waiting-task")))
        result = self.overview()
        self.assertEqual(result["checkpoints"]["counts"], {"RETURNED": 1, "WAITING_APPROVAL": 1})
        self.assertEqual(result["checkpoints"]["total"], 2)

    def test_probe_exception_is_contained_and_other_nodes_still_run(self):
        self.settings["node-b"] = {"target": "user@SECOND_HOST_ALIAS", "cwd": "REMOTE_WORKSPACE"}
        self.write_config()
        def dispatch(name, task, **options):
            self.assertEqual(task, ["true"])
            if name == "node-a":
                raise OSError("fake transport error")
            return {"passed": True}
        with patch("hand.tools.ssh_dispatch", side_effect=dispatch):
            result = self.overview()
        self.assertEqual(result["nodes"]["items"]["node-a"]["status"], "fail")
        self.assertEqual(result["nodes"]["items"]["node-b"]["status"], "ok")

    def test_monitor_cli_once_and_dry_run_return_machine_json(self):
        from hand import cli
        (self.root / "logs").mkdir()
        config = self.root / "monitor.json"
        config.write_text(json.dumps({"target": "HOST_ALIAS", "log_dir": "logs", "state_file": "state.json"}))
        with patch.object(cli, "PROJECT_ROOT", self.root), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(["monitor", "--once", "--dry-run", "--config", str(config)]), 0)
        self.assertTrue(json.loads(output.getvalue())["dry_run"])
        self.assertFalse((self.root / "state.json").exists())
        with patch.object(cli, "PROJECT_ROOT", self.root), patch.dict(os.environ, {}, clear=True), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(["monitor", "--once"]), 1)
        self.assertEqual(json.loads(output.getvalue())["status"], "blocked")

    def test_status_all_cli_prints_overview_even_if_unconfigured(self):
        from hand import cli
        with patch.object(cli, "PROJECT_ROOT", self.root), patch.dict(os.environ, {}, clear=True), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(["status-all"]), 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "partial")


if __name__ == "__main__":
    unittest.main(verbosity=2)
