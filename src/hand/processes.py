"""Bounded POSIX process groups, streamed evidence, no shell or model dependency."""
from __future__ import annotations

import os
import math
from pathlib import Path
from queue import SimpleQueue
import signal
import subprocess
from threading import Event, Thread
import time
from typing import Callable


def run_process(argv: list[str], *, cwd: Path, stdout: Path, stderr: Path,
                timeout: float, cancel: Event | None = None,
                on_line: Callable[[bytes], None] | None = None,
                stop_reason: Callable[[], str | None] | None = None,
                env: dict[str, str] | None = None) -> dict:
    """Stream each pipe through its own blocking reader, including partial lines.

    SIGKILL applies to the entire new process group on timeout/cancel, even if
    the parent exited while descendants retained the pipes. Bytes are preserved
    before parsing; malformed JSON therefore cannot destroy the source evidence.
    Readers flush raw bytes before stdout callbacks. The main thread monitors
    termination and joins both readers before parsing the unterminated tail.
    Callbacks for complete stdout lines run on the stdout reader thread.
    """
    started = time.monotonic()
    result = {"command": argv, "exit_code": None, "reason": None,
              "stdout_path": str(stdout), "stderr_path": str(stderr)}
    process = None
    pending = b""
    readers = {}
    reader_errors = SimpleQueue()

    def drain(pipe, destination):
        nonlocal pending
        try:
            # Each reader owns its handles. If a detached descendant holds a
            # pipe beyond the bounded join, the main thread must not close or
            # reuse that descriptor underneath an outstanding os.read.
            with pipe, destination:
                fd = pipe.fileno()
                while True:
                    try:
                        chunk = os.read(fd, 65536)
                    except InterruptedError:
                        continue
                    except BlockingIOError:
                        # Pipes are blocking; tolerate a transient injected or
                        # platform error without confusing it with EOF.
                        time.sleep(0.001)
                        continue
                    if not chunk:
                        break
                    destination.write(chunk)
                    destination.flush()
                    if destination is out and on_line:
                        pending += chunk
                        while b"\n" in pending:
                            line, pending = pending.split(b"\n", 1)
                            on_line(line)
        except BaseException as exc:
            # Thread failures must retain the caller's exception semantics,
            # rather than disappearing into an unhandled daemon exception.
            reader_errors.put(exc)

    out = stdout.open("wb")
    try:
        err = stderr.open("wb")
    except BaseException:
        out.close()
        raise
    try:
        try:
            if not math.isfinite(timeout) or timeout <= 0:
                raise ValueError("timeout must be positive and finite")
            if cancel and cancel.is_set():
                result["reason"] = "cancelled"
                return result
            deadline = started + timeout
            process = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       bufsize=0, start_new_session=True, env=env)
            for name, pipe, destination in (("stdout", process.stdout, out),
                                            ("stderr", process.stderr, err)):
                reader = Thread(target=drain, args=(pipe, destination),
                                name=f"hand-{name}", daemon=True)
                reader.start()
                readers[destination] = reader
            # A parent may exit while descendants still hold the pipes, or
            # close its pipes while continuing to run. Monitor both lifetimes.
            while process.poll() is None or any(r.is_alive() for r in readers.values()):
                if not reader_errors.empty():
                    raise reader_errors.get()
                reason = ("cancelled" if cancel and cancel.is_set() else
                          "timeout" if time.monotonic() >= deadline else
                          stop_reason() if stop_reason else None)
                if reason:
                    result["reason"] = reason
                    break
                time.sleep(min(0.025, max(0, deadline - time.monotonic())))
        finally:
            if process:
                # Kill before joining: descendants may still own write ends.
                _kill_group(process)
                process.wait()
            for reader in readers.values():
                reader.join(timeout=5)

        if not reader_errors.empty():
            raise reader_errors.get()
        if any(r.is_alive() for r in readers.values()):
            # A writer outside the killed group (or a stalled callback) can
            # outlive the joins. Keep the original stop reason and exit code;
            # never race that reader by parsing or closing its pending data.
            result["exit_code"] = process.wait()
            return result
        if pending and on_line:
            on_line(pending)
        result["exit_code"] = process.wait()
    except (OSError, ValueError) as exc:
        result["reason"] = str(exc)
    finally:
        # Readers close their own handles, including on failure. Close only
        # handles that were never handed to a successfully started reader.
        for destination in (out, err):
            if destination not in readers:
                destination.close()
        if process:
            for pipe, destination in ((process.stdout, out), (process.stderr, err)):
                if destination not in readers:
                    if pipe and not pipe.closed:
                        pipe.close()
        result["minutes"] = (time.monotonic() - started) / 60
    return result


def _kill_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
