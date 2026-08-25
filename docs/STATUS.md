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

## Two different kinds of "done"

| | |
|---|---|
| **Implementation gate** | The code does what this phase specified, and it is proven by tests. |
| **Empirical replication correctness** | The code does what the *target wallet* did. Only replay against real data can establish this. |

P5 is where the distinction becomes structural rather than rhetorical: the repository now
contains the machinery that *would* settle the open items, and still contains no data to
settle them with. Every journal here declares `provenance = SYNTHETIC`.

---

## Current position

| | |
|---|---|
| **Current phase** | **P5 — Deterministic replay engine** |
| P5 replay-engine gate | **PASSED** |
| Target-wallet empirical replay | **UNRUN / BLOCKED** — only `SYNTHETIC` journals exist |
| Current branch | `feature/p5-replay` |
| P5 boundary commit | `ab22f3b` — `feat: add deterministic replay engine` |
| Preceding correction | `2ee8c21` — `fix: remove unsourced band-target constraint`, on `fix/p4-unsourced-config-constraint` |
| Last accepted milestone | P4 — endgame decision engine (`41b61af`, tip `15ff368`) |
| Next milestone | **P6 — Polymarket / BTC market-data adapters** |
| `main` | `2ee8c21` — fast-forwarded through P4 and then the correction, pushed |
| Remote | `origin` → `https://github.com/bomb707/hedge.git` |

### Branch policy

```text
main                                latest ACCEPTED milestone
feature/<phase>                     active phase development
fix/<subject>                       accepted corrections, merged forward by fast-forward
bootstrap/phase-0                   retained P0 boundary
feature/p1-numeric-accounting       retained P1 boundary
feature/p2-market-state-events      retained P2 boundary
feature/p3-strategy-core            retained P3 boundary
feature/p4-endgame-decision         retained P4 boundary, unchanged
fix/p4-unsourced-config-constraint  retained correction boundary
```

Nothing merged by merge commit, rebased, squashed, or force-pushed. `main` advances by
fast-forward only.

---

## The P4 correction

`StrategyConfig` required `band_hard > endgame_tilt`. That is a reasonable engineering
relationship under the current defaults, but **it is not a strategy rule**: the frozen sources
treat the endgame gate and the hard band as independent eligibility controls (Canonical §32)
and state no relationship between their magnitudes.

Removed. Positivity validation for both is kept and no replacement relationship was guessed.
Production defaults are unchanged (tilt 30, band 5, band_hard 100). An unusual but explicitly
configured combination may now legitimately suppress both sides — a deterministic strategy
result, not corrupted state — and that is regression-tested with the documented case
(favourite UP, tilt 30, band 5, `band_hard` 20, `I = +20`). The eligibility test that claimed
both sides can never be blocked at once is rescoped to a property *of the default numbers*.

---

## Blockers

| Blocker | Blocks | Detail |
|---|---|---|
| **No reconstructed target-wallet journal** | **L1, L2-empirical, and O04's closure** | The single missing artefact. P5 built the machinery that consumes it. |
| **O12** — BTC spot fixed-point scale unknown | **P6** | No authoritative evidence for external-feed precision. `BtcPrice` is self-describing, so nothing is frozen. |
| **O11** — authoritative resolution source unnamed | P10 | Both sources say "prefer on-chain" without naming a source, depth, or timeout. |

Nothing blocks P6.

---

## Open strategy items

Full detail in [`OPEN_ITEMS.md`](OPEN_ITEMS.md). **P5 closed none and added none.**

```text
O01 quote-centre source            OPEN      O08 latency for queue dominance OPEN
O02 volatility sigma               OPEN      O09 spot-to-CLOB timing model   OPEN
O03 base-lot L selection rule      OPEN      O10 venue precision / scales    CLOSED for kernel
O04 grid-target selection          OPEN      O11 resolution source           OPEN
O05 endgame tilt magnitude         FITTED    O12 BTC spot scale              OPEN * (P6)
O06 endgame gate magnitude         FITTED    O13 tick tie-breaking           OPEN
O07 fee/rebate calibration         OPEN
```

Synthetic replay closes nothing. The sweep runner can now produce a reproducible trajectory
per candidate for O01, O03, O04, O05, O06, and O13 — but choosing between trajectories needs
an empirical objective, and there is none yet. That is why `run_sweep` deliberately returns
no score and no ranking.

---

## What P5 delivered

- **Versioned journal** — schema version 1, checked on decode; a header record plus one
  record per step.
- **Canonical codec** — UTF-8 NDJSON, sorted keys, compact separators, ASCII output, `\n`
  endings. **No floats**: the encoder refuses one. Enums by explicit stable value; no `repr`,
  `pickle`, class paths, or object identities. Optional values always present, explicitly
  `null`.
- **Byte contract** — `decode(encode(j)) == j` and `encode(decode(b)) == b`, byte for byte,
  verified on a 39-step / 58 581-byte journal.
- **Complete config snapshot** — every behaviour-affecting P1-P4 choice, so a replay can
  never depend on what today's defaults are. A test asserts the encoded config covers every
  declared `StrategyConfig` field, so adding a field without a codec entry fails.
- **Provenance** — a required header field. No journal here is anything but `SYNTHETIC`, and
  a test asserts that.
- **Recorder** — runs `reduce_event` (P2) and `StrategyEngine.decide` (P4) **unchanged**, with
  the frozen ordering: reduce the event, *then* decide.
- **Verifier** — compares the **complete** `DecisionResult` after every event and fails at the
  *first* divergence with step index, event id, and ingress ordinal.
- **Sweep runner** — deterministic, non-mutating, and explicitly not a scorer.
- **Architectural guard** — a static test proves `market/`, `accounting/`, `strategy/`, and
  `numeric/` cannot import `maker5m.replay`, and that no `replay_mode` / `is_replay` switch
  exists in any of them.

### Synthetic corpus

39 events spanning all five phases and all six event types: multiple book updates, spot
ticks, fractional partial fills on both sides, order-state events, health events, phase
events, and a timestamp tie broken only by ingress ordinal. The centre crosses `0.5` in both
directions so ENDGAME sees both favourites, including the exact-`0.50` case (DOWN by A1) and
the raw-`0.504`-quantizes-to-`0.50` case (UP). The mandatory accounting example — 120 UP at
$72, 100 DOWN at $50 → `−$2` / `−$22` — is reached exactly through real fills and holds
across steps 22–30.

### O04 divergence, demonstrated

Sweeping the one corpus under both grid policies produces two different decision
trajectories, differing in `candidate_down_size` while `candidate_up_size` always agrees —
exactly the shape O04 describes. Reproducible across repeated runs and across A→B→A ordering.
**This demonstrates the machinery, not which policy is right.**

---

## Tests

| | |
|---|---|
| Status | **green** |
| Suite | 690 passed (608 at the P4 boundary; +82 in P5, including the correction) |
| `ruff check` | clean |
| `ruff format --check` | clean, 93 files |
| `mypy` (strict) | clean, 86 files — `src/` and `tests/` |

Tamper coverage, all fail-closed: changed fill amount, changed ordinal, changed timestamp
(across a phase boundary), tampered recorded decision, tampered telemetry with an identical
order, unknown event tag, unknown enum value, unknown schema version, unsupported centre
component, unsupported base-lot selector, missing field, unexpected extra field, boolean
where an integer belongs, misplaced step index, header that is not a header, invalid JSON,
missing trailing newline, empty input, non-bytes input, invalid UTF-8, and a logically
invalid decoded value. A separate test proves verification fails at the **first** divergence
when two steps are tampered.

---

## Verification ladder

Canonical §34.

```text
L0  arithmetic                 PASSED   (P1 - full L0 suite green)
L1  historical reconstruction  BLOCKED  (needs target-wallet ledger data, not in repo)
L2  offline replay             ENGINE PASSED / EMPIRICAL UNRUN
L3  live paper                 not started  (P13)
L4  minimum-size live          not started  (P14)
```

**L1 remains UNRUN and is not relabelled.**

**L2 is split deliberately.** The replay *engine* is proven: recorded journals round-trip
byte-identically and every recorded decision is reproduced exactly by the production code
path. The *empirical* half of Canonical §34-L2 — reproducing the target wallet's behaviour —
is **UNRUN**, because every journal in this repository is `SYNTHETIC`. Synthetic data proves
the machinery works and proves nothing about the wallet. It is not passed and it is not
waived.

---

## Update ritual

At each accepted boundary, update: current phase, branch, boundary commit, last accepted
milestone, next milestone, blockers, open-item labels, and test status. Record the
implementation gate and the empirical status **separately**. If a gate was not actually
executed, say so — code existing is not completion
([`DEVELOPMENT_PLAN.md`](DEVELOPMENT_PLAN.md), rule 1).
