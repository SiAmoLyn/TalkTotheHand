"""Blocking reader regressions, collected by pytest or standalone unittest."""
import os
from pathlib import Path
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event, Thread, current_thread, main_thread
import time
import unittest
from unittest.mock import patch

from hand.opencode import EventParser
from hand.processes import run_process


ROOT = Path(__file__).resolve().parents[1]


class BlockingReaderTests(unittest.TestCase):
    def setUp(self):
        directory = ROOT / "work/pkg2_3-process-tests"
        directory.mkdir(parents=True, exist_ok=True)
        self.temp = TemporaryDirectory(dir=directory)
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)

    def run_child(self, body, **options):
        return run_process([sys.executable, "-B", "-c", body], cwd=self.work,
                           stdout=self.work / "stdout", stderr=self.work / "stderr",
                           **options)

    def test_short_timeout_preserves_exact_bug_reproduction(self):
        """Keep the reported 0.3s budget and the exact unterminated bytes."""
        lines = []
        started = time.monotonic()
        result = self.run_child(
            'import os,time; os.write(1,b"{XXpartialXX:"); time.sleep(30)',
            timeout=0.3, on_line=lines.append)
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(result["reason"], "timeout")
        self.assertEqual(result["exit_code"], -signal.SIGKILL)
        self.assertEqual((self.work / "stdout").read_bytes(), b"{XXpartialXX:")
        self.assertEqual((self.work / "stderr").read_bytes(), b"")
        self.assertEqual(lines, [b"{XXpartialXX:"])
        self.assertEqual(set(result), {"command", "exit_code", "reason", "stdout_path",
                                       "stderr_path", "minutes"})

    def test_one_blocking_daemon_reader_per_pipe_without_selectors(self):
        real_popen, real_read = subprocess.Popen, os.read
        fds, threads = {}, {}

        def launch(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            fds.update({process.stdout.fileno(): "stdout", process.stderr.fileno(): "stderr"})
            return process

        def read(fd, count):
            if fd in fds:
                reader = current_thread()
                self.assertIsNot(reader, main_thread())
                self.assertTrue(reader.daemon)
                self.assertTrue(os.get_blocking(fd))
                self.assertEqual(count, 65536)
                previous = threads.setdefault(fds[fd], reader)
                self.assertIs(previous, reader)
            return real_read(fd, count)

        with patch("hand.processes.subprocess.Popen", side_effect=launch), \
                patch("hand.processes.os.read", side_effect=read), \
                patch("hand.processes.os.set_blocking", side_effect=AssertionError("leave pipes blocking")), \
                patch("selectors.DefaultSelector", side_effect=AssertionError("selector must not be used")):
            result = self.run_child("import os; os.write(1,b'out'); os.write(2,b'err')", timeout=3)

        self.assertIsNone(result["reason"], result)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual((self.work / "stdout").read_bytes(), b"out")
        self.assertEqual((self.work / "stderr").read_bytes(), b"err")
        self.assertEqual(set(threads), {"stdout", "stderr"})
        self.assertIsNot(threads["stdout"], threads["stderr"])
        self.assertFalse(any(t.is_alive() for t in threads.values()))

    def test_unterminated_bytes_are_flushed_before_termination(self):
        expected = {"stdout": b"\x00\xffpartial", "stderr": b"\xff\x00diagnostic"}
        lines, observed = [], []

        def stop_when_flushed():
            if all((self.work / name).read_bytes() == data for name, data in expected.items()):
                self.assertEqual(lines, [], "the unterminated tail must wait for the joins")
                observed.append(True)
                return "token budget exceeded"
            return None

        result = self.run_child(
            f"import os,time; os.write(1,{expected['stdout']!r}); "
            f"os.write(2,{expected['stderr']!r}); time.sleep(30)",
            timeout=3, on_line=lines.append, stop_reason=stop_when_flushed)
        self.assertTrue(observed, "both raw files must be readable before killing the writer")
        self.assertEqual(result["reason"], "token budget exceeded")
        self.assertEqual(result["exit_code"], -signal.SIGKILL)
        self.assertEqual(lines, [expected["stdout"]])
        for name, data in expected.items():
            self.assertEqual((self.work / name).read_bytes(), data)

    def test_complete_lines_run_in_reader_and_tail_runs_after_join(self):
        calls = []

        def consume(line):
            self.assertIn(line, (self.work / "stdout").read_bytes())
            if current_thread() is main_thread():
                self.assertTrue(calls)
                self.assertFalse(calls[0][1].is_alive(), "join must precede the tail callback")
            calls.append((line, current_thread()))

        result = self.run_child(
            "import os,time; os.write(1,b'first\\n\\nsplit'); time.sleep(.05); "
            "os.write(1,b' line\\n\\xfftail'); os.write(2,b'not a stdout callback\\n')",
            timeout=3, on_line=consume)
        self.assertIsNone(result["reason"], result)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual([line for line, _ in calls], [b"first", b"", b"split line", b"\xfftail"])
        self.assertTrue(all(thread is calls[0][1] for _, thread in calls[:-1]))
        self.assertIsNot(calls[0][1], main_thread())
        self.assertIs(calls[-1][1], main_thread())
        self.assertEqual((self.work / "stdout").read_bytes(), b"first\n\nsplit line\n\xfftail")

    def test_live_parser_still_stops_for_token_budget(self):
        parser = EventParser()
        event = b'{"type":"step_finish","part":{"tokens":{"total":101}}}\n'
        result = self.run_child(
            f"import os,time; os.write(1,{event!r}); time.sleep(30)",
            timeout=3, on_line=parser.consume,
            stop_reason=lambda: "token budget exceeded" if parser.tokens > 100 else None)
        self.assertEqual(result["reason"], "token budget exceeded")
        self.assertEqual(result["exit_code"], -signal.SIGKILL)
        self.assertEqual(parser.tokens, 101)
        self.assertEqual((self.work / "stdout").read_bytes(), event)

    def test_callback_failures_reach_caller_and_reap_process(self):
        real_popen = subprocess.Popen
        children = []

        def launch(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            children.append(process)
            return process

        for error_type in (ValueError, RuntimeError):
            with self.subTest(error_type=error_type):
                def consume(line):
                    self.assertEqual((self.work / "stdout").read_bytes(), b"line\npartial")
                    raise error_type("parser failed")

                with patch("hand.processes.subprocess.Popen", side_effect=launch):
                    body = "import os,time; os.write(1,b'line\\npartial'); time.sleep(30)"
                    if error_type is ValueError:
                        result = self.run_child(body, timeout=3, on_line=consume)
                        self.assertEqual(result["reason"], "parser failed")
                        self.assertIsNone(result["exit_code"])
                    else:
                        with self.assertRaisesRegex(RuntimeError, "parser failed"):
                            self.run_child(body, timeout=3, on_line=consume)
                self.assertIsNotNone(children[-1].poll())
                self.assertTrue(children[-1].stdout.closed)
                self.assertTrue(children[-1].stderr.closed)

    def test_bounded_joins_keep_stop_result_and_reader_handles(self):
        """Shorten only the test's join wait; verify production supplies 5s."""
        real_popen, real_read = subprocess.Popen, os.read
        release = Event()
        fds, children, readers, joins = {}, [], [], []

        def launch(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            children.append(process)
            fds[process.stdout.fileno()] = "stdout"
            return process

        def stalled_read(fd, count):
            if fd in fds and not release.wait(3):
                raise AssertionError("test failed to release the reader")
            return real_read(fd, count)

        class ShortJoinThread(Thread):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                readers.append(self)

            def join(self, timeout=None):
                joins.append((timeout, children[0].poll()))
                return super().join(timeout=0.05)

        lines = []
        try:
            with patch("hand.processes.subprocess.Popen", side_effect=launch), \
                    patch("hand.processes.os.read", side_effect=stalled_read), \
                    patch("hand.processes.Thread", ShortJoinThread):
                result = self.run_child(
                    "import os,time; os.write(1,b'partial'); time.sleep(30)",
                    timeout=0.3, on_line=lines.append)
            self.assertEqual(result["reason"], "timeout")
            self.assertEqual(result["exit_code"], -signal.SIGKILL)
            self.assertEqual(joins, [(5, -signal.SIGKILL), (5, -signal.SIGKILL)])
            self.assertTrue(readers[0].is_alive())
            self.assertFalse(children[0].stdout.closed, "reader must retain its descriptor")
            self.assertEqual(lines, [], "main thread must not parse a live reader's tail")
        finally:
            release.set()
            for reader in readers:
                Thread.join(reader, timeout=3)
        self.assertFalse(any(reader.is_alive() for reader in readers))
        self.assertEqual((self.work / "stdout").read_bytes(), b"partial")
        self.assertTrue(children[0].stdout.closed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
