"""Regression tests for the three findings of the security-audit pass over Kronos (2026-10-17). Each of these was reproduced with a local proof of concept before it was fixed."""
import os
import pathlib
import tempfile
import time
import unittest

import fixture_kronos as fx
from vega_custody.capsule import capsule as cap
from vega_twogate.kronos import contain


def req(data="payload"):
    return cap.make(data, {"source": "user"})


class F1_ForgedRestore(unittest.TestCase):
    """HIGH. The hard stop was lifted by anyone who could append a hash-valid HARD_STOP_RESTORED record and delete the marker file: the chain is hash-linked, not signed."""

    def forge(self, f):
        from vega_custody.dag.dag import Dag
        other = Dag([str(p) for p in f.dag.content.paths], [str(p) for p in f.dag.locations.paths])      # what any process running as the same user can do
        other.record("HARD_STOP_RESTORED", {"leaf": "forged"})
        other.record("HARD_STOP_RESTORED", {"leaf": "forged", "commitment": {"kind": "WCOMMIT", "action": contain.RESTORE_ACTION, "action_hash": "0" * 64, "expiry": 9999999999, "nonce_sha256": "1" * 64}})
        return contain.HardStop(f.hs.path, Dag([str(p) for p in f.dag.content.paths], [str(p) for p in f.dag.locations.paths]), f.led)

    def test_forged_restore_records_and_a_deleted_marker_do_not_lift_the_stop(self):
        f = fx.build()
        f.hs.engage("poc")
        fresh = self.forge(f)
        os.remove(f.hs.path)
        self.assertTrue(fresh.engaged())
        self.assertTrue(contain.HardStop(f.hs.path, f.dag).engaged())                  # no ledger given: cannot verify any restore, stays engaged

    def test_a_real_human_restore_lifts_it_and_a_replayed_restore_record_cannot_lift_the_next_stop(self):
        f = fx.build()
        f.hs.engage("first")
        signed = f.sign(contain.RESTORE_ACTION, {"why": "reviewed"})
        f.hs.restore(f.commit, {"why": "reviewed"}, signed)
        self.assertFalse(f.hs.engaged())
        old_restore = [e for e in f.dag.content.entries() if e["kind"] == "HARD_STOP_RESTORED"][-1]
        f.hs.engage("second")
        from vega_custody.dag.dag import Dag
        other = Dag([str(p) for p in f.dag.content.paths], [str(p) for p in f.dag.locations.paths])
        other.record("HARD_STOP_RESTORED", old_restore["payload"])                      # replay the genuine record, hoping it lifts the new stop
        os.remove(f.hs.path)
        self.assertTrue(contain.HardStop(f.hs.path, Dag([str(p) for p in f.dag.content.paths], [str(p) for p in f.dag.locations.paths]), f.led).engaged())

    def test_a_restore_signed_for_another_action_does_not_count(self):
        f = fx.build()
        f.hs.engage("x")
        other = f.minted("run_job")
        f.dag.record("HARD_STOP_RESTORED", {"leaf": "x", "commitment": other})
        os.remove(f.hs.path)
        self.assertTrue(f.hs.engaged())


@unittest.skipUnless(os.name == "nt", "the job object is the Windows containment; on POSIX a descendant that calls setsid is a documented limit (use a cgroup or a container)")
class F2_DescendantsOutliveTheJob(unittest.TestCase):
    """HIGH. A descendant that detached and changed directory kept running after the job ended and the work folder was removed."""

    def code(self, marker, delay=3, chdir=True):
        m = str(marker).replace("\\", "/")
        return ("import subprocess, sys, os\n"
                "child = \"import os, time; %s time.sleep(%d); open(%r, 'w').write('escaped')\"\n"
                "subprocess.Popen([sys.executable, '-I', '-c', child], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=0x00000008 | 0x00000200)\n"
                "print('finished normally')\n") % ("os.chdir(os.path.dirname(%r));" % m if chdir else "", delay, m)

    def test_a_detached_descendant_is_found_killed_and_the_run_is_refused(self):
        marker = pathlib.Path(tempfile.mkdtemp()) / "survivor.txt"
        f = fx.build()
        with self.assertRaises(contain.ContainmentRefused) as cm:
            f.k.run("escape", [fx.PY, "-I", "-c", self.code(marker)], req(), f.minted(), timeout=20)
        self.assertIn("outlived", str(cm.exception))
        self.assertTrue(f.hs.engaged())
        time.sleep(5)
        self.assertFalse(marker.exists(), "the descendant survived the containment and ran")

    def test_the_job_is_closed_on_every_path_so_nothing_is_left(self):
        marker = pathlib.Path(tempfile.mkdtemp()) / "survivor2.txt"
        f = fx.build()
        with self.assertRaises(contain.ContainmentRefused):
            f.k.run("escape2", [fx.PY, "-I", "-c", self.code(marker, delay=2, chdir=False)], req(), f.minted(), timeout=20)
        time.sleep(4)
        self.assertFalse(marker.exists())

    def test_a_well_behaved_job_still_runs_and_releases(self):
        f = fx.build()
        out = f.k.run("fine", [fx.PY, "-I", "-c", "print('ok')"], req(), f.minted(), timeout=20)
        self.assertEqual(cap.data_of(out).strip(), b"ok")
        self.assertFalse(f.hs.engaged())

    def test_process_count_is_capped_by_the_job(self):
        f = fx.build()
        code = ("import subprocess, sys\nps = []\n"
                "try:\n    for i in range(80):\n        ps.append(subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(5)']))\nexcept OSError:\n    print('capped at', len(ps))\n"
                "for p in ps:\n    p.kill()\n")
        try:
            out = f.k.run("fork", [fx.PY, "-I", "-c", code], req(), f.minted(), timeout=30)
            self.assertIn(b"capped", out and cap.data_of(out))
        except contain.ContainmentRefused:
            pass                                                                       # refused is also a safe outcome: the point is that 80 processes never ran


class F3_ReviewerFailures(unittest.TestCase):
    """LOW/MEDIUM. A reviewer that crashed or answered without 'ok' escaped as an exception and left the work folder (with the job's output) on disk."""

    def folders(self):
        return {d for d in os.listdir(tempfile.gettempdir()) if d.startswith("vega_kronos_")}

    def test_a_reviewer_that_crashes_or_answers_loosely_refuses_the_output_and_cleans_up(self):
        for reviewer in (lambda out: {}, lambda out: 1 / 0, lambda out: "yes", lambda out: None, lambda out: {"ok": "yes"}, lambda out: {"ok": 1}):
            f = fx.build()
            before = self.folders()
            with self.assertRaises(contain.ContainmentRefused):
                f.k.run("rev", [fx.PY, "-I", "-c", "print('private output')"], req(), f.minted(), timeout=20, reviewer=reviewer)
            self.assertEqual(self.folders() - before, set())

    def test_an_explicit_true_still_releases(self):
        f = fx.build()
        out = f.k.run("rev", [fx.PY, "-I", "-c", "print('fine')"], req(), f.minted(), timeout=20, reviewer=lambda o: {"ok": True, "hard": False, "reason": "ok"})
        self.assertEqual(cap.data_of(out).strip(), b"fine")


class F4_StartFailures(unittest.TestCase):
    def test_a_program_that_cannot_start_leaves_no_work_folder_and_runs_nothing(self):
        f = fx.build()
        before = {d for d in os.listdir(tempfile.gettempdir()) if d.startswith("vega_kronos_")}
        with self.assertRaises(contain.ContainmentRefused):
            f.k.run("nostart", ["this-program-does-not-exist-anywhere"], req(), f.minted(), timeout=10)
        self.assertEqual({d for d in os.listdir(tempfile.gettempdir()) if d.startswith("vega_kronos_")} - before, set())


if __name__ == "__main__":
    unittest.main()
