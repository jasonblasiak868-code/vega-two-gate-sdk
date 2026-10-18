"""recover.py - after containment removes something, recovery restores the last KNOWN-GOOD state from the
three-source store. A copy qualifies only if its bytes re-hash to the recorded digest; a corrupted source is
skipped, never trusted. Every recovery is recorded."""
import os
import sys

from vega_custody.store.store import StoreRefused  # noqa: E402


class RecoveryFailed(Exception):
    pass


def last_good(store, dag):
    """Newest SAVE whose bytes verify in at least one source. Returns (save_entry, capsule) or raises."""
    saves = [e for e in dag.content.entries() if e["kind"] == "SAVE"]
    for e in reversed(saves):
        digest = e["payload"]["data_sha256"]
        try:
            return e, store.load(digest)
        except StoreRefused:
            continue
    raise RecoveryFailed("no saved state verifies in any source")


def recover(store, dag, digest: str = None, reason: str = "") -> dict:
    """Restore a specific saved capsule (by digest), or the newest known-good one if none is given."""
    if digest is None:
        entry, capsule = last_good(store, dag)
        digest = entry["payload"]["data_sha256"]
    else:
        try:
            capsule = store.load(digest)
        except StoreRefused as exc:
            raise RecoveryFailed(str(exc)) from exc
    states = store.audit(digest)
    dag.record("KRONOS_RECOVER", {"data_sha256": digest, "capsule_prefix_hash": capsule["prefix_hash"],
                                  "source_states": states, "reason": reason})
    return capsule
