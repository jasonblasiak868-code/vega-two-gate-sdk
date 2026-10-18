"""RED TEAM PASS 2 (two-gate): attacks on state, authority and input. Different angle from pass 1: the attacker owns the file system or the caller, not the child."""
import json
import os
import unittest

import fixture_kronos as fx
from vega_custody.capsule import capsule as cap
from vega_jacket import humanauth
from vega_twogate.kronos import contain


def req(data="payload"):
    return cap.make(data, {"source": "user"})


OKJOB = [fx.PY, "-I", "-c", "print('ok')"]


class T(unittest.TestCase):
    def setUp(self):
        self.f = fx.build()

    def test_deleting_the_marker_file_does_not_lift_the_hard_stop(self):
        """Attack: any process with file access unlinks HARD_STOP. The stop must also live in the record."""
        self.f.hs.engage("test")
        os.remove(self.f.hs.path)
        self.assertTrue(self.f.hs.engaged())
        with self.assertRaises(contain.HardStopEngaged):
            self.f.k.run("after", OKJOB, req(), self.f.minted(), timeout=10)

    def test_only_the_human_key_lifts_it_even_after_the_file_is_gone(self):
        self.f.hs.engage("test")
        os.remove(self.f.hs.path)
        params = {"scope": "hard_stop", "reason": "reviewed"}
        with self.assertRaises(contain.ContainmentRefused):
            self.f.hs.restore(self.f.commit, params, {"nonce": "n", "expiry": 9999999999, "signature": "00"})
        self.assertTrue(self.f.hs.engaged())
        self.f.hs.restore(self.f.commit, params, self.f.sign(contain.RESTORE_ACTION, params))
        self.assertFalse(self.f.hs.engaged())

    def test_engaging_twice_is_harmless_and_one_restore_clears_it(self):
        self.f.hs.engage("a")
        self.f.hs.engage("b")
        params = {"scope": "hard_stop", "reason": "reviewed"}
        self.f.hs.restore(self.f.commit, params, self.f.sign(contain.RESTORE_ACTION, params))
        self.assertFalse(self.f.hs.engaged())

    def test_one_commitment_starts_exactly_one_job(self):
        """Attack: replay a minted commitment for many jobs inside its expiry."""
        c = self.f.minted()
        self.f.k.run("first", OKJOB, req(), c, timeout=10)
        with self.assertRaises(contain.ContainmentRefused) as cm:
            self.f.k.run("second", OKJOB, req(), c, timeout=10)
        self.assertIn("one commitment, one job", str(cm.exception))

    def test_an_unminted_forged_commitment_is_refused(self):
        forged = {"kind": "WCOMMIT", "device": fx.DEV, "surface": fx.SURF, "action": "run_job", "action_hash": "f" * 64, "expiry": 9999999999, "nonce_sha256": "e" * 64}
        with self.assertRaises(contain.ContainmentRefused):
            self.f.k.run("forged", OKJOB, req(), forged, timeout=10)

    def test_garbage_requests_fail_closed_and_start_nothing(self):
        before = len(fx.kinds(self.f.dag, "KRONOS_LOCATE"))
        for bad in (None, {}, {"prefix_hash": 1}, "text", 7, {"data": "x", "prefix_hash": "0" * 64}, {"a": {"b": [1] * 1000}}):
            with self.assertRaises(Exception):
                self.f.k.run("bad", OKJOB, bad, self.f.minted(), timeout=5)
        self.assertEqual(len(fx.kinds(self.f.dag, "KRONOS_LOCATE")), before)

    def test_argv_is_never_run_through_a_shell(self):
        """Attack: metacharacters in an argument must stay a literal string."""
        evil = "; echo PWNED & whoami | cat"
        res = self.f.k.run("meta", [fx.PY, "-I", "-c", "import sys; print(sys.argv[1])", evil], req(), self.f.minted(), timeout=10)
        self.assertEqual(cap.data_of(res).decode().strip(), evil)

    def test_a_tampered_record_makes_the_stop_fail_closed(self):
        """Attack: damage the DAG file so the stop state cannot be read. An unreadable record must count as engaged."""
        class Broken:
            class content:
                @staticmethod
                def entries():
                    raise OSError("unreadable")
        hs = contain.HardStop(self.f.tmp / "NO_SUCH_MARKER", Broken())
        self.assertTrue(hs.engaged())

    def test_unicode_and_odd_job_ids_do_not_break_the_record(self):
        for jid in ("job-é-✓", "../../x", "a b c", "j" * 400):
            res = self.f.k.run(jid, OKJOB, req(), self.f.minted(), timeout=10)
            self.assertEqual(cap.data_of(res).decode().strip(), "ok")
        self.assertTrue(self.f.dag.verify()[0])

    def test_the_record_stays_valid_after_every_attack_in_this_file(self):
        self.f.hs.engage("x")
        self.assertTrue(self.f.dag.verify()[0])


class JobBinding(unittest.TestCase):
    """A commitment must be for the action run_job (always) and, by default, for exactly this job id, program and request."""

    def test_a_signature_for_another_action_never_starts_a_job_even_in_loose_mode(self):
        f = fx.build(bind_job=False)
        for action in ("adapter_union", "conductor_step:publish", "release_public", "spend_funds"):
            with self.assertRaises(contain.ContainmentRefused, msg=action):
                f.k.run("j", OKJOB, req(), f.minted(action), timeout=10)
        self.assertEqual([e for e in f.dag.content.entries() if e["kind"] == "KRONOS_IDENTIFY"], [])

    def test_a_job_commitment_runs_only_the_job_it_was_signed_for(self):
        f = fx.build(bind_job=True)
        r = req()
        other_argv = [fx.PY, "-I", "-c", "print('something else')"]
        c = f.minted("run_job", contain.job_params("job-a", OKJOB, r))
        with self.assertRaises(contain.ContainmentRefused):                             # same signature, a different program
            f.k.run("job-a", other_argv, r, c, timeout=10)
        with self.assertRaises(contain.ContainmentRefused):                             # same signature, a different job id
            f.k.run("job-b", OKJOB, r, c, timeout=10)
        with self.assertRaises(contain.ContainmentRefused):                             # same signature, a different request capsule
            f.k.run("job-a", OKJOB, req("other payload"), c, timeout=10)
        out = f.k.run("job-a", OKJOB, r, c, timeout=10)                                  # the one thing it authorises
        self.assertEqual(cap.data_of(out).strip(), b"ok")
        with self.assertRaises(contain.ContainmentRefused):                             # and only once
            f.k.run("job-a", OKJOB, r, c, timeout=10)

    def test_an_unbound_commitment_is_refused_in_the_default_mode(self):
        f = fx.build(bind_job=True)
        with self.assertRaises(contain.ContainmentRefused):
            f.k.run("job-a", OKJOB, req(), f.minted(), timeout=10)


if __name__ == "__main__":
    unittest.main()
