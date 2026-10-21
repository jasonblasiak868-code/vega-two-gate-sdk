# vega-two-gate-sdk — Specification

Spec v1.0 — 2026-10-19 — describes the published reference implementation as tested; where this document and the source disagree, the source wins.

## 1. Purpose and scope

This specification describes the two-gate containment kit: how a job is admitted, run bounded, reviewed, released or removed, how the hard stop engages and lifts, and how removed state is recovered. It specifies only what the published code in `src/vega_twogate/` does, as exercised by the files in `tests/`.

Out of scope: the internals of the dependency kits (capsule format, DAG hash-linking, ledger minting and verification, signature schemes, entry-gate admission rules, bounded-process job objects) — those are specified by vega-custody-sdk and vega-governance-jacket-sdk.
An operating-system-level sandbox is DESIGNED ONLY per STATUS.md and is not part of this specification.
Nothing here is legal advice; the licence text is a draft that has not been reviewed by an attorney.

## 2. Vocabulary

Terms as the code uses them (definitions simplified from the docstrings):

- **Gate.** A checkpoint that refuses by default. The *entry gate* (from vega-governance-jacket-sdk) admits only verified request capsules; the *exit gate* is the review step in this kit: output is reviewed before it is released, and a failed review removes it.
- **Containment corridor.** The path `identify -> locate -> (entry gate) -> run contained -> review -> (exit gate) -> release`, with `remove -> hard stop -> recover` as the failure path (the pipeline printed at the top of `contain.py`).
- **Request capsule.** The input, as a capsule from vega-custody-sdk: content plus metadata with a `prefix_hash` fingerprint. Its data becomes the job's stdin.
- **Commitment.** A human-signed note for one exact action (`kind` `WCOMMIT`), minted in the ledger, with an `action`, an `action_hash`, an `expiry` and a `nonce_sha256`. For a job it must be for the action `run_job`.
- **Job binding.** With `bind_job=True` (the default), the commitment's `action_hash` must equal `humanauth.action_hash(job_params(job_id, argv, request))`, so one signature authorises exactly one job id, program, arguments and request. `bind_job=False` is a legacy loose mode that checks only the action name.
- **Single-use.** A commitment whose `action_hash` already appears in a `KRONOS_IDENTIFY` record is refused: one commitment, one job. The check and the record happen under one lock, so two concurrent runs cannot both start on one commitment.
- **Contained run.** The job runs as a separate process (a `ProcessJob` from vega-custody-sdk) with a stripped environment, a throwaway working directory, a wall-clock timeout and an output cap.
- **Output cap.** `OUTPUT_CAP` = 256 KiB. Readers never buffer more than cap+1 bytes per stream; exceeding it ends the run with status `overflow`.
- **Reviewer.** A callable that receives the output bytes and returns a verdict dict. Only `{"ok": True, ...}` releases. A reviewer that crashes, or returns anything unclear, has *not* approved: that is a failed review. With no reviewer, the strict default (`strict_reviewer`) applies; `reviewer=contain.NO_REVIEW` releases without review and the record says `released WITHOUT review`.
- **Hard stop.** A one-way ratchet: anyone or anything can engage it; while engaged nothing runs; only a human key can lift it (the restore action `restore_after_hard_stop` is HUMAN_ONLY). It is file-backed (a marker file) *and* record-backed (DAG entries), so it survives restarts and marker deletion.
- **Restore.** Lifting the hard stop. A `HARD_STOP_RESTORED` record lifts the stop only if it carries a commitment for exactly `restore_after_hard_stop` that verifies in the ledger and whose nonce has not already lifted an earlier stop.
- **Record (DAG).** The append-only, hash-linked chain from vega-custody-sdk. This kit writes record kinds `KRONOS_IDENTIFY`, `KRONOS_LOCATE`, `KRONOS_REVIEW`, `KRONOS_REMOVE`, `KRONOS_RELEASE`, `KRONOS_RECOVER`, `HARD_STOP_ENGAGED`, `HARD_STOP_RESTORED`.
- **Ledger.** The hash-chained commitment ledger from vega-custody-sdk; `ledger.verify(commitment)` decides whether a commitment was genuinely minted.
- **Three-source store.** The custody store that keeps each saved capsule in three sources; a copy qualifies only if its bytes re-hash to the recorded digest.
- **Known-good state.** The newest `SAVE` record whose capsule still verifies in at least one store source.
- **Survivors.** Processes a job started that are still running after the job itself finished — treated as an escape attempt.

## 3. Components

### 3.1 `vega_twogate/__init__.py`

- **Role.** Package root.
- **Public interface.** `__version__` ("0.1.0") and the package docstring only.
- **Behavior / invariants.** No logic.

### 3.2 `vega_twogate/kronos/__init__.py`

- **Role.** Namespace for the `contain` and `recover` modules. Exports nothing itself; callers import `from vega_twogate.kronos import contain, recover`.

### 3.3 `vega_twogate/kronos/contain.py`

- **Role.** Contain any model or execution between two gates, with a hard stop.

- **Public interface.**
  - Exceptions: `ContainmentRefused`, `HardStopEngaged` (a subclass of `ContainmentRefused`).
  - `HardStop(path, dag, ledger=None)` with `engaged()`, `engage(reason)`, `restore(commit, params, signed)`.
  - Functions: `identify(dag, job_id, request, commitment)`, `locate(dag, job_id, workdir, pid, parent)`, `review(dag, job_id, output, reviewer, parent)`, `remove(dag, job_id, workdir, parent)`, `job_params(job_id, argv, request)`, `_kill_tree(proc, wait=5.0)`, `_run_bounded(proc, stdin_bytes, timeout, cap)`.
  - `Containment(entry_gate, hard_stop, dag, ledger, clock=time.time, bind_job=True)` with `run(job_id, argv, request, commitment, timeout=10.0, reviewer=None)`.
  - Constants: `OUTPUT_CAP` (256 KiB), `RESTORE_ACTION` ("restore_after_hard_stop"), `RUN_ACTION` ("run_job"), `FILE_HASH_CAP` (64 MiB per file), `TOTAL_HASH_CAP` (512 MiB).

- **Behavior on success.** `Containment.run` records `KRONOS_IDENTIFY` and `KRONOS_LOCATE`, starts the job in a fresh throwaway directory with a stripped environment (`PYTHONIOENCODING`, `TEMP`, `TMP`, plus `SYSTEMROOT` on Windows only), feeds it the request capsule's data on stdin, reads stdout/stderr under the cap, waits for a clean exit, checks for survivors, records `KRONOS_REVIEW` (with the output's sha256 and the verdict), removes the working directory and verifies it is gone (`KRONOS_REMOVE` with `removed_ok`), then releases the output as a capsule chained to the request (`predecessor` = the request's `prefix_hash`) and records `KRONOS_RELEASE`. The argv is never run through a shell.

- **Behavior on failure (fail-closed).** `run` refuses — raising `ContainmentRefused` — if: the hard stop is engaged (raises `HardStopEngaged`); the entry gate refuses the request; the commitment is unminted, expired, not `WCOMMIT`, not for `run_job`, or (with `bind_job=True`) not bound to exactly this job; or the commitment was already used. If the program cannot be started inside a containment job, nothing ran: the work folder is removed and the run refused. On timeout or output overflow the process tree is killed (verified), the workdir removed, and the hard stop engaged (for timeout always; for overflow when the kill was not confirmed or removal failed — and in all such cases the output is never released). A program that closes its output but does not exit, or leaves survivors, is killed, torn down, and engages the hard stop. A non-zero exit code or a failed review removes the output; the stop engages when the verdict is `hard`, the exit code was non-zero, or the workdir could not be removed. A workdir that cannot be removed even after a successful run engages the stop and withholds the output. Every refusal still leaves a complete record.

- **HardStop behavior.** `engaged()` is true if the marker file exists *or* the record's last stop event is an engage with no later verified restore; an unreadable record counts as engaged (fail closed), and with no ledger no restore can verify, so the stop stays engaged. `engage(reason)` is open to everyone, writes the marker file, and records `HARD_STOP_ENGAGED`; a record failure never blocks or undoes the stop (it is already engaged on disk). `restore(commit, params, signed)` commits the action `restore_after_hard_stop` through the jacket commit path with `route="human"` and `boundary_crossing=True`; a refused commit raises `ContainmentRefused` and the stop stays engaged. A successful restore records `HARD_STOP_RESTORED` (with the receipt leaf and the commitment) and deletes the marker. Replay is excluded: a restore commitment whose nonce already lifted a stop does not lift another, and a hand-appended `HARD_STOP_RESTORED` record without a ledger-verifiable commitment does not count — the chain is hash-linked, not signed.

- **`_kill_tree` / `_run_bounded`.** `_kill_tree` kills the whole tree (`taskkill /F /T` on Windows, process-group SIGKILL elsewhere), then `proc.kill()`, then *verifies* the process is gone: it returns True only if a bounded wait confirms death. An unverified kill is treated as failure, never as success. `_run_bounded` streams stdin and reads both output streams in threads, never buffering more than cap+1 bytes of either (so a flooding child cannot exhaust memory before the cap bites), and returns status `ok`, `timeout`, or `overflow`.

- **`remove`.** Hashes every regular file in the workdir first (sha256, streamed in 1 MiB chunks), recording symlinks, unreadable entries, non-regular files, and files over the caps as *skipped* — never followed, never read whole (a job could otherwise plant a link to a secret and get its hash into the record). Removal is retried up to 20 times at 0.1 s (a just-killed program can hold its folder briefly) and the result is recorded honestly as `removed_ok`. A DAG failure during the teardown record must never crash the teardown: it returns `removed_ok: False` with `record_failed`, and the caller engages the stop.

- **Invariants.** One commitment starts at most one job. Nothing runs while the hard stop is engaged. Only a human signature for exactly `restore_after_hard_stop` lifts the stop, at most once per signature. Output is released only after a passing review *and* a verified removal of the workdir. Every stage of every run — success or failure — is recorded in the DAG in order.

### 3.4 `vega_twogate/kronos/recover.py`

- **Role.** After containment removes something, restore the last known-good state from the three-source store.

- **Public interface.** `RecoveryFailed` (exception), `last_good(store, dag)`, `recover(store, dag, digest=None, reason="")`.

- **Behavior.** `last_good` scans `SAVE` records newest-first and returns the first whose capsule the store can load — the store only returns bytes that re-hash to the recorded digest, so a corrupted source is skipped, never trusted. If no save verifies in any source it raises `RecoveryFailed` (fail closed). `recover` restores a specific digest, or the newest known-good one when no digest is given; an unknown or unverifiable digest raises `RecoveryFailed`. Every recovery is recorded as `KRONOS_RECOVER` with the digest, the capsule's `prefix_hash`, the per-source states from `store.audit(digest)`, and the reason.

- **Invariants.** Recovery never returns unverified bytes. A corrupted source never blocks recovery while another source verifies. Every recovery leaves a record.

## 4. Guarantees and non-guarantees

### Guarantees (as demonstrated)

| Guarantee | Module | Test |
|---|---|---|
| A run needs a minted, unexpired, human-kind, `run_job` commitment; with `bind_job=True` it must bind exactly this job id, program and request | contain.py `Containment.run` | test_kronos.py; redteam_2 (`JobBinding`, forgery) |
| One commitment starts exactly one job (replay refused) | contain.py (identify scan) | redteam_2 (`test_one_commitment_starts_exactly_one_job`) |
| A tampered request capsule is refused at the entry gate | contain.py + jacket EntryGate | test_kronos.py |
| Nothing runs while the hard stop is engaged | contain.py `HardStop.engaged` | test_kronos.py; redteam_2 |
| Deleting the marker file does not lift the stop | contain.py (record-backed state) | redteam_2 |
| Forged or replayed restore records do not lift the stop; no ledger means it stays engaged | contain.py `_valid_restore`, `_dag_engaged` | redteam_3 (`F1_ForgedRestore`) |
| Only a genuine human signature for `restore_after_hard_stop` lifts the stop, once | contain.py `HardStop.restore` | test_kronos.py; redteam_2 |
| An unreadable record counts as engaged (fail closed) | contain.py `_dag_engaged` | redteam_2 (`test_a_tampered_record_makes_the_stop_fail_closed`) |
| Output over the cap is cut off without buffering it all, and is never released | contain.py `_run_bounded`, `review` | test_kronos.py; redteam_1 (flood) |
| A hung job's whole tree is killed, the kill verified, and the stop engaged | contain.py + custody ProcessJob | test_kronos.py; redteam_1; redteam_3 (`F2`, Windows) |
| A descendant that outlives the job is found, killed, output withheld, stop engaged | contain.py (survivors check) | redteam_3 (`F2_DescendantsOutliveTheJob`, Windows) |
| A reviewer that crashes or answers loosely refuses the output and cleans up | contain.py `review` | redteam_3 (`F3_ReviewerFailures`) |
| Symlinks are never followed or hashed during teardown | contain.py `remove` | redteam_1 (symlink canary) |
| A workdir that cannot be removed engages the stop and withholds output | contain.py `remove`/`run` | redteam_1 (residue) |
| The child does not inherit the parent's environment secrets; argv never goes through a shell | contain.py `run` | redteam_1 (env canary); redteam_2 (metacharacters) |
| A program that cannot start leaves no workdir and runs nothing | contain.py `run` | redteam_3 (`F4_StartFailures`) |
| Recovery returns the newest verifying save despite a corrupted source, and is recorded | recover.py | test_kronos.py (recovery checks) |
| Released output is a capsule chained to the request; the DAG verifies after every attack | contain.py `run` | test_kronos.py; redteam_2 |

### Non-guarantees

| Not guaranteed | Why |
|---|---|
| OS-level isolation | Process containment only; the source docstring says run hostile code inside a VM or container. STATUS.md: sandbox is DESIGNED ONLY. |
| Containment of a detached descendant on POSIX | The red-team suite skips that case off Windows and names a cgroup or container as the answer. |
| Unforgeable stop state on a single compromised disk | Whoever can write both the chain files and the ledger files can forge both; the source recommends a second replica or off-machine witnessing. |
| Meaningful output review by default | The strict default (`strict_reviewer`) checks form only: valid text, no control or escape characters, no credential-shaped strings, under the cap. It does not judge meaning. |
| Safety of the program's side effects outside the workdir | Containment bounds the process, its environment, its runtime and its output — not what an authorised program legitimately does. |
| Production hardening or cryptographic strength | Reference implementation; not independently security-reviewed; crypto not expert-reviewed. |

## 5. Failure modes

From the tests (file names cited):

- **Tampered request capsule** — entry gate refuses before anything starts (test_kronos.py).
- **Missing / unminted / forged commitment** — `ledger.verify` fails; refused (test_kronos.py; redteam_2).
- **Expired commitment** — refused (test_kronos.py).
- **Commitment for another action** — refused even in loose mode; no `KRONOS_IDENTIFY` is written (redteam_2 `JobBinding`).
- **Commitment replay** — second job under the same `action_hash` refused: one commitment, one job (redteam_2).
- **Commitment not bound to this job** — different program, job id, or request under a bound signature: refused (redteam_2 `JobBinding`).
- **Garbage request objects** — fail closed, start nothing, record nothing (redteam_2).
- **Hung job** — timeout, verified tree kill, teardown, hard stop engaged (test_kronos.py; redteam_1).
- **Output flood** — cut off at the cap without buffering it all; nothing released; workdir removed (redteam_1).
- **Kill that cannot be confirmed** — reported in the refusal message and the stop engages (redteam_1).
- **Detached descendants / process-count abuse** — survivors detected, killed, output withheld, stop engaged; count capped by the job object on Windows (redteam_3 `F2`).
- **Residue: workdir will not delete** — `removed_ok: False`, stop engaged, output withheld (redteam_1).
- **Symlink planted to a secret** — skipped, never followed or hashed; target untouched (redteam_1).
- **Reviewer crash / loose verdict** — treated as a failed review; output removed; workdir cleaned (redteam_3 `F3`).
- **Hard-flagged review or non-zero exit code** — output removed, stop engaged (test_kronos.py).
- **Marker file deleted / record corrupted / forged or replayed restore records** — stop stays engaged; fails closed (redteam_2; redteam_3 `F1`).
- **Corrupt store source during recovery** — skipped; next verifying source or older save used; per-source states recorded; if nothing verifies, `RecoveryFailed` (test_kronos.py).
- **Record write fails during engage or teardown** — never blocks or undoes the stop; teardown reports `record_failed` and the caller engages the stop (contain.py; REVIEW.md info findings).

## 6. Dependencies and layer rules

Registry entry (verbatim): layer **4 contain**, owns *"containment between two gates (Kronos) and the hard stop"*, requires **[vega-custody-sdk, vega-governance-jacket-sdk]**, status **BUILT**.

Imports actually used: from vega-governance-jacket-sdk — `vega_jacket.humanauth` (action hashing), `vega_jacket.commit.commit` (`Commit`, `CommitRefused`), `vega_jacket.gate.entry_gate` (`EntryGate`, in tests/fixtures); from vega-custody-sdk — `vega_custody.capsule.capsule`, `vega_custody.dag.dag` (`Dag`, `DagError`), `vega_custody.proc` (`ProcessJob`), `vega_custody.store.store` (`StoreRefused`; `ThreeSourceStore`, `DirBackend` in tests), `vega_custody.ledger` (in tests). No network-capable modules are imported (REVIEW.md).

Dependencies point down layers only, never up or sideways: this kit (layer 4) uses layer 0 (record) and layer 1 (authority), and nothing else. It owns no capsule format, no ledger, no signature scheme and no gate admission logic of its own — it calls the owners.

## 7. Conformance

A third party can verify this kit by running `tests/` with the `src` directories of this repo and both dependencies on `PYTHONPATH`: `test_kronos.py` exits 0 only if every check passes; each `redteam_*.py` file is a unittest module that must pass with zero failures and zero errors (Windows-only tests skip elsewhere). `fixture_kronos.py` is a shared fixture, not a test file. STATUS.md and REVIEW.md record 63 tests in 5 files, all passing, and an automated review with 0 high, 0 medium, 0 low findings and 5 reviewed info notes.

What passing means: the behaviors in sections 3–5 hold on the tested platforms, as demonstrated by these tests. What passing does NOT mean: that the kit is secure in general, hardened for production, independently security-reviewed, or safe against an attacker who controls the machine's storage — a clean scan is not proof of safety (REVIEW.md).
