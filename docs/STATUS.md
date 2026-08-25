# Status

Compact project tracker. Update at **every accepted git boundary** — this file plus the
commit is the audit trail.

---

## LIVE TRADING: DISABLED

No execution path, market-data feeds, credentials, signing material, or venue connectivity
exist in this repository. `maker5m.safety.LIVE_TRADING_ENABLED` is `False` and is asserted
by `tests/unit/test_project_skeleton.py`.

Unlocking is a **P14** decision, gated on the full Canonical §35 acceptance checklist plus
explicit human authorisation. It is not a configuration knob and must never be flipped by
an agent.

---

## Current position

| | |
|---|---|
| **Current phase** | **P0 — Repository baseline + architecture freeze** |
| Current branch | `bootstrap/phase-0` |
| Baseline commit | `0de1f7c` — pristine import of the supplied strategy sources and figures |
| P0 boundary commit | `acc7ab2` — `chore: establish bot architecture and strategy invariants` |
| Last accepted milestone | P0 complete — architecture, invariants, open items, plan, and skeleton frozen |
| Next milestone | **P1 — fixed-point numeric kernel + exact accounting** (blocked, see below) |
| Remote | none configured — nothing has been pushed |

---

## Blockers

| Blocker | Blocks | Detail |
|---|---|---|
| **O10** — venue quantity/price precision unknown | **P1** | `SHARE_SCALE` / `MONEY_SCALE` are frozen at P1 and cannot change afterwards without invalidating recorded journals. They must be chosen from measured venue behaviour, not a guess. |
| **O04** — the two frozen sources give different DOWN-side grid targets | **P3** | Canonical §12.1 yields `-45` (size `16.37`); Detailed §12 and `d3_grid.png` show `-30` (size `1.37`) for the same worked example. The modular fingerprint passes either way, so it cannot arbitrate. Precedence says Canonical wins; the figure is direct reconstructed evidence. **Needs a user decision or replay evidence.** |
| **O11** — authoritative resolution source unnamed | P10 | Both sources say "prefer on-chain" without naming a source, confirmation depth, or timeout. |

Neither O10 nor O04 blocks P2 (state, events, phase machine), so P2 can proceed in
parallel if desired.

---

## Open strategy items

All eleven are open. Full detail in [`OPEN_ITEMS.md`](OPEN_ITEMS.md).

```text
O01 quote-centre source            OPEN      O07 fee/rebate calibration      OPEN
O02 volatility sigma               OPEN      O08 latency for queue dominance OPEN
O03 base-lot L selection rule      OPEN      O09 spot-to-CLOB timing model   OPEN
O04 grid-target selection          OPEN *    O10 venue precision / scales    OPEN *
O05 endgame tilt magnitude         FITTED    O11 resolution source           OPEN
O06 endgame gate magnitude         FITTED

* blocking
```

Nothing here may be hard-coded as an assumption (I18).

---

## Tests

| | |
|---|---|
| Status | **green** |
| Suite | 28 passed |
| `ruff check` | clean |
| `ruff format --check` | clean |
| `mypy` (strict) | clean |

Phase-0 coverage is deliberately narrow — there is no behaviour to test yet. What exists
are guards that must never regress:

- frozen strategy sources match their pinned checksums;
- every subpackage imports and documents its responsibility and plane;
- live trading is disabled;
- required project documents exist and are non-empty;
- **Plane 2 purity**: `numeric`, `market`, `strategy`, and `accounting` import no clock,
  no I/O, no networking, and no source of nondeterminism (I20). This guard starts green
  against the empty skeleton and becomes load-bearing at P2/P3.

---

## Verification ladder

Canonical §34. Nothing has been attempted beyond L0 preparation.

```text
L0  arithmetic                 not started  (P1)
L1  historical reconstruction  not started  (P1 gate)
L2  offline replay             not started  (P5)
L3  live paper                 not started  (P13)
L4  minimum-size live          not started  (P14)
```

---

## Update ritual

At each accepted boundary, update: current phase, branch, boundary commit, last accepted
milestone, next milestone, blockers, open-item labels, and test status. If a phase gate was
not actually executed, say so — code existing is not completion
([`DEVELOPMENT_PLAN.md`](DEVELOPMENT_PLAN.md), rule 1).
