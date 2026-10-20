# vega-two-gate-sdk — Capabilities

A minute inventory of what this kit actually enforces, capability by capability. Every entry names where the enforcement lives in the published code and which test file demonstrates it. The framing question for each: why can a policy-based harness — one that relies on prompts, configuration or a model checking itself — not offer this? The structural answer is the same every time and is stated once here, then applied: **authority held outside the model cannot be replicated by prompt-level policy.** A policy is text the model can read around, misinterpret, or be steered against; a gate that refuses without a human signature does not depend on the model behaving. Claims below are about what THIS design does, never about any third party's product.

Everything listed is BUILT per STATUS.md. The operating-system-level sandbox is DESIGNED ONLY and appears nowhere below as a capability.

---

## 1. Single-use, job-bound human commitment to run anything

- **Capability.** No job starts without a commitment that is minted in the ledger, unexpired, of kind `WCOMMIT`, for the action `run_job`, and — in the default `bind_job=True` mode — whose `action_hash` equals the hash of exactly `job_params(job_id, argv, request)`. One commitment starts exactly one job; a replay is refused by scanning prior `KRONOS_IDENTIFY` records.
- **Why policy cannot provide it.** A prompt-level rule says "only run approved jobs," but the model both reads the rule and decides whether it applied. Here the decision is arithmetic: a hash comparison against a ledger entry only the human key could have caused to exist. Approval for one job cannot be stretched to another because the job id, program, arguments and request capsule are inside the signed hash.
- **Where enforced.** `src/vega_twogate/kronos/contain.py` — the check block at the top of `Containment.run`, `job_params`, `RUN_ACTION`.
- **Demonstrated by.** `tests/redteam_2_state_and_replay.py` (`JobBinding`: other-action signatures, different program/job/request, unbound commitments, single-use replay; forgery refused) and `tests/test_kronos.py` (no commitment, expired commitment).
- **Honest limit.** The commitment binds what was signed; it cannot judge whether the human signed wisely. Careless approval still starts a job.

## 2. Entry gate on the request capsule

- **Capability.** The request must pass the entry gate (`entry_gate.admit(request)`) before any commitment check or process start: a tampered capsule is refused and nothing runs.
- **Why policy cannot provide it.** Input validation by instruction is a request to the model; the gate is a verification step with no discretion — a capsule whose bytes do not match its fingerprint is refused regardless of what any component believes about it.
- **Where enforced.** `contain.py` — `Containment.run` calls the jacket-owned `EntryGate`; this kit calls the owner instead of growing its own gate.
- **Demonstrated by.** `tests/test_kronos.py` (tampered request refused at the entry gate); `tests/redteam_2_state_and_replay.py` (garbage request objects fail closed and start nothing — no `KRONOS_LOCATE` written).
- **Honest limit.** The gate verifies integrity and admission rules, not intent: a genuinely signed capsule carrying a bad idea still passes.

## 3. Bounded contained run

- **Capability.** The job runs as a separate `ProcessJob` (custody-owned) with a stripped environment (only `PYTHONIOENCODING`, `TEMP`, `TMP`, plus `SYSTEMROOT` on Windows), a fresh throwaway working directory, a wall-clock timeout, and a 256 KiB output cap enforced by streaming readers that never buffer more than cap+1 bytes per stream. Argv is never passed through a shell. Stdin is fed without deadlocking even when the child never reads it.
- **Why policy cannot provide it.** "Do not run too long" and "do not print too much" are unenforceable as instructions to the running program itself. Bounds must be imposed from outside the process, by whoever owns its lifetime — which is what a job object with a verified timeout and capped readers is.
- **Where enforced.** `contain.py` — `Containment.run` (env, workdir, start), `_run_bounded` (cap, timeout, stdin writer), `OUTPUT_CAP`.
- **Demonstrated by.** `tests/redteam_1_containment.py` (output flood cut off before the timeout; env canary not inherited; huge unread stdin does not deadlock), `tests/redteam_2_state_and_replay.py` (shell metacharacters stay literal), `tests/test_kronos.py` (oversized output refused).
- **Honest limit.** Process-level containment, not an OS sandbox — the source says run hostile code inside a VM or container. Resource bounds other than time, output and environment are not imposed by this kit.

## 4. Verified kill and survivor detection

- **Capability.** On timeout, overflow or a job that will not exit, the whole process tree is killed and the kill is *verified* — `_kill_tree` returns True only if a bounded wait confirms the process is gone; an unconfirmed kill is reported in the refusal message and engages the hard stop. After a normal exit, `survivors()` detects descendants that outlived the job; they are killed, the output is withheld and the stop engages. On Windows the job object also caps process counts and catches detached descendants.
- **Why policy cannot provide it.** A kill that is issued but never confirmed is a hope. Nothing a model or a policy says can make a process dead; only checking the process table can. The design treats "I killed it" and "it is confirmed gone" as different statements and never releases output on the first alone.
- **Where enforced.** `contain.py` — `_kill_tree`, the survivors check and the kill-confirmation branches in `Containment.run`; custody's `ProcessJob.kill`/`survivors`.
- **Demonstrated by.** `tests/redteam_1_containment.py` (a kill that cannot be confirmed is not silent; `_kill_tree` returns False for a process that will not die), `tests/redteam_3_audit_findings.py` (`F2_DescendantsOutliveTheJob`, Windows-only: detached descendant found and killed, process count capped).
- **Honest limit.** On POSIX a descendant that detaches from the process group is a documented limit — the test suite skips that case off Windows and names a cgroup or container as the answer.

## 5. Review before release (the exit gate)

- **Capability.** Output is reviewed *before* it is released. Only a verdict dict with `ok` exactly `True` releases; a reviewer that crashes, returns a non-dict, or answers loosely (`"yes"`, `1`, missing `ok`) has not approved — that is a failed review, the output is removed and the workdir cleaned. A `hard: True` verdict engages the hard stop. Output already over the cap fails review unconditionally.
- **Why policy cannot provide it.** A model asked "is this output safe?" answers with the same fallible machinery that produced the output. The gate here is not an opinion channel: ambiguous, crashed or missing verdicts default to *refuse*, so the exit is closed unless approval is explicit and machine-checkable.
- **Where enforced.** `contain.py` — `review`, the release branch of `Containment.run`.
- **Demonstrated by.** `tests/redteam_3_audit_findings.py` (`F3_ReviewerFailures`: six loose/crashing reviewers all refuse and clean up; an explicit ok true still releases), `tests/test_kronos.py` (hard-flagged review removes the output and engages the stop).
- **Honest limit.** The kit ships no reviewer; with `reviewer=None` the recorded verdict is a default ok. Review quality is entirely the caller's responsibility.

## 6. Verified teardown that never follows a planted link

- **Capability.** Removal first records what is being removed: every regular file hashed (streamed, 1 MiB chunks) under per-file and total caps; symlinks, unreadable entries, non-regular files and oversized files recorded as *skipped* and never followed or read. Then the workdir is deleted with up to 20 retries, and the record states honestly whether it is gone (`removed_ok`). A DAG failure during teardown never crashes it — it reports `record_failed` and the caller engages the stop. A workdir that cannot be removed engages the hard stop and withholds the output, even after an otherwise successful run.
- **Why policy cannot provide it.** "Clean up afterwards" is unverifiable as an instruction; and a teardown that reads whatever it finds can be turned into an exfiltration oracle — plant a symlink to a secret and its hash lands in the record. The enforcement is mechanical: never follow links, never read above the caps, verify absence, report truthfully.
- **Where enforced.** `contain.py` — `remove`, `FILE_HASH_CAP`, `TOTAL_HASH_CAP`, `_teardown`, the removal branches of `run`.
- **Demonstrated by.** `tests/redteam_1_containment.py` (symlink canary never followed or hashed, target untouched; residue engages the stop with `removed_ok: False`; files named like secrets hash without crashing the record; workdir confirmed gone after a normal run).
- **Honest limit.** Files a job wrote *outside* the workdir are not hunted down; containment bounds the workdir, not the whole filesystem.

## 7. Hard stop: one-way ratchet, human key only

- **Capability.** Anything — a component, a test, an operator, a failure path — can engage the hard stop. While engaged, `run` raises `HardStopEngaged` before touching anything else. The stop lives in two places at once: a marker file *and* the hash-chained record, so it survives restarts and marker deletion; an unreadable record counts as engaged; with no ledger, no restore can verify and the stop stays engaged. Lifting requires committing the action `restore_after_hard_stop` through the jacket human route with a valid signature; a `HARD_STOP_RESTORED` record lifts the stop only if its commitment verifies in the ledger, is for exactly that action, and its nonce has not already lifted an earlier stop. Hand-appended and replayed restore records do not count — the chain is hash-linked, not signed. Engaging never depends on the record succeeding.
- **Why policy cannot provide it.** A freeze the frozen party can lift is not a freeze. Under prompt-level control the same model that misbehaved would decide when to resume. Here the resume decision requires a signature the model cannot produce, the stop state is duplicated so no single deletion clears it, and every ambiguous state resolves to *stopped*. This is the authority-outside-the-model argument in its sharpest form: engage is open to all, lift is closed to all but one key.
- **Where enforced.** `contain.py` — `HardStop` (`engaged`, `engage`, `restore`, `_valid_restore`, `_dag_engaged`), `RESTORE_ACTION`, the first check in `Containment.run`.
- **Demonstrated by.** `tests/test_kronos.py` (nothing runs while engaged; restore without a valid signature refused; wrong-action signature refused; genuine signature restores; same approval cannot restore twice), `tests/redteam_2_state_and_replay.py` (marker deletion does not lift; tampered record fails closed; double engage harmless), `tests/redteam_3_audit_findings.py` (`F1_ForgedRestore`: forged records, replayed genuine records, no-ledger construction all stay engaged).
- **Honest limit.** Stated in the source: someone who can write *both* the chain files and the ledger files can forge both. Keep a second ledger replica on another drive or witness the roots outside the machine.

## 8. Recovery of the last known-good state

- **Capability.** After containment removes something, `recover.py` restores the newest `SAVE` whose bytes verify — the store returns a copy only if it re-hashes to the recorded digest, so a corrupted source is skipped, never trusted. A specific digest can be requested; an unknown or unverifiable digest raises `RecoveryFailed`. Every recovery is recorded with per-source states and a reason.
- **Why policy cannot provide it.** "Restore from backup" as an instruction trusts whichever copy is handy. The enforcement is digest arithmetic across three sources: a copy that does not re-hash does not exist as far as recovery is concerned, and the audit trail shows which source was corrupt.
- **Where enforced.** `src/vega_twogate/kronos/recover.py` — `last_good`, `recover`; custody's three-source store does the hashing.
- **Demonstrated by.** `tests/test_kronos.py` (recovery returns the known-good capsule despite a corrupted local source; per-source states recorded with `local: corrupt`; `last_good` finds the newest verifying save; unknown digest fails closed).
- **Honest limit.** Recovery restores what was *saved*; state changed after the last save is gone. If no save verifies in any source, recovery refuses rather than guessing.

## 9. Complete record of every corridor step

- **Capability.** Success and failure both leave an ordered, append-only trail: `KRONOS_IDENTIFY`, `KRONOS_LOCATE`, `KRONOS_REVIEW` (with the output's sha256 and the verdict), `KRONOS_REMOVE` (with file hashes, skips and `removed_ok`), `KRONOS_RELEASE`, `HARD_STOP_ENGAGED` / `HARD_STOP_RESTORED`, `KRONOS_RECOVER`. The released capsule is chained to the request capsule. The DAG verifies intact after every attack in the suite.
- **Why policy cannot provide it.** A model's account of what it did is testimony; a hash-linked chain written at each step — including the refusal paths — is evidence a third party can check without trusting the model, the vendor or the project.
- **Where enforced.** `contain.py` (every record call, ordered by `parents=`), `recover.py`, custody's DAG.
- **Demonstrated by.** `tests/test_kronos.py` (record kinds in exact order; result chained to request; DAG intact), `tests/redteam_2_state_and_replay.py` (record stays valid after every attack; unicode and hostile job ids do not break it).
- **Honest limit.** Hash-linking proves sequence and integrity, not authorship — the chain is not signed, which is exactly why the hard stop's restore additionally requires a ledger-verified commitment.

---

## This repo's part in the family predictive loop

The family runs a loop: records -> lessons -> knowledge graph -> lattice foresight -> human decision. This kit owns the point where the loop touches the world. When foresight above predicts a path and a human signs a commitment for one step of it, containment is what makes that step safe to execute: the job runs between two gates, bounded and reviewed, and every outcome — released, refused, killed, removed, stopped, recovered — lands as a record in the append-only chain. Those records are the raw material the layers above distil into lessons and knowledge, and the hard stop is the human's hand on the loop: engage is instant and open to anyone, resume requires the one key the model never holds, and recovery puts the last known-good state back before the next decision is made. Containment does not predict, remember or deliberate; it makes sure that when the loop acts, the action is authorised once, bounded, observed, recorded, and reversible to a verified state — so the human decision at the top of the loop is the one that always closes it.
