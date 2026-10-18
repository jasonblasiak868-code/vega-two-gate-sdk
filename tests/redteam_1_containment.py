"""RED TEAM PASS 1 (two-gate): attacks a hostile contained job can mount. Each test is an attack that WORKED before the 2026-10-16 fixes."""
import os
import pathlib
import sys
import time
import unittest

import fixture_kronos as fx
from vega_custody.capsule import capsule as cap
from vega_twogate.kronos import contain


def req(data="payload"):
    return cap.make(data, {"source": "user"})


class T(unittest.TestCase):
    def setUp(self):
        self.f = fx.build()

    def test_output_flood_is_cut_off_without_buffering_it_all(self):
        """Attack: print endlessly. communicate() used to hold everything in memory until exit."""
        flood = [fx.PY, "-I", "-c", "import sys\nwhile True:\n    sys.stdout.write('x' * 65536)\n    sys.stdout.flush()"]
        t0 = time.time()
        with self.assertRaises(contain.ContainmentRefused) as cm:
            self.f.k.run("flood", flood, req(), self.f.minted(), timeout=8)
        self.assertLess(time.time() - t0, 6, "the cap must stop it long before the timeout")
        self.assertIn("over the cap", str(cm.exception))
        rm = fx.kinds(self.f.dag, "KRONOS_REMOVE")[-1]["payload"]
        self.assertTrue(rm["removed_ok"])

    def test_symlink_planted_in_the_workdir_is_never_followed_or_hashed(self):
        """Attack: link to a secret outside; the teardown used to read it and put its hash in the record."""
        canary = self.f.tmp / "canary.txt"
        canary.write_text("TOP SECRET CANARY")
        script = "import os,sys\ntry:\n    os.symlink(sys.argv[1], 'link')\nexcept OSError:\n    open('nolink','w').write('1')\nprint('ok')"
        self.f.k.run("sym", [fx.PY, "-I", "-c", script, str(canary)], req(), self.f.minted(), timeout=10)
        rm = fx.kinds(self.f.dag, "KRONOS_REMOVE")[-1]["payload"]
        import hashlib
        canary_hash = hashlib.sha256(canary.read_bytes()).hexdigest()
        self.assertNotIn(canary_hash, [x["sha256"] for x in rm["files"]])
        self.assertEqual(canary.read_text(), "TOP SECRET CANARY", "the target must be untouched")
        if not any(x["path"] == "nolink" for x in rm["files"]):       # the symlink was created: it must be recorded as skipped
            self.assertTrue(any(x["path"] == "link" and x["why"] == "symlink" for x in rm["skipped"]))

    def test_a_kill_that_cannot_be_confirmed_is_not_silent(self):
        """Attack: a process that survives the kill. The old code ignored every kill error."""
        orig = contain.ProcessJob.kill
        contain.ProcessJob.kill = lambda self, wait=5.0: (orig(self, wait), False)[1]   # really kill (cleanup) but report 'not confirmed dead'
        try:
            with self.assertRaises(contain.ContainmentRefused) as cm:
                self.f.k.run("hang", [fx.PY, "-I", "-c", "import time; time.sleep(60)"], req(), self.f.minted(), timeout=1)
        finally:
            contain.ProcessJob.kill = orig
        self.assertIn("NOT be confirmed dead", str(cm.exception))
        self.assertTrue(self.f.hs.engaged())

    def test_kill_tree_itself_reports_failure_for_a_process_that_will_not_die(self):
        """The verification inside _kill_tree: a process that never exits must make it return False (a nonexistent pid keeps the real kill calls harmless)."""
        import subprocess

        class Stubborn:
            pid = 4194000

            def kill(self):
                pass

            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired("x", timeout)

            def poll(self):
                return None
        self.assertFalse(contain._kill_tree(Stubborn(), wait=0.05))

        class Dies(Stubborn):
            def wait(self, timeout=None):
                return 0

            def poll(self):
                return 0
        self.assertTrue(contain._kill_tree(Dies(), wait=0.05))

    def test_workdir_that_cannot_be_removed_engages_the_hard_stop(self):
        """Attack: leave residue. A failed removal used to pass silently (ignore_errors)."""
        orig = contain.shutil.rmtree
        contain.shutil.rmtree = lambda *a, **k: None
        try:
            with self.assertRaises(contain.ContainmentRefused):
                self.f.k.run("residue", [fx.PY, "-I", "-c", "print('hi')"], req(), self.f.minted(), timeout=10)
        finally:
            contain.shutil.rmtree = orig
        self.assertTrue(self.f.hs.engaged())
        rm = fx.kinds(self.f.dag, "KRONOS_REMOVE")[-1]["payload"]
        self.assertFalse(rm["removed_ok"])

    def test_file_named_like_a_secret_cannot_crash_the_teardown_record(self):
        """Attack: create files named key, token, password. The DAG refuses forbidden field names, which used to crash the run after cleanup."""
        script = "for n in ('key','token','password','secret','seed','api_key'):\n    open(n,'w').write('x')\nprint('ok')"
        res = self.f.k.run("names", [fx.PY, "-I", "-c", script], req(), self.f.minted(), timeout=10)
        self.assertEqual(cap.data_of(res).decode().strip(), "ok")
        rm = fx.kinds(self.f.dag, "KRONOS_REMOVE")[-1]["payload"]
        self.assertEqual(sorted(x["path"] for x in rm["files"]), ["api_key", "key", "password", "secret", "seed", "token"])

    def test_child_does_not_inherit_the_parents_secrets(self):
        os.environ["KRONOS_CANARY_SECRET"] = "s3cr3t-canary"
        try:
            res = self.f.k.run("env", [fx.PY, "-I", "-c", "import os; print(sorted(os.environ))"], req(), self.f.minted(), timeout=10)
        finally:
            del os.environ["KRONOS_CANARY_SECRET"]
        self.assertNotIn("KRONOS_CANARY_SECRET", cap.data_of(res).decode())

    def test_a_huge_stdin_the_child_never_reads_does_not_deadlock(self):
        """Attack: stall the gate by never draining stdin."""
        t0 = time.time()
        res = self.f.k.run("stdin", [fx.PY, "-I", "-c", "import time; time.sleep(0.3); print('done')"], req("x" * 3_000_000), self.f.minted(), timeout=15)
        self.assertEqual(cap.data_of(res).decode().strip(), "done")
        self.assertLess(time.time() - t0, 12)


if __name__ == "__main__":
    unittest.main()
