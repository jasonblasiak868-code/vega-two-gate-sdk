# Changelog

## 2026-10-20: fixes found by a source read

These were found by reading the source against its own documentation after release, not by the earlier test and review passes. Each fix has a regression test in
`tests/test_findings_20261020.py` that failed before the fix, and each fix was checked by undoing it and confirming a test fails. Behaviour that changed is listed under "Changed".

### Fixed
- **The default reviewer approved everything.** With no reviewer, any output within the size cap and with a clean exit code was released. A strict default now applies (valid text, no control or escape characters, no credential-shaped string, under the cap). Releasing with no review must be asked for by name: `reviewer=contain.NO_REVIEW`.
- **"One commitment, one job" could be raced.** The check and the record were two steps. Two threads sharing one `Containment` could both start a job on one commitment. They now run under one lock.
- **A record failure right after a job started left the program running and its work folder on disk.** The job is now killed and removed, and the call is refused.
- The job's stdin, stdout and stderr pipes were never closed (one set leaked per job). They are now closed.
- A commitment that was not an object, or had a non-numeric expiry, raised a raw error. It is now a clean refusal.
- `_kill_tree` now uses the shared process-tree kill in `vega-custody-sdk`, which finds the process group even after the leader was reaped.

### Changed (read before upgrading)
- Output containing control or terminal-escape characters, invalid UTF-8, or a credential-shaped string is no longer released by default. Pass your own `reviewer`, or `contain.NO_REVIEW` to opt out deliberately.
