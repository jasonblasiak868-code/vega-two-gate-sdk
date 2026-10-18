"""Selftest: exit 0 only if every check passes."""
import hashlib
import os
import pathlib
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from vega_jacket.humanauth import human_signer  # noqa: E402
from vega_jacket import humanauth  # noqa: E402
from vega_custody.capsule import capsule as cap  # noqa: E402
from vega_jacket.commit.commit import Commit  # noqa: E402
from vega_custody.dag.dag import Dag  # noqa: E402
from vega_jacket.gate.entry_gate import EntryGate  # noqa: E402
from vega_twogate.kronos import contain, recover  # noqa: E402
from vega_custody.ledger.backend_local import LocalBackend  # noqa: E402
from vega_custody.ledger.ledger import Ledger  # noqa: E402
from vega_custody.store.store import DirBackend, ThreeSourceStore  # noqa: E402

fails, N = [], 0
PW, DEV, SURF = "correct horse battery", "dev-t", "surf-t"
PY = sys.executable


def check(name, cond):
    global N
    N += 1
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


def raises(exc, fn):
    try:
        fn()
    except exc:
        return True
    return False


blob, pub = human_signer.generate(PW)
t = pathlib.Path(tempfile.mkdtemp())
dag = Dag([t / "c.jsonl"], [t / "l.jsonl"])
led = Ledger([LocalBackend(t / "led.jsonl")])
ver = humanauth.Verifier(pub, DEV, SURF)
commit = Commit(ver, led, dag)
hs = contain.HardStop(t / "HARD_STOP", dag, led)
k = contain.Containment(EntryGate(dag), hs, dag, led, bind_job=False)


def minted(action="run_job", n=[0]):
    n[0] += 1
    c = {"kind": "WCOMMIT", "device": DEV, "surface": SURF, "action": action, "action_hash": f"{n[0]:064x}",
         "expiry": int(time.time()) + 300, "nonce_sha256": f"{n[0] + 500:064x}"}
    led.mint(c)
    return c


req = cap.make("payload", {"source": "user"})
ok_cmd = [PY, "-I", "-c", "import sys; print('echo:' + sys.stdin.read())"]

res = k.run("j1", ok_cmd, req, minted())
check("contained job runs and its output is released", cap.data_of(res).decode().strip() == "echo:payload")
check("result is chained to the request", res["predecessor"] == req["prefix_hash"])
kinds = [e["kind"] for e in dag.content.entries() if e["kind"].startswith("KRONOS_")]
check("identify, locate, review, remove, release all recorded in order",
      kinds == ["KRONOS_IDENTIFY", "KRONOS_LOCATE", "KRONOS_REVIEW", "KRONOS_REMOVE", "KRONOS_RELEASE"])
check("job working directory removed", not any(p.name.startswith("vega_kronos_") and (pathlib.Path(tempfile.gettempdir()) / p.name).exists()
                                               and (pathlib.Path(tempfile.gettempdir()) / p.name).stat().st_mtime > time.time() - 5
                                               for p in pathlib.Path(tempfile.gettempdir()).glob("vega_kronos_*")))

check("no commitment, no run", raises(contain.ContainmentRefused, lambda: k.run(
    "j2", ok_cmd, req, {"kind": "WCOMMIT", "action": "run_job", "action_hash": "a" * 64,
                        "expiry": int(time.time()) + 300, "nonce_sha256": "b" * 64})))
check("expired commitment refused", raises(contain.ContainmentRefused, lambda: k.run(
    "j3", ok_cmd, req, {**minted(), "expiry": 1})))
tampered = dict(req)
tampered["data_b64"] = cap.make("evil")["data_b64"]
check("tampered request refused at the entry gate", raises(Exception, lambda: k.run("j4", ok_cmd, tampered, minted())))

# --- failures contain, remove, and engage the hard stop
check("hung job is killed and engages the hard stop", raises(contain.ContainmentRefused, lambda: k.run(
    "hang", [PY, "-I", "-c", "import time; time.sleep(60)"], req, minted(), timeout=2)) and hs.engaged())
check("nothing runs while the hard stop is engaged", raises(contain.HardStopEngaged, lambda: k.run(
    "j5", ok_cmd, req, minted())))

# --- only the human key lifts it
params = {"scope": "hard_stop", "reason": "reviewed"}
check("restore without a valid human signature refused", raises(contain.ContainmentRefused, lambda: hs.restore(
    commit, params, {"nonce": "n", "expiry": int(time.time()) + 60, "signature": "00"})) and hs.engaged())
wrong = human_signer.sign(blob, PW, DEV, SURF, "release_public", params, 300)
check("signature for a different action cannot restore", raises(contain.ContainmentRefused,
      lambda: hs.restore(commit, params, wrong)) and hs.engaged())
good = human_signer.sign(blob, PW, DEV, SURF, contain.RESTORE_ACTION, params, 300)
hs.restore(commit, params, good)
check("genuine human signature restores", not hs.engaged())
check("same approval cannot restore twice", raises(contain.ContainmentRefused, lambda: hs.restore(commit, params, good)))

# --- review: a hard-flagged output is removed and stops everything
bad_review = lambda out: {"ok": False, "hard": True, "reason": "secret pattern in output"}
check("reviewer can refuse output and engage the stop", raises(contain.ContainmentRefused, lambda: k.run(
    "j6", ok_cmd, req, minted(), reviewer=bad_review)) and hs.engaged())
hs.restore(commit, params, human_signer.sign(blob, PW, DEV, SURF, contain.RESTORE_ACTION, params, 300))
check("failing job exit code engages the hard stop", raises(contain.ContainmentRefused, lambda: k.run(
    "j7", [PY, "-I", "-c", "raise SystemExit(7)"], req, minted())) and hs.engaged())
hs.restore(commit, params, human_signer.sign(blob, PW, DEV, SURF, contain.RESTORE_ACTION, params, 300))
check("oversized output refused", raises(contain.ContainmentRefused, lambda: k.run(
    "j8", [PY, "-I", "-c", "print('x' * 400000)"], req, minted())))
hs.restore(commit, params, human_signer.sign(blob, PW, DEV, SURF, contain.RESTORE_ACTION, params, 300)) if hs.engaged() else None

# --- recover from the three-source store, skipping a corrupted copy
src = {"local": DirBackend(t / "s1"), "external": DirBackend(t / "s2"), "server": DirBackend(t / "s3")}
store = ThreeSourceStore(src, dag)
good_cap = cap.make("known good state", {"source": "user", "license_class": "own"})
saved = store.save(good_cap)
pathlib.Path(src["local"].uri(saved["data_sha256"])).write_bytes(b"corrupted")
rec = recover.recover(store, dag, reason="after containment failure")
check("recovery returns the known-good capsule despite a corrupted source", rec == good_cap)
check("recovery recorded with per-source states", any(
    e["kind"] == "KRONOS_RECOVER" and e["payload"]["source_states"]["local"] == "corrupt" for e in dag.content.entries()))
check("last_good finds the newest verifying save", recover.last_good(store, dag)[1] == good_cap)
check("recovering an unknown digest fails closed", raises(recover.RecoveryFailed, lambda: recover.recover(store, dag, "0" * 64)))

check("DAG intact", dag.verify()[0])
print(f"{N - len(fails)}/{N} passed" if not fails else f"FAILED: {fails}")
sys.exit(1 if fails else 0)
