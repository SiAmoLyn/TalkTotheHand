"""Short deadlines with one EOF before a delayed peer; no model or dependency."""
import errno
import os
from pathlib import Path
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from hand.processes import run_process


ROOT = Path(__file__).resolve().parents[1]


class ShortTimeoutDrainTests(unittest.TestCase):
    def setUp(self):
        directory = ROOT / "work/pkg2_2-process-tests"
        directory.mkdir(parents=True, exist_ok=True)
        self.temp = TemporaryDirectory(dir=directory)
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)

    def check_eof_order(self, early):
        """Make reads of the peer lag kill, EOF and transient read failures.

        The child really writes before sleeping. Only read scheduling and
        readiness notifications are injected; no byte or EOF is fabricated.
        A selector that cannot be used after kill must not affect final drain.
        """
        expected = {"stdout": b'{"partial":', "stderr": b"diagnostic\x00\xff"}
        late = "stderr" if early == "stdout" else "stdout"
        real_popen, real_read = subprocess.Popen, os.read
        fds, children, events = {}, [], []
        early_eof = False
        killed = False
        after_kill_errors = [BlockingIOError(errno.EAGAIN, "late pipe not ready"),
                             InterruptedError(errno.EINTR, "late pipe interrupted")]
        guard = time.monotonic() + 3

        def launch(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            children.append(process)
            fds.update({process.stdout.fileno(): "stdout", process.stderr.fileno(): "stderr"})

            def cleanup():
                if process.poll() is None:
                    process.kill()
                process.wait()
                for pipe in (process.stdout, process.stderr):
                    pipe.close()

            self.addCleanup(cleanup)
            return process

        from hand.processes import _kill_group

        def kill(process):
            nonlocal killed
            if killed:
                return
            events.append("kill")
            killed = True
            return _kill_group(process)

        def ordered_read(fd, count):
            nonlocal early_eof
            if time.monotonic() >= guard:
                raise AssertionError("drain did not reach both real EOFs")
            name = fds.get(fd)
            if name == late:
                if not killed or children[0].poll() is None or not early_eof:
                    raise BlockingIOError(errno.EAGAIN, "defer peer until kill and first EOF")
                if after_kill_errors:
                    events.append("retry:" + type(after_kill_errors[0]).__name__)
                    raise after_kill_errors.pop(0)
                # Force multiple reads even for a tiny, unterminated fragment.
                count = min(count, 3)
            chunk = real_read(fd, count)
            if name is not None:
                events.append(("data:" if chunk else "eof:") + name)
                if name == early and not chunk:
                    early_eof = True
            return chunk

        body = ("import os, time\nfrom pathlib import Path\n"
                f"os.write(1, {expected['stdout']!r})\n"
                f"os.write(2, {expected['stderr']!r})\n"
                f"os.close({1 if early == 'stdout' else 2})\n"
                "Path('written').touch()\ntime.sleep(30)\n")
        lines = []
        started = time.monotonic()
        with patch("hand.processes.subprocess.Popen", side_effect=launch), \
                patch("hand.processes._kill_group", side_effect=kill), \
                patch("hand.processes.os.read", side_effect=ordered_read), \
                patch("selectors.DefaultSelector", side_effect=AssertionError("selector must not be used")):
            result = run_process([sys.executable, "-B", "-c", body], cwd=self.work,
                                 stdout=self.work / "stdout", stderr=self.work / "stderr",
                                 timeout=0.3, on_line=lines.append)

        self.assertLess(time.monotonic() - started, 3)
        self.assertTrue((self.work / "written").exists(), "child must write before timeout")
        self.assertEqual(result["reason"], "timeout")
        self.assertEqual(result["exit_code"], -signal.SIGKILL)
        for name, content in expected.items():
            self.assertEqual((self.work / name).read_bytes(), content)
            self.assertEqual(events.count("eof:" + name), 1)
        self.assertEqual(lines, [expected["stdout"]])
        self.assertFalse(after_kill_errors, events)
        self.assertLess(events.index("eof:" + early), events.index("kill"))
        self.assertLess(events.index("kill"), events.index("data:" + late))
        self.assertLess(events.index("eof:" + early), events.index("eof:" + late))

    def test_short_timeout_stderr_eof_before_deferred_stdout(self):
        self.check_eof_order("stderr")

    def test_short_timeout_stdout_eof_before_deferred_stderr(self):
        self.check_eof_order("stdout")

    def test_short_timeout_preserves_real_partial_line(self):
        result = run_process([sys.executable, "-B", "-c",
                              "import os,time; os.write(1,b'{\"partial\":'); time.sleep(30)"],
                             cwd=self.work, stdout=self.work / "stdout",
                             stderr=self.work / "stderr", timeout=0.3)
        self.assertEqual(result["reason"], "timeout")
        self.assertEqual(result["exit_code"], -signal.SIGKILL)
        self.assertEqual((self.work / "stdout").read_bytes(), b'{"partial":')
        self.assertEqual((self.work / "stderr").read_bytes(), b"")


if __name__ == "__main__":
    unittest.main(verbosity=2)
