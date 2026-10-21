"""Regression tests for defects found by reading the source (2026-10-20): the default reviewer approved everything, 'one commitment, one job' could be raced, and a failure
between starting a job and running it left its work folder behind. Each test failed before its fix."""
import glob
import os
import tempfile
import threading
import unittest

import fixture_kronos as fx
from vega_custody.capsule import capsule as cap
from vega_custody.dag.dag import DagError
from vega_twogate.kronos import contain


def req(data="payload"):
    return cap.make(data, {"source": "user"})


def emit(text_expr):
    return [fx.PY, "-I", "-c", "import sys; sys.stdout.buffer.write(%s)" % text_expr]


class DefaultReviewIsStrict(unittest.TestCase):
    def setUp(self):
        self.f = fx.build()

    def run_default(self, argv, name="d"):
        return self.f.k.run(name, argv, req(), self.f.minted(), timeout=15)

    def test_plain_text_is_released_by_default(self):
        out = self.run_default(emit("b'hello world\\n'"))
        self.assertEqual(cap.data_of(out).strip(), b"hello world")

    def test_a_credential_shaped_string_is_not_released_by_default(self):
        for i, secret in enumerate(("sk-" + "a1B2c3D4" * 4, "ghp_" + "Z9y8X7w6" * 5, "AKIA" + "ABCDEFGH" * 2, "-----BEGIN RSA " + "PRIVATE KEY-----")):
            with self.assertRaises(contain.ContainmentRefused) as cm:
                self.run_default(emit("b'%s'" % secret), "cred%d" % i)
            self.assertIn("not released", str(cm.exception))

    def test_binary_and_terminal_escape_output_is_not_released_by_default(self):
        for i, payload in enumerate(("b'\\xff\\xfe\\x00\\x01'", "b'\\x1b[2J\\x1b[31mred'", "b'a\\x00b'", "'\\u202egnp.exe'.encode()")):
            with self.assertRaises(contain.ContainmentRefused):
                self.run_default(emit(payload), "ctl%d" % i)

    def test_invalid_utf8_with_no_control_characters_is_not_released_by_default(self):
        with self.assertRaises(contain.ContainmentRefused):
            self.run_default(emit("b'caf\\xe9 au lait'"), "latin1")          # a latin-1 e-acute: not UTF-8, and nothing else wrong with it

    def test_each_kind_of_control_character_is_refused_on_its_own(self):
        for i, cp in enumerate(("\\x01", "\\x1b", "\\x7f", "\\u0085", "\\u009b", "\\u202e", "\\u2066")):
            with self.assertRaises(contain.ContainmentRefused, msg=cp):
                self.run_default(emit("('ab%s cd').encode()" % cp), "cc%d" % i)

    def test_tab_newline_and_carriage_return_are_fine(self):
        out = self.run_default(emit("b'a\\tb\\r\\nc\\n'"), "ws")
        self.assertEqual(cap.data_of(out), b"a\tb\r\nc\n")

    def test_a_refused_default_review_is_recorded_without_the_secret(self):
        secret = "sk-" + "a1B2c3D4" * 4
        with self.assertRaises(contain.ContainmentRefused):
            self.run_default(emit("b'%s'" % secret))
        rv = fx.kinds(self.f.dag, "KRONOS_REVIEW")[-1]["payload"]
        self.assertFalse(rv["ok"])
        self.assertNotIn(secret, str(rv))

    def test_releasing_without_review_must_be_asked_for_by_name_and_is_recorded_as_such(self):
        out = self.f.k.run("x", emit("b'\\xff\\xfe binary ok'"), req(), self.f.minted(), timeout=15, reviewer=contain.NO_REVIEW)
        self.assertIsNotNone(out)
        rv = fx.kinds(self.f.dag, "KRONOS_REVIEW")[-1]["payload"]
        self.assertTrue(rv["ok"])
        self.assertIn("WITHOUT review", rv["reason"])

    def test_an_explicit_reviewer_still_decides(self):
        with self.assertRaises(contain.ContainmentRefused):
            self.f.k.run("x", emit("b'fine'"), req(), self.f.minted(), timeout=15, reviewer=lambda o: {"ok": False, "reason": "no"})


class OneCommitmentOneJobUnderRace(unittest.TestCase):
    def test_two_threads_with_the_same_commitment_run_exactly_one_job(self):
        f = fx.build()
        c = f.minted()
        results, lock = [], threading.Lock()
        start = threading.Barrier(2)
        argv = [fx.PY, "-I", "-c", "import time; time.sleep(0.6); print('ran')"]

        def go(i):
            start.wait()
            try:
                f.k.run("race%d" % i, argv, req(), c, timeout=20)
                r = "ran"
            except contain.ContainmentRefused as exc:
                r = "refused"
            except Exception as exc:                                             # any other error type is also a defect: a refusal must be a refusal
                r = "error:" + type(exc).__name__
            with lock:
                results.append(r)
        ts = [threading.Thread(target=go, args=(i,)) for i in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(60)
        self.assertEqual(sorted(results), ["ran", "refused"], results)
        self.assertEqual(len([e for e in fx.kinds(f.dag, "KRONOS_IDENTIFY") if e["payload"]["action_hash"] == c["action_hash"]]), 1)


class FailureAfterStartLeavesNothingBehind(unittest.TestCase):
    def test_a_record_failure_right_after_the_job_starts_still_removes_the_work_folder(self):
        f = fx.build()
        before = set(glob.glob(os.path.join(tempfile.gettempdir(), "vega_kronos_*")))
        orig = contain.locate

        def boom(*a, **k):
            raise DagError("simulated record failure")
        contain.locate = boom
        try:
            with self.assertRaises(contain.ContainmentRefused):
                f.k.run("loc", [fx.PY, "-I", "-c", "print('x')"], req(), f.minted(), timeout=10)
        finally:
            contain.locate = orig
        after = set(glob.glob(os.path.join(tempfile.gettempdir(), "vega_kronos_*")))
        self.assertEqual(after - before, set(), "the job's work folder was left on disk")


if __name__ == "__main__":
    unittest.main()
