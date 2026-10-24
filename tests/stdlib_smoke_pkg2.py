"""Narrow, dependency-free checks of the real modules, runnable with Python 3.9+.

This does not replace pytest, Pydantic/LangGraph regression, or Python 3.12 QA.
All files, logs and temporary directories remain inside the project.
"""
import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Event, Timer
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from hand.opencode import EventParser, permission_config
from hand.processes import run_process
from hand.tools import gate_aabb, gate_clipsim, blender_run, godot_run

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures"


class Smoke(unittest.TestCase):
    def setUp(self):
        (ROOT / "work/pkg2-smoke").mkdir(parents=True, exist_ok=True)
        self.temp = TemporaryDirectory(dir=ROOT / "work/pkg2-smoke")
        self.work = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_parser_three_streams(self):
        for fixture in ("normal", "timeout", "error"):
            parser = EventParser()
            for line in (FIXTURES / f"opencode-{fixture}.jsonl").read_bytes().splitlines():
                parser.consume(line)
            self.assertTrue(parser.session_id)
            if fixture == "normal":
                self.assertEqual(parser.tokens, 22)
                self.assertTrue(parser.finished)
                self.assertFalse(parser.errors)
            else:
                self.assertTrue(parser.errors)
                self.assertFalse(parser.finished)

    def test_policy(self):
        policy = permission_config(self.work, [])
        self.assertEqual(policy["share"], "disabled")
        self.assertEqual(policy["permission"]["bash"], "deny")
        self.assertEqual(policy["permission"]["read"]["**/.ssh/**"], "deny")

    def test_hard_timeout_and_partial_bytes(self):
        result = run_process([sys.executable, "-c", "import os,time; os.write(1,b'partial'); time.sleep(30)"],
                             cwd=self.work, stdout=self.work / "out", stderr=self.work / "err", timeout=.2)
        self.assertEqual(result["reason"], "timeout")
        self.assertLess(result["exit_code"], 0)
        self.assertEqual((self.work / "out").read_bytes(), b"partial")

    def test_cancel(self):
        cancel = Event()
        timer = Timer(.2, cancel.set)
        timer.start()
        try:
            result = run_process([sys.executable, "-c", "import time; print('partial',flush=True); time.sleep(30)"],
                                 cwd=self.work, stdout=self.work / "out", stderr=self.work / "err",
                                 timeout=10, cancel=cancel)
        finally:
            timer.cancel()
        self.assertEqual(result["reason"], "cancelled")
        self.assertEqual((self.work / "out").read_text(), "partial\n")

    def test_process_group(self):
        child = "import time; from pathlib import Path; time.sleep(.6); Path('survived').touch()"
        parent = f"import subprocess,sys; subprocess.Popen([sys.executable,'-c',{child!r}])"
        result = run_process([sys.executable, "-c", parent], cwd=self.work, stdout=self.work / "out",
                             stderr=self.work / "err", timeout=.2)
        self.assertEqual(result["reason"], "timeout")
        time.sleep(.7)
        self.assertFalse((self.work / "survived").exists())

    def test_aabb_numeric_pass_fail(self):
        (self.work / "bounds.json").write_bytes((FIXTURES / "aabb.json").read_bytes())
        result = gate_aabb(Path("bounds.json"), workspace=self.work, expected_dimensions={"part": [1, 2, 3]})
        self.assertTrue(result["passed"], result)
        self.assertTrue(all("measured" in c and "threshold" in c for c in result["checks"]))
        result = gate_aabb(Path("bounds.json"), workspace=self.work, expected_dimensions={"part": [5, 2, 3]})
        self.assertEqual(result["status"], "failed")
        self.assertEqual(json.loads(Path(result["report_path"]).read_text()), result)

    def test_four_path_rejections(self):
        results = [blender_run(Path("../escape"), workspace=self.work),
                   godot_run(Path("../escape"), workspace=self.work),
                   gate_aabb(Path("../escape"), workspace=self.work),
                   gate_clipsim(Path("../escape"), Path("ref"), workspace=self.work,
                                reference_sha256="0" * 64, reference_band=[0, 1])]
        for result in results:
            self.assertEqual(result["status"], "blocked")
            self.assertIn("whitelist", result["error"])
            self.assertTrue(Path(result["report_path"]).is_file())

    def test_clip_score_and_missing_script(self):
        (self.work / "image").write_bytes(b"fixture")
        (self.work / "ref").write_bytes(b"locked")
        (self.work / "sim.py").write_text('print(\'{"score":0.8}\')\n')
        options = dict(artifact=Path("image"), reference=Path("ref"), workspace=self.work,
                       reference_sha256=hashlib.sha256(b"locked").hexdigest(), reference_band=[.7, .9])
        result = gate_clipsim(**options, script=Path("sim.py"))
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["score"], .8)
        result = gate_clipsim(**options, script=Path("missing.py"))
        self.assertEqual(result["status"], "blocked")
        self.assertNotIn("score", result)

    def test_fake_blender_godot_process_wrappers(self):
        tool = self.work / "fake-tool"
        tool.write_text(f"#!{sys.executable}\nfrom pathlib import Path\nPath('product').write_text('fixture')\nprint('PASS=2 FAIL=0')\n")
        tool.chmod(0o700)
        (self.work / "build.py").write_text("# fixture")
        result = blender_run(Path("build.py"), workspace=self.work, executable=str(tool))
        self.assertTrue(result["passed"], result)
        self.assertTrue(any(Path(a["path"]).name == "product" for a in result["artifacts"]))
        (self.work / "project.godot").write_text("config_version=5")
        result = godot_run(Path("."), workspace=self.work, executable=str(tool))
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["pass_count"], 2)
        self.assertEqual(result["fail_count"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
