"""Trace only a local fake Python child, using stdlib; never starts a model.

Run with -B from the project root. All evidence is under work/pkg2_2-checks.
The parent timestamps reads, EOFs and kill; no cross-process monotonic comparison.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import selectors
import subprocess
import sys
import time
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from hand import processes


def trace(directory, timeout):
    directory.mkdir(parents=True, exist_ok=True)
    expected = {"stdout": b'{"partial":', "stderr": b"diagnostic\x00\xff"}
    real_popen, real_read = subprocess.Popen, os.read
    real_kill = processes._kill_group
    real_select = selectors.DefaultSelector.select
    fds, events = {}, []
    started = time.monotonic()

    def record(kind, **data):
        events.append(dict(seconds=time.monotonic() - started, kind=kind, **data))

    def launch(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        fds.update({child.stdout.fileno(): "stdout", child.stderr.fileno(): "stderr"})
        record("launched", pid=child.pid, fds=fds.copy())
        return child

    def read(fd, count):
        try:
            chunk = real_read(fd, count)
        except OSError as exc:
            if fd in fds:
                record("read_error", pipe=fds[fd], error=repr(exc))
            raise
        if fd in fds:
            record("read" if chunk else "eof", pipe=fds[fd], hex=chunk.hex())
        return chunk

    def kill(child):
        record("kill_group", pid=child.pid)
        return real_kill(child)

    def select(selector, wait):
        record("select_begin", timeout=wait)
        ready = real_select(selector, wait)
        record("select_end", ready=[fds.get(key.fd, str(key.fd)) for key, mask in ready])
        return ready

    body = ("import json,os,time\nfrom pathlib import Path\n"
            f"out_count=os.write(1,{expected['stdout']!r})\n"
            f"err_count=os.write(2,{expected['stderr']!r})\n"
            "Path('child-write-receipt.json').write_text(json.dumps("
            "dict(stdout_bytes=out_count,stderr_bytes=err_count)))\n"
            "time.sleep(30)\n")
    lines = []
    with patch.object(processes.subprocess, "Popen", side_effect=launch), \
            patch.object(processes.os, "read", side_effect=read), \
            patch.object(processes, "_kill_group", side_effect=kill), \
            patch.object(selectors.DefaultSelector, "select", select):
        result = processes.run_process([sys.executable, "-B", "-c", body], cwd=directory,
                                       stdout=directory / "stdout", stderr=directory / "stderr",
                                       timeout=timeout, on_line=lines.append)
    observed = {name: (directory / name).read_bytes() for name in expected}
    receipt_path = directory / "child-write-receipt.json"
    receipt = json.loads(receipt_path.read_text()) if receipt_path.exists() else None
    written = {name + "_bytes": len(value) for name, value in expected.items()}
    summary = dict(timeout=timeout, result=result,
                   child_write_receipt=receipt,
                   logs_hex={name: value.hex() for name, value in observed.items()},
                   passed=(result["reason"] == "timeout" and result["exit_code"] == -9
                           and receipt == written and observed == expected
                           and lines == [expected["stdout"]]))
    (directory / "trace.json").write_text(json.dumps(events, indent=2) + "\n")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, default=1)
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error("--repeat must be positive")
    checks = ROOT / "work/pkg2_2-checks"
    checks.mkdir(parents=True, exist_ok=True)
    output = checks / ("trace-" + str(time.time_ns()))
    output.mkdir()
    environment = dict(python=sys.version, executable=sys.executable,
                       platform=platform.platform(), selector=selectors.DefaultSelector.__name__,
                       module=str(Path(processes.__file__).resolve()),
                       source_sha256=hashlib.sha256(Path(processes.__file__).read_bytes()).hexdigest(),
                       dependencies={name: importlib.util.find_spec(name) is not None
                                     for name in ("pytest", "pydantic", "langgraph", "yaml")})
    (output / "environment.json").write_text(json.dumps(environment, indent=2) + "\n")
    results = [trace(output / (str(index) + "-" + str(timeout)), timeout)
               for index in range(args.repeat) for timeout in (0.3, 5.0)]
    (output / "summary.json").write_text(json.dumps(results, indent=2) + "\n")
    passed = sum(row["passed"] for row in results)
    print(f"{passed}/{len(results)} byte-exact timeout checks passed; evidence: {output.relative_to(ROOT)}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
