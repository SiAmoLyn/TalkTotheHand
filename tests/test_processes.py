"""Offline pipe-drain regressions; also runnable with stdlib unittest alone."""
import errno
import os
from pathlib import Path
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event
import time
import unittest
from unittest.mock import patch

from hand.processes import run_process


ROOT = Path(__file__).resolve().parents[1]


class ProcessDrainTests(unittest.TestCase):
    def setUp(self):
        directory = ROOT / "work/pkg2_1-process-tests"
        directory.mkdir(parents=True, exist_ok=True)
        self.temp = TemporaryDirectory(dir=directory)
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)

    def run_child(self, body, **options):
        return run_process([sys.executable, "-c", body], cwd=self.work,
                           stdout=self.work / "stdout", stderr=self.work / "stderr",
                           **options)

    def check_deferred_drain(self, ending):
        """Keep real bytes unread until exit/kill, without any readiness events."""
        stdout = b'{"partial":'
        stderr = b"diagnostic without newline\x00\xff"
        ready = self.work / "ready"
        cancel = Event()
        pipe_fds = set()
        children = []
        real_popen = subprocess.Popen
        real_read = os.read
        deadline = time.monotonic() + 4

        def launch(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            children.append(process)
            pipe_fds.update((process.stdout.fileno(), process.stderr.fileno()))

            def cleanup():
                if process.poll() is None:
                    process.kill()
                process.wait()
                for pipe in (process.stdout, process.stderr):
                    pipe.close()

            self.addCleanup(cleanup)
            return process

        def deferred_read(fd, count):
            if fd in pipe_fds and time.monotonic() >= deadline:
                raise AssertionError("pipe drain did not finish")
            if ending == "cancelled" and ready.exists():
                cancel.set()
            if fd in pipe_fds and children[0].poll() is None:
                raise BlockingIOError(errno.EAGAIN, "defer reads until process exit")
            return real_read(fd, count)

        body = (f"import os, time; from pathlib import Path; "
                f"os.write(1, {stdout!r}); os.write(2, {stderr!r}); "
                "Path('ready').touch()")
        if ending != "normal":
            body += "; time.sleep(30)"
        lines = []
        with patch("hand.processes.subprocess.Popen", side_effect=launch), \
                patch("selectors.DefaultSelector", side_effect=AssertionError("selector must not be used")), \
                patch("hand.processes.os.read", side_effect=deferred_read):
            result = self.run_child(body, timeout=1 if ending == "timeout" else 3,
                                    cancel=cancel, on_line=lines.append)

        self.assertTrue(ready.exists(), "child must write both pipes before termination")
        self.assertEqual(result["reason"], None if ending == "normal" else ending)
        self.assertEqual(result["exit_code"], 0 if ending == "normal" else -signal.SIGKILL)
        self.assertEqual((self.work / "stdout").read_bytes(), stdout)
        self.assertEqual((self.work / "stderr").read_bytes(), stderr)
        self.assertEqual(lines, [stdout])

    def test_normal_exit_drains_without_notifications(self):
        self.check_deferred_drain("normal")

    def test_timeout_drains_both_pipes_after_kill(self):
        self.check_deferred_drain("timeout")

    def test_cancellation_drains_both_pipes_after_kill(self):
        self.check_deferred_drain("cancelled")

    def test_transient_read_errors_do_not_finalize_logs(self):
        real_read = os.read
        real_popen = subprocess.Popen
        errors = {}
        retried = []

        def launch(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            for pipe in (process.stdout, process.stderr):
                errors[pipe.fileno()] = [BlockingIOError(errno.EAGAIN, "try again"),
                                        InterruptedError(errno.EINTR, "interrupted")]
            return process

        def flaky_read(fd, count):
            if errors.get(fd):
                retried.append(type(errors[fd][0]))
                raise errors[fd].pop(0)
            return real_read(fd, count)

        with patch("hand.processes.subprocess.Popen", side_effect=launch), \
                patch("hand.processes.os.read", side_effect=flaky_read):
            result = self.run_child("import os; os.write(1, b'out'); os.write(2, b'err')",
                                    timeout=3)

        self.assertIsNone(result["reason"], result)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual((self.work / "stdout").read_bytes(), b"out")
        self.assertEqual((self.work / "stderr").read_bytes(), b"err")
        self.assertEqual(retried.count(BlockingIOError), 2)
        self.assertEqual(retried.count(InterruptedError), 2)

    def test_large_output_and_unterminated_tail_are_preserved(self):
        stdout = b"line\n" * 40000 + b'{"partial":'
        stderr = b"\x00\xffdiagnostic" * 20000
        lines = []
        body = ("import os\n"
                "def write_all(fd, data):\n"
                "    while data:\n"
                "        data = data[os.write(fd, data):]\n"
                "write_all(1, b'line\\n' * 40000 + b'{\"partial\":')\n"
                "write_all(2, b'\\x00\\xffdiagnostic' * 20000)\n")
        result = self.run_child(body, timeout=5, on_line=lines.append)

        self.assertIsNone(result["reason"], result)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual((self.work / "stdout").read_bytes(), stdout)
        self.assertEqual((self.work / "stderr").read_bytes(), stderr)
        self.assertEqual(lines, [b"line"] * 40000 + [b'{"partial":'])


if __name__ == "__main__":
    unittest.main(verbosity=2)
