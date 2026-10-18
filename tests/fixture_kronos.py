"""Shared fixture for the two-gate red-team passes (not a test file)."""
import pathlib
import sys
import tempfile
import time
import types

from vega_jacket import humanauth
from vega_jacket.commit.commit import Commit
from vega_jacket.gate.entry_gate import EntryGate
from vega_jacket.humanauth import human_signer
from vega_custody.dag.dag import Dag
from vega_custody.ledger.backend_local import LocalBackend
from vega_custody.ledger.ledger import Ledger
from vega_twogate.kronos import contain

PW, DEV, SURF = "correct horse battery", "dev-rt", "surf-rt"
PY = sys.executable


def build(bind_job=False):
    blob, pub = human_signer.generate(PW)
    t = pathlib.Path(tempfile.mkdtemp())
    dag = Dag([t / "c.jsonl"], [t / "l.jsonl"])
    led = Ledger([LocalBackend(t / "led.jsonl")])
    commit = Commit(humanauth.Verifier(pub, DEV, SURF), led, dag)
    hs = contain.HardStop(t / "HARD_STOP", dag, led)
    k = contain.Containment(EntryGate(dag), hs, dag, led, bind_job=bind_job)
    n = [0]

    def minted(action="run_job", params=None):
        n[0] += 1
        c = {"kind": "WCOMMIT", "device": DEV, "surface": SURF, "action": action, "action_hash": humanauth.action_hash(params) if params is not None else f"{n[0]:064x}",
             "expiry": int(time.time()) + 300, "nonce_sha256": f"{n[0] + 900:064x}"}
        led.mint(c)
        return c

    def sign(action, params, lease=300):
        return human_signer.sign(blob, PW, DEV, SURF, action, params, lease)
    return types.SimpleNamespace(dag=dag, led=led, commit=commit, hs=hs, k=k, minted=minted, sign=sign, tmp=t, blob=blob)


def kinds(dag, prefix=""):
    return [e for e in dag.content.entries() if e["kind"].startswith(prefix)]
