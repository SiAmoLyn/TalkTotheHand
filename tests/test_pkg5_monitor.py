"""Full watchdog ticks: synthetic logs, a fake clock and an injected executor."""
from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from hand.monitor import monitor_once, read_state


ROOT = Path(__file__).resolve().parents[1]


class MonitorTests(unittest.TestCase):
    def setUp(self):
        directory = ROOT / "work/pkg5-monitor-tests"
        directory.mkdir(parents=True, exist_ok=True)
        self.temp = TemporaryDirectory(dir=directory)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.logs = self.root / "logs"
        self.logs.mkdir()
        self.config = self.root / "monitor.json"
        self.state = self.root / "state.json"
        self.now = 1800000000.0
        self.calls = []
        self.settings = {"target": "HOST_ALIAS", "log_dir": "logs", "state_file": "state.json",
                         "actions": {name: {"enabled": True, "argv": ["FAKE_ACTION", name]}
                                     for name in ("action1", "action2", "action3")}}
        self.write_config()

    def write_config(self):
        self.config.write_text(json.dumps(self.settings))

    def fake(self, argv):
        self.calls.append((self.now, argv))
        return {"exit_code": 0, "stdout": "synthetic receipt"}

    def log(self, *, zeros=4, successes=0):
        rows = [{"timestamp": self.now - i, "target": "HOST_ALIAS", "recv": 0} for i in range(zeros)]
        rows += [{"timestamp": self.now, "target": "HOST_ALIAS", "recv": 12} for _ in range(successes)]
        (self.logs / "network.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))

    def day8(self, *, host="edge.hatch.example.com", recv="0", sent="215", dur="15.51s",
             stamp="2026-10-24 00:00:05.547", suffix=""):
        received = "" if recv is None else f" recv={recv}"
        return (f"{stamp}  INFO conn{{in=tun src=10.0.0.2:48392 dst={host}:443 out=Proxy "
                f"sent={sent}{received} dur={dur}}}: Disconnected {suffix}\n")

    def configure_day8(self, *, zone="UTC", now="2026-10-24 00:05:00"):
        self.settings.update(target="hatch.example.com", log_timezone=zone)
        self.now = datetime.fromisoformat(now).replace(tzinfo=ZoneInfo(zone)).timestamp()
        self.write_config()

    def test_day8_blackhole_window_detects_four_target_zero_records(self):
        self.configure_day8()
        (self.logs / "day8.log").write_text(self.day8() * 4)
        observation = self.phase("detected")["observation"]
        self.assertTrue(observation["blackhole"])
        self.assertEqual(observation["recv_zero"], 4)
        self.assertEqual(observation["success"], 0)
        self.assertEqual(observation["bad_lines"], 0)
        self.assertEqual(observation["diagnostics"]["sent_bytes"], 860)
        self.assertAlmostEqual(observation["diagnostics"]["duration_seconds"], 62.04)
        self.assertEqual(self.calls, [])

    def test_day8_tcp_shape_with_resolved_ip_detects_blackhole(self):
        # Real tun-mode record shape (Sisi acceptance, 2026-10-24): tcp{...}
        # with dst=host@resolved-ip:port — the shape that actually carries
        # recv=0 during a blackhole.
        self.configure_day8()
        line = ("2026-10-24 00:00:05.547  INFO tcp{in=tun src=198.18.0.2:49611 "
                "dst=edge.hatch.example.com@185.45.5.35:443 out=Proxy sent=215 "
                "recv=0 dur=15.60s}: Disconnected\n")
        (self.logs / "day8.log").write_text(line * 4)
        observation = self.phase("detected")["observation"]
        self.assertTrue(observation["blackhole"])
        self.assertEqual(observation["recv_zero"], 4)
        self.assertEqual(observation["success"], 0)
        self.assertEqual(observation["bad_lines"], 0)

    def test_day8_one_success_suppresses_blackhole(self):
        self.configure_day8()
        rows = self.day8() * 4 + self.day8(recv="5560", sent="2266", dur="601.17s")
        (self.logs / "day8.log").write_text(rows)
        observation = self.phase("idle")["observation"]
        self.assertFalse(observation["blackhole"])
        self.assertEqual(observation["success"], 1)
        self.assertEqual(observation["recv_zero"], 4)
        self.assertEqual(self.calls, [])

    def test_day8_other_destinations_ignore_even_missing_or_invalid_recv(self):
        self.configure_day8()
        rows = self.day8() * 4
        rows += self.day8(host="other.example.com", recv="5560", sent="9999")
        rows += self.day8(host="other.example.com", recv=None)
        rows += self.day8(host="other.example.com", recv="invalid")
        (self.logs / "day8.log").write_text(rows)
        observation = self.phase("detected")["observation"]
        self.assertEqual(observation["success"], 0)
        self.assertEqual(observation["recv_zero"], 4)
        self.assertEqual(observation["ignored_lines"], 3)
        self.assertEqual(observation["bad_lines"], 0)
        self.assertEqual(observation["diagnostics"]["sent_bytes"], 860)

    def test_day8_target_matches_only_dst_host_not_port_or_other_fields(self):
        self.configure_day8()
        (self.logs / "day8.log").write_text(self.day8(host="other.example.com",
                                                   suffix="target=hatch.example.com recv=1") * 4)
        observation = self.phase("idle")["observation"]
        self.assertEqual(observation["ignored_lines"], 4)
        self.assertEqual(observation["success"], 0)
        self.assertEqual(observation["recv_zero"], 0)
        self.settings["target"] = "443"
        self.write_config()
        self.state.unlink()
        observation = self.phase("idle")["observation"]
        self.assertEqual(observation["ignored_lines"], 4)

    def test_day8_missing_recv_bad_lines_and_optional_diagnostics_tolerance(self):
        self.configure_day8()
        rows = self.day8(sent="invalid", dur="NaNs", suffix="status=ok success=true") * 4
        rows += self.day8(recv=None) + self.day8(recv="bad") + self.day8(recv="-1")
        rows += self.day8().replace("dst=edge.hatch.example.com:443 ", "")
        rows += "broken line\n" + self.day8(stamp="2026-99-24 00:00:05.547")
        (self.logs / "day8.log").write_text(rows)
        observation = self.phase("detected")["observation"]
        self.assertEqual(observation["bad_lines"], 6)
        self.assertEqual(observation["recv_zero"], 4)
        self.assertEqual(observation["success"], 0)
        self.assertEqual(observation["diagnostics"], {"sent_bytes": 0, "duration_seconds": 0.0})

    def test_day8_milliseconds_and_configured_timezone_are_preserved(self):
        self.configure_day8(zone="Asia/Shanghai", now="2026-10-24 00:00:09.918")
        (self.logs / "day8.log").write_text(self.day8(stamp="2026-10-24 00:00:09.918", recv="1"))
        observation = self.phase("idle")["observation"]
        self.assertEqual(observation["success"], 1)
        self.assertEqual(observation["latest_success_at"], self.now)
        self.assertAlmostEqual(observation["latest_success_at"] % 1, 0.918, places=6)

    def test_day8_millisecond_window_boundaries_and_no_fraction_timestamp(self):
        self.configure_day8(now="2026-10-24 00:10:09.918")
        rows = self.day8(stamp="2026-10-24 00:00:09.918") * 3
        rows += self.day8(stamp="2026-10-24 00:05:00")
        rows += self.day8(stamp="2026-10-24 00:00:09.917", recv="1")
        rows += self.day8(stamp="2026-10-24 00:10:09.919", recv="1")
        (self.logs / "day8.log").write_text(rows)
        observation = self.phase("detected")["observation"]
        self.assertEqual(observation["recv_zero"], 4)
        self.assertEqual(observation["ignored_lines"], 2)
        self.assertEqual(observation["bad_lines"], 0)

    def test_day8_diagnostics_do_not_change_recv_decision(self):
        self.configure_day8()
        rows = self.day8(sent="999999999", dur="999999s") * 4
        (self.logs / "day8.log").write_text(rows)
        self.assertTrue(self.phase("detected")["observation"]["blackhole"])

    def test_legacy_text_and_json_success_markers_remain_compatible(self):
        stamp = datetime.fromtimestamp(self.now, timezone.utc).isoformat()
        rows = [f"[{stamp}] target=HOST_ALIAS recv=0 status=ok\n",
                f"{stamp} target=HOST_ALIAS recv=0 status=success\n",
                f"{stamp} target=HOST_ALIAS recv=0 success=true\n",
                json.dumps({"ts": self.now, "target": "HOST_ALIAS", "recv": 0, "success": True}) + "\n"]
        (self.logs / "legacy.log").write_text("".join(rows))
        observation = self.phase("idle")["observation"]
        self.assertEqual(observation["success"], 4)
        self.assertEqual(observation["bad_lines"], 0)
        self.assertEqual(observation["diagnostics"], {"sent_bytes": 0, "duration_seconds": 0.0})

    def tick(self, **options):
        return monitor_once(self.config, root=self.root, clock=lambda: self.now, executor=self.fake, **options)

    def phase(self, expected):
        before = len(self.calls)
        result = self.tick()
        self.assertEqual(result["state"]["phase"], expected, result)
        self.assertLessEqual(len(self.calls) - before, 1)
        self.assertEqual(read_state(self.state), result["state"])
        return result

    def start_action1(self):
        self.log()
        self.phase("detected")
        return self.phase("action1")

    def test_entire_lifecycle_wait_recheck_repeated_action2_recovery_and_once_only_action3(self):
        phases = [self.phase("idle")["state"]["phase"]]
        self.log()
        for expected in ("detected", "action1", "wait"):
            phases.append(self.phase(expected)["state"]["phase"])
        self.now += 299
        self.log()
        self.assertEqual(self.phase("wait")["reason"], "recheck_wait")
        self.now += 1
        self.phase("recheck")
        self.assertEqual(self.phase("recheck")["reason"], "minimum_action_interval")
        self.now += 300
        self.log()
        self.assertEqual(self.phase("recheck")["reason"], "action2_interval")
        self.now += 1200
        self.log()
        self.phase("action2")
        self.phase("wait")
        self.now += 300
        self.log()
        self.phase("recheck")
        self.now += 300
        self.log()
        self.assertEqual(self.phase("recheck")["reason"], "action2_interval")
        self.now += 1200
        self.log()
        self.phase("action2")
        self.phase("wait")
        self.now += 300
        self.log(successes=1)
        self.phase("recheck")
        self.phase("recovered")
        self.assertEqual(self.phase("recovered")["reason"], "minimum_action_interval")
        self.now += 300
        self.log(successes=1)
        self.phase("action3")
        self.phase("recovered")
        self.phase("idle")
        self.phase("idle")
        self.assertEqual([c[1][1] for c in self.calls], ["action1", "action2", "action2", "action3"])
        self.assertTrue(all(b[0] - a[0] >= 600 for a, b in zip(self.calls, self.calls[1:])))
        self.assertGreaterEqual(self.calls[2][0] - self.calls[1][0], 1800)
        events = [json.loads(line) for line in Path(str(self.state) + ".events.jsonl").read_text().splitlines()]
        self.assertEqual(sum(e["action"] is not None for e in events), 4)

    def test_minimum_guard_survives_reload_and_clock_rollback(self):
        self.start_action1()
        old = read_state(self.state)
        old.update(phase="detected", detected_at=self.now, closure_done=False)
        self.state.write_text(json.dumps(old))
        for step in (599, -800):
            self.now += step
            self.log()
            self.assertEqual(self.tick()["reason"], "minimum_action_interval")
        self.assertEqual(len(self.calls), 1)

    def test_recovery_requires_five_minute_wait_even_when_success_arrives_early(self):
        self.start_action1()
        self.phase("wait")
        self.now += 60
        self.log(successes=1)
        self.phase("wait")
        self.now += 240
        self.phase("recheck")
        self.phase("recovered")

    def test_bad_lines_mixed_formats_window_and_target_filtering(self):
        stamp = datetime.fromtimestamp(self.now, timezone.utc).isoformat()
        rows = [f"[{stamp}] target=HOST_ALIAS recv=0\n"] * 4
        rows += ["bad\n", "{}\n", "[]\n", f"{stamp} target=HOST_ALIAS recv=bad\n", "\n"]
        rows += [json.dumps({"timestamp": self.now - 601, "target": "HOST_ALIAS", "recv": 12}) + "\n",
                 json.dumps({"timestamp": self.now + 1, "target": "HOST_ALIAS", "recv": 12}) + "\n",
                 json.dumps({"timestamp": self.now, "target": "OTHER_ALIAS", "recv": 12}) + "\n"]
        (self.logs / "day8.log").write_text("".join(rows))
        report = self.phase("detected")
        self.assertEqual(report["observation"]["bad_lines"], 5)
        self.assertEqual(report["observation"]["ignored_lines"], 3)
        self.assertEqual(report["observation"]["recv_zero"], 4)

    def test_success_suppresses_blackhole_and_never_calls_executor(self):
        self.log(successes=1)
        report = self.phase("idle")
        self.assertEqual(report["observation"]["success"], 1)
        self.assertEqual(self.calls, [])

    def test_four_zero_threshold_and_inclusive_ten_minute_window(self):
        self.log(zeros=3)
        self.phase("idle")
        with (self.logs / "network.jsonl").open("a") as stream:
            stream.write(json.dumps({"timestamp": self.now - 600, "target": "HOST_ALIAS", "recv": 0}) + "\n")
        self.assertEqual(self.phase("detected")["observation"]["recv_zero"], 4)

    def test_epoch_zero_action_time_is_not_mistaken_for_missing(self):
        self.now = 0
        self.log()
        # Records with negative timestamps are invalid; provide four at zero.
        (self.logs / "network.jsonl").write_text((json.dumps({"timestamp": 0, "target": "HOST_ALIAS", "recv": 0}) + "\n") * 4)
        self.phase("detected")
        self.phase("action1")
        self.phase("wait")
        self.now = 1800
        self.log()
        self.phase("recheck")
        self.phase("action2")
        self.assertEqual(len(self.calls), 2)

    def test_success_true_with_recv_zero_counts_as_success(self):
        self.log()
        with (self.logs / "network.jsonl").open("a") as stream:
            stream.write(json.dumps({"timestamp": self.now, "target": "HOST_ALIAS", "recv": 0, "success": True}) + "\n")
        self.assertEqual(self.phase("idle")["observation"]["success"], 1)

    def test_dry_run_new_detection_writes_nothing(self):
        self.log()
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        report = self.tick(dry_run=True)
        self.assertEqual(report["proposed_state"]["phase"], "detected")
        self.assertEqual(report["state"]["phase"], "idle")
        after = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(self.calls, [])

    def test_dry_run_pending_action_does_not_reserve_or_execute(self):
        self.log()
        self.phase("detected")
        before = self.state.read_bytes()
        events = Path(str(self.state) + ".events.jsonl").read_bytes()
        report = self.tick(dry_run=True)
        self.assertEqual(report["planned_action"], "action1")
        self.assertEqual(self.state.read_bytes(), before)
        self.assertEqual(Path(str(self.state) + ".events.jsonl").read_bytes(), events)
        self.assertEqual(self.calls, [])

    def test_no_recent_logs_never_means_recovered_or_action2(self):
        self.start_action1()
        self.phase("wait")
        self.now += 1800
        self.phase("recheck")
        self.assertEqual(self.phase("recheck")["reason"], "no_fresh_evidence")
        self.assertEqual(len(self.calls), 1)

    def test_action_failure_consumes_cooldown_and_keeps_receipt(self):
        self.log()
        self.phase("detected")
        def failed(argv):
            self.assertEqual(read_state(self.state)["last_action_result"]["status"], "reserved")
            self.assertEqual(read_state(self.state)["last_action_at"], self.now)
            return {"exit_code": 8, "stderr": "fake failure"}
        report = monitor_once(self.config, root=self.root, clock=lambda: self.now, executor=failed)
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(read_state(self.state)["last_action_result"]["receipt"]["exit_code"], 8)
        self.assertEqual(read_state(self.state)["last_action_at"], self.now)

    def test_executor_exception_is_structured_and_reservation_is_retained(self):
        self.log()
        self.phase("detected")
        def failed(argv):
            raise OSError("fake spawn error")
        report = monitor_once(self.config, root=self.root, clock=lambda: self.now, executor=failed)
        self.assertEqual(report["status"], "blocked")
        self.assertIn("fake spawn error", report["error"])
        self.assertEqual(read_state(self.state)["last_action_at"], self.now)

    def test_production_executor_only_launches_configured_fake_binary_with_literal_argv(self):
        fake = self.root / "fake-application"
        fake.write_text(f"#!{sys.executable}\nimport json,sys\nfrom pathlib import Path\n"
                        "Path('action-argv.json').write_text(json.dumps(sys.argv[1:]))\nprint('fake action')\n")
        fake.chmod(0o700)
        argument = "literal; $(touch INJECTED)"
        self.settings["actions"]["action1"]["argv"] = [str(fake), argument]
        self.write_config()
        self.log()
        self.phase("detected")
        report = monitor_once(self.config, root=self.root, clock=lambda: self.now)
        self.assertEqual(report["action"], "action1")
        self.assertTrue(report["passed"], report)
        self.assertEqual(json.loads((self.root / "action-argv.json").read_text()), [argument])
        receipt = report["state"]["last_action_result"]["receipt"]
        self.assertEqual(Path(receipt["stdout_path"]).read_text(), "fake action\n")
        self.assertFalse((self.root / "INJECTED").exists())

    def test_production_executor_fake_binary_timeout_retains_cooldown(self):
        fake = self.root / "fake-application"
        fake.write_text(f"#!{sys.executable}\nimport time\nprint('partial fake output',flush=True)\ntime.sleep(30)\n")
        fake.chmod(0o700)
        self.settings["actions"]["action1"]["argv"] = [str(fake)]
        self.settings["action_timeout"] = 0.5
        self.write_config()
        self.log()
        self.phase("detected")
        report = monitor_once(self.config, root=self.root, clock=lambda: self.now)
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["state"]["last_action_result"]["receipt"]["reason"], "timeout")
        self.assertEqual(read_state(self.state)["last_action_at"], self.now)

    def test_disabled_action_reports_blocked_without_calls(self):
        self.settings["actions"]["action1"]["enabled"] = False
        self.write_config()
        self.log()
        self.phase("detected")
        report = self.phase("detected")
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(self.calls, [])

    def test_action3_disabled_returns_idle_after_confirmed_recovery(self):
        self.settings["actions"]["action3"]["enabled"] = False
        self.write_config()
        self.start_action1()
        self.phase("wait")
        self.now += 300
        self.log(successes=1)
        self.phase("recheck")
        self.phase("recovered")
        self.phase("idle")
        self.assertEqual(len(self.calls), 1)

    def test_recovery_does_not_use_success_from_before_last_action(self):
        self.start_action1()
        self.phase("wait")
        self.now += 300
        self.phase("recheck")
        self.log(zeros=0)
        (self.logs / "old.log").write_text(json.dumps({"timestamp": self.now - 301, "target": "HOST_ALIAS", "recv": 1}) + "\n")
        self.phase("recheck")

    def test_bad_state_is_preserved_and_never_causes_action(self):
        for raw in ("{bad", '{"phase":"invalid"}', '{"phase":"detected","target":"HOST_ALIAS"}'):
            self.state.write_text(raw)
            report = self.tick()
            self.assertEqual(report["status"], "blocked")
            self.assertEqual(self.state.read_text(), raw)
        self.assertEqual(self.calls, [])

    def test_invalid_config_never_weakens_hard_intervals(self):
        for key, bad in (("wait_seconds", 299), ("min_action_interval_seconds", 599),
                         ("action2_interval_seconds", 1799), ("recv_zero_threshold", 3),
                         ("action_timeout", float("nan")), ("max_log_files", True)):
            with self.subTest(key=key):
                self.settings[key] = bad
                self.write_config()
                self.assertEqual(self.tick()["status"], "blocked")
                del self.settings[key]
        self.assertEqual(self.calls, [])

    def test_missing_logs_and_path_escape_are_blocked(self):
        self.settings["log_dir"] = "missing"
        self.write_config()
        self.assertEqual(self.tick()["status"], "blocked")
        self.settings["log_dir"] = "logs"
        self.settings["state_file"] = "../outside.json"
        self.write_config()
        self.assertEqual(self.tick()["status"], "blocked")
        self.assertEqual(self.calls, [])

    def test_state_cannot_overwrite_config_or_log_input(self):
        for path in ("monitor.json", "logs/state.json"):
            self.settings["state_file"] = path
            self.write_config()
            self.assertEqual(self.tick()["status"], "blocked")
        self.assertEqual(self.calls, [])

    def test_concurrent_tick_lock_blocks_executor(self):
        self.log()
        self.phase("detected")
        before = self.state.read_bytes()
        with Path(str(self.state) + ".lock").open("a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.tick()["status"], "blocked")
        self.assertEqual(self.state.read_bytes(), before)
        self.assertEqual(self.calls, [])

    def test_state_target_cannot_silently_change(self):
        self.log()
        self.phase("detected")
        self.settings["target"] = "OTHER_ALIAS"
        self.write_config()
        self.assertEqual(self.tick()["status"], "blocked")
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
