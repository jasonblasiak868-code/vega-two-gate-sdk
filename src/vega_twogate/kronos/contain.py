"""contain.py - Kronos: contain any model or execution between two gates, with a hard stop.

    identify -> locate -> (entry gate) -> run contained -> review -> (exit gate) -> release
                         any failure:  remove -> hard stop -> recover (recover.py)

  - the job runs as a separate process with a stripped environment, a throwaway working directory, a wall-clock
    timeout and an output cap; on timeout the whole process tree is killed
  - the entry gate admits only verified capsules; the job needs a minted, unexpired commitment
  - the output is reviewed BEFORE it is released; a failed review removes it and can engage the hard stop
  - the hard stop is a one-way ratchet anyone (or anything) can engage and only the human key can lift
    (restore_after_hard_stop is HUMAN_ONLY). While engaged, nothing runs.
HONEST LIMIT: process containment, not an OS sandbox. Run hostile code inside a VM or container."""
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time

from vega_jacket import humanauth  # noqa: E402
from vega_custody.capsule import capsule as capsule_mod  # noqa: E402
from vega_jacket.commit.commit import CommitRefused  # noqa: E402
from vega_custody.dag.dag import DagError  # noqa: E402
from vega_custody.proc import ProcessJob  # noqa: E402

OUTPUT_CAP = 256 * 1024
RESTORE_ACTION = "restore_after_hard_stop"


class ContainmentRefused(Exception):
    pass


class HardStopEngaged(ContainmentRefused):
    pass


class HardStop:
    """File-backed so it survives restarts. engage() is open to everyone; restore() needs the human key.

    A RESTORED record only lifts the stop if it carries a commitment for exactly restore_after_hard_stop that verifies in the ledger and has not lifted an earlier stop. A record anyone appended to the
    chain files by hand (the chain is hash-linked, not signed) does not count. If no ledger was given the stop cannot verify a restore and stays engaged: it fails closed.
    HONEST LIMIT: someone who can write BOTH the chain files and the ledger files can forge both; keep a second ledger replica on a drive or witness the roots outside the machine."""

    def __init__(self, path, dag, ledger=None):
        self.path, self.dag, self.ledger = pathlib.Path(path), dag, ledger

    def _valid_restore(self, entry, used) -> bool:
        c = entry["payload"].get("commitment")
        try:
            if self.ledger is None or not isinstance(c, dict) or c.get("action") != RESTORE_ACTION or c.get("nonce_sha256") in used:
                return False
            if not self.ledger.verify(c):
                return False
            used.add(c.get("nonce_sha256"))
            return True
        except Exception:  # noqa: BLE001 - a restore that cannot be verified does not lift the stop
            return False

    def _dag_engaged(self) -> bool:
        """True if the record's last stop event is an engage with no later VERIFIED restore. Fail closed: an unreadable record counts as engaged."""
        try:
            state, used = False, set()
            for e in self.dag.content.entries():
                if e["kind"] == "HARD_STOP_ENGAGED":
                    state = True
                elif e["kind"] == "HARD_STOP_RESTORED" and self._valid_restore(e, used):
                    state = False
            return state
        except Exception:                          # noqa: BLE001 - any failure to read the record must keep the stop
            return True

    def engaged(self) -> bool:
        """The stop is engaged if the marker file exists OR the record says so. Deleting the file alone does not lift it; only restore() with the human key does."""
        return self.path.exists() or self._dag_engaged()

    def engage(self, reason: str):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text(json.dumps({"reason": reason, "at": time.time()}), encoding="utf-8")
        try:
            self.dag.record("HARD_STOP_ENGAGED", {"reason": reason})
        except DagError:
            pass  # the stop itself must never depend on the record succeeding  # reviewed: the stop is already engaged on disk; the record failing must never block or undo it

    def restore(self, commit, params: dict, signed: dict) -> dict:
        """Lift the stop. Only a human signature for exactly restore_after_hard_stop does it."""
        try:
            out = commit.commit(RESTORE_ACTION, params, signed, route="human", boundary_crossing=True)
        except CommitRefused as exc:
            raise ContainmentRefused(f"restore refused: {exc}") from exc
        if self.ledger is None:
            self.ledger = commit.ledger
        self.dag.record("HARD_STOP_RESTORED", {"leaf": out["receipt"]["leaf"], "commitment": out["commitment"]})
        self.path.unlink(missing_ok=True)
        return out


def _kill_tree(proc, wait: float = 5.0) -> bool:
    """Kill the whole process tree and VERIFY the process is gone. True only if it is. (A kill that is never checked is a hard stop that may not have stopped.)"""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=10)
        else:
            import signal
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass  # fall through to the direct kill; the wait below decides whether it worked  # reviewed: fall through to proc.kill() and the verified wait below; failure here is detected there
    try:
        proc.kill()
    except OSError:
        pass  # reviewed: process may already be gone; the verified wait below decides success
    try:
        proc.wait(timeout=wait)
    except subprocess.TimeoutExpired:
        return False
    return proc.poll() is not None


def _run_bounded(proc, stdin_bytes: bytes, timeout: float, cap: int):
    """Feed stdin and read stdout/stderr WITHOUT buffering more than cap+1 bytes of either. Returns (out, err, status), status in ok | timeout | overflow.
    (communicate() holds all output in memory until the process ends, so a flooding child could exhaust memory before any cap was checked.)"""
    import threading
    bufs = {"out": bytearray(), "err": bytearray()}
    over = threading.Event()

    def reader(stream, key):
        try:
            while True:
                chunk = stream.read1(65536)
                if not chunk:
                    return
                room = cap + 1 - len(bufs[key])
                if room > 0:
                    bufs[key] += chunk[:room]
                if len(bufs[key]) > cap:
                    over.set()
                    return
        except (OSError, ValueError):
            return

    def writer():
        try:
            proc.stdin.write(stdin_bytes)
        except (OSError, ValueError):
            pass  # reviewed: child closed or broke the pipe: nothing to report, readers and timeout still bound the run
        finally:
            try:
                proc.stdin.close()
            except (OSError, ValueError):
                pass  # reviewed: stdin already closed by the child: harmless

    readers = [threading.Thread(target=reader, args=(proc.stdout, "out"), daemon=True), threading.Thread(target=reader, args=(proc.stderr, "err"), daemon=True)]
    for th in readers + [threading.Thread(target=writer, daemon=True)]:
        th.start()
    deadline = time.time() + timeout
    while time.time() < deadline:
        if over.is_set():
            return bytes(bufs["out"]), bytes(bufs["err"]), "overflow"
        if proc.poll() is not None and not any(th.is_alive() for th in readers):
            return bytes(bufs["out"]), bytes(bufs["err"]), "ok"
        time.sleep(0.01)
    return bytes(bufs["out"]), bytes(bufs["err"]), "timeout"


def identify(dag, job_id: str, request: dict, commitment: dict) -> dict:
    return dag.record("KRONOS_IDENTIFY", {"job": job_id, "request": request["prefix_hash"],
                                          "action": commitment["action"], "action_hash": commitment["action_hash"]})


def locate(dag, job_id: str, workdir: str, pid: int, parent: str) -> dict:
    return dag.record("KRONOS_LOCATE", {"job": job_id, "workdir": workdir, "pid": pid}, parents=[parent])


def review(dag, job_id: str, output: bytes, reviewer, parent: str) -> dict:
    """The reviewer must return a dict whose 'ok' is exactly True. A reviewer that crashes, returns something else, or leaves out 'ok' has not approved the output: that is a failed review."""
    if reviewer is None:
        verdict = {"ok": True, "hard": False, "reason": "default review"}
    else:
        try:
            verdict = reviewer(output)
            if not isinstance(verdict, dict) or verdict.get("ok") is not True and verdict.get("ok") is not False:
                verdict = {"ok": False, "hard": False, "reason": "the reviewer did not return a clear ok true or false"}
        except Exception as exc:  # reviewed: a reviewer that fails has not approved anything; the run is refused and cleaned up like any failed review
            verdict = {"ok": False, "hard": False, "reason": f"the reviewer failed: {type(exc).__name__}"}
    if len(output) > OUTPUT_CAP:
        verdict = {"ok": False, "hard": False, "reason": f"output {len(output)} bytes over cap"}
    e = dag.record("KRONOS_REVIEW", {"job": job_id, "output_sha256": hashlib.sha256(output).hexdigest(),
                                     "ok": bool(verdict["ok"]), "reason": str(verdict.get("reason", ""))}, parents=[parent])
    return {**verdict, "entry": e["entry_hash"]}


FILE_HASH_CAP = 64 * 1024 * 1024          # per file; larger files are recorded as skipped, never read whole
TOTAL_HASH_CAP = 512 * 1024 * 1024


def remove(dag, job_id: str, workdir: pathlib.Path, parent: str) -> dict:
    """Record what is being removed (hashes), then remove it, then VERIFY it is gone. Symlinks and special files are recorded as skipped and never followed or read
    (a job could otherwise plant a link to a secret and get its hash into the record). The returned entry carries removed_ok."""
    files, skipped, total = [], [], 0
    for dp, dn, fs in os.walk(workdir, followlinks=False):
        for name in dn + fs:
            p = pathlib.Path(dp) / name
            rel = str(p.relative_to(workdir))
            if p.is_symlink():
                skipped.append({"path": rel, "why": "symlink"})
        for name in fs:
            p = pathlib.Path(dp) / name
            rel = str(p.relative_to(workdir))
            if p.is_symlink():
                continue
            try:
                st = p.lstat()
            except OSError:
                skipped.append({"path": rel, "why": "unreadable"})
                continue
            import stat as _stat
            if not _stat.S_ISREG(st.st_mode):
                skipped.append({"path": rel, "why": "not a regular file"})
            elif st.st_size > FILE_HASH_CAP or total + st.st_size > TOTAL_HASH_CAP:
                skipped.append({"path": rel, "why": "too large to hash (%d bytes)" % st.st_size})
            else:
                h = hashlib.sha256()
                with open(p, "rb") as fh:
                    for chunk in iter(lambda: fh.read(1 << 20), b""):
                        h.update(chunk)
                files.append({"path": rel, "sha256": h.hexdigest()})
                total += st.st_size
    for attempt in range(20):                                    # a just-killed program can hold its folder for a moment: retry briefly, then report honestly
        shutil.rmtree(workdir, ignore_errors=True)
        if not workdir.exists():
            break
        time.sleep(0.1)
    gone = not workdir.exists()
    try:
        e = dag.record("KRONOS_REMOVE", {"job": job_id, "files": files, "skipped": skipped, "removed_ok": gone}, parents=[parent])
    except DagError as exc:                       # the record must never be what crashes a teardown; the caller engages the stop
        return {"entry_hash": parent, "removed_ok": False, "record_failed": type(exc).__name__}
    return {**e, "removed_ok": gone}


RUN_ACTION = "run_job"


def job_params(job_id: str, argv, request: dict) -> dict:
    """The exact arguments a human signs to authorise ONE job: which job, which program and arguments, which request capsule. Use humanauth.action_hash(job_params(...))."""
    return {"job_id": job_id, "argv": list(argv), "request": request["prefix_hash"]}


class Containment:
    def __init__(self, entry_gate, hard_stop: HardStop, dag, ledger, clock=time.time, bind_job: bool = True):
        """bind_job=True (default): the commitment must be for the action 'run_job' AND its action_hash must be the hash of job_params(job_id, argv, request), so a signature
        for one job, or for any other action, cannot start a different job. bind_job=False checks only the action name (legacy loose mode; every job under one signature
        would run whatever program the caller chooses)."""
        self.entry_gate, self.hard_stop, self.dag, self.ledger, self._clock, self.bind_job = entry_gate, hard_stop, dag, ledger, clock, bool(bind_job)

    def _teardown(self, job_id, work, parent) -> bool:
        r = remove(self.dag, job_id, work, parent)
        return bool(r["removed_ok"]) and "record_failed" not in r

    def run(self, job_id: str, argv: list, request: dict, commitment: dict, timeout: float = 10.0,
            reviewer=None) -> dict:
        if self.hard_stop.engaged():
            raise HardStopEngaged("hard stop engaged: nothing runs until a human lifts it")
        self.entry_gate.admit(request)  # raises on a tampered capsule or a card claiming human-only power
        if int(commitment.get("expiry", 0)) <= int(self._clock()) or not self.ledger.verify(commitment):
            raise ContainmentRefused("no minted, unexpired commitment for this job")
        if commitment.get("kind") != "WCOMMIT":
            raise ContainmentRefused("only a human commitment (kind WCOMMIT) can start a job")
        if commitment.get("action") != RUN_ACTION:
            raise ContainmentRefused(f"the commitment is for {commitment.get('action')!r}, not {RUN_ACTION!r}: a signature for another action cannot start a job")
        if self.bind_job and commitment.get("action_hash") != humanauth.action_hash(job_params(job_id, argv, request)):
            raise ContainmentRefused("the commitment does not bind this job id, program and request: sign job_params(job_id, argv, request)")

        if any(e["kind"] == "KRONOS_IDENTIFY" and e["payload"].get("action_hash") == commitment["action_hash"] for e in self.dag.content.entries()):
            raise ContainmentRefused("this commitment was already used for a job: one commitment, one job")
        ident = identify(self.dag, job_id, request, commitment)
        work = pathlib.Path(tempfile.mkdtemp(prefix="vega_kronos_"))
        env = {"PYTHONIOENCODING": "utf-8", "TEMP": str(work), "TMP": str(work)}
        if os.environ.get("SYSTEMROOT"):
            env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
        try:
            job = ProcessJob.start(argv, cwd=work, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except (OSError, ValueError) as exc:                  # never run a program that could not be put inside a job: clean up and refuse
            gone = self._teardown(job_id, work, ident["entry_hash"])
            raise ContainmentRefused(f"{job_id}: the program could not be started inside a containment job ({type(exc).__name__}: {exc}); nothing ran" + ("" if gone else "; work folder NOT removed")) from exc
        proc = job.proc
        try:
            loc = locate(self.dag, job_id, str(work), proc.pid, ident["entry_hash"])
            stdin = capsule_mod.data_of(request)
            stdin = stdin.encode("utf-8") if isinstance(stdin, str) else stdin
            out, err, status = _run_bounded(proc, stdin, timeout, OUTPUT_CAP)
            if status in ("timeout", "overflow"):
                dead = job.kill()                                 # the whole family, verified: a descendant that detached is still in the job
                gone = self._teardown(job_id, work, loc["entry_hash"])
                if status == "timeout" or not dead or not gone:
                    self.hard_stop.engage(f"{job_id}: {status}" + ("" if dead else "; KILL NOT CONFIRMED") + ("" if gone else "; WORKDIR NOT REMOVED"))
                if not dead:
                    raise ContainmentRefused(f"{job_id}: {status}; the process could NOT be confirmed dead, hard stop engaged")
                raise ContainmentRefused(f"{job_id}: " + ("timed out; process tree killed, hard stop engaged" if status == "timeout" else "output over the cap; process killed, nothing released"))
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                dead = job.kill()
                gone = self._teardown(job_id, work, loc["entry_hash"])
                self.hard_stop.engage(f"{job_id}: the program closed its output but did not exit" + ("" if dead else "; KILL NOT CONFIRMED") + ("" if gone else "; WORKDIR NOT REMOVED"))
                raise ContainmentRefused(f"{job_id}: the program did not exit; hard stop engaged") from None
            left = job.survivors()
            if left != 0:                                          # something the job started is still running after the job itself finished: that is an escape attempt
                dead = job.kill()
                gone = self._teardown(job_id, work, loc["entry_hash"])
                self.hard_stop.engage(f"{job_id}: {left} process(es) outlived the job" + ("" if dead else "; KILL NOT CONFIRMED") + ("" if gone else "; WORKDIR NOT REMOVED"))
                raise ContainmentRefused(f"{job_id}: a descendant outlived the job; killed, output not released, hard stop engaged")
        finally:
            job.kill()                                             # idempotent; on every exit path nothing of the job is left running
            job.close()

        rv = review(self.dag, job_id, out, reviewer, loc["entry_hash"])
        if proc.returncode != 0 or not rv["ok"]:
            reason = rv.get("reason") or f"exit code {proc.returncode}"
            gone = self._teardown(job_id, work, rv["entry"])
            if rv.get("hard") or proc.returncode != 0 or not gone:
                self.hard_stop.engage(f"{job_id}: {reason}" + ("" if gone else "; WORKDIR NOT REMOVED"))
            raise ContainmentRefused(f"{job_id}: output not released ({reason})")

        if not self._teardown(job_id, work, rv["entry"]):
            self.hard_stop.engage(f"{job_id}: WORKDIR NOT REMOVED after a successful run")
            raise ContainmentRefused(f"{job_id}: working directory could not be removed; output not released, hard stop engaged")
        result = capsule_mod.make(out, {"job": job_id, "direction": "exit"}, predecessor=request["prefix_hash"])
        self.dag.record("KRONOS_RELEASE", {"job": job_id, "response": result["prefix_hash"]}, parents=[rv["entry"]])
        return result
