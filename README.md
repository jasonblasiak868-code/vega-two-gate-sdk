# vega-two-gate-sdk

![vega-two-gate-sdk](assets/tile.svg)

Two-gate containment (Kronos) with a hard stop only the human key can lift.

> **Licensing.** Source-available, not open source. Free for personal use; free for registered small businesses (under US$1M revenue and valuation); everyone else needs a commercial license (see LICENSE; for a commercial license write to jasonblasiak.ai.mail@gmail.com). Patent pending. Draft licence text, not yet reviewed by an attorney.

Package: `vega_twogate` (src layout). Depends on: vega-custody-sdk, vega-governance-jacket-sdk.

## Modules

| Module | What it does |
|---|---|
| `kronos` | Containment between two gates, a hard stop only the human key can lift, and recovery from the three-source store. |

## What it does NOT do (read this)

- Containment here is process-level; it does not replace a VM or container.

## Status

See STATUS.md. Tests: 52 tests in 4 files, all passing.  See SECURITY.md for the threat model.

## Run the tests

Put `src` of this repo and of the repos it depends on on PYTHONPATH, then run each file in `tests/`.
