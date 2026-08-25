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
| **Empirical replication correctness** | The code does what the *target wallet* did. Only replay or live evidence can establish this. |

A green suite is evidence of the first and **no evidence at all** of the second. P4 makes
this sharpest: `decide()` now composes six unresolved or fitted choices at once.

---

## Current position

| | |
|---|---|
| **Current phase** | **P4 — ENDGAME controller + strategy decision engine** |
| P4 implementation gate | **PASSED** |
| P4 empirical replication | **UNPROVEN** — composes O01, O03, O04, O05, O06, O13 at reference/fitted settings |
| Current branch | `feature/p4-endgame-decision` |
| P4 boundary commit | recorded by the immediately following commit on this branch |
| Last accepted milestone | P3 — strategy pricing and grid core (`32703aa`, tip `1e5f549`) |
| Next milestone | **P5 — Deterministic replay engine** |
| `main` | `1e5f549` — fast-forwarded to the accepted P3 HEAD, pushed |
| Remote | `origin` → `https://github.com/bomb707/hedge.git` |

### Branch policy

```text
main                            latest ACCEPTED milestone
feature/<phase>                 active phase development
bootstrap/phase-0               retained P0 boundary
feature/p1-numeric-accounting   retained P1 boundary
feature/p2-market-state-events  retained P2 boundary
feature/p3-strategy-core        retained P3 boundary
```

Nothing merged, rebased, squashed, or force-pushed. `main` advances by fast-forward only.

---

## Blockers

| Blocker | Blocks | Detail |
|---|---|---|
| **O04** — the two sources give different DOWN-side grid targets | P3/P4 empirical correctness | Both readings run through `decide()` and produce different DOWN sizes (`16.37` vs `1.37` at the worked example). The fingerprint provably cannot arbitrate. Closes only on replay evidence. |
| **O12** — BTC spot fixed-point scale unknown | **P6** | No authoritative evidence for external-feed precision. `BtcPrice` is self-describing, so nothing is frozen. |
| **O11** — authoritative resolution source unnamed | P10 | Both sources say "prefer on-chain" without naming a source, depth, or timeout. |

Nothing blocks P5. Replay is in fact the phase that starts making O01/O04/O05/O06/O13
closable.

---

## Open strategy items

Full detail in [`OPEN_ITEMS.md`](OPEN_ITEMS.md). **P4 closed none and added none.**

```text
O01 quote-centre source            OPEN      O08 latency for queue dominance OPEN
O02 volatility sigma               OPEN      O09 spot-to-CLOB timing model   OPEN
O03 base-lot L selection rule      OPEN      O10 venue precision / scales    CLOSED for kernel
O04 grid-target selection          OPEN *    O11 resolution source           OPEN
O05 endgame tilt magnitude         FITTED    O12 BTC spot scale              OPEN *
O06 endgame gate magnitude         FITTED    O13 tick tie-breaking           OPEN
O07 fee/rebate calibration         OPEN

* blocking a specific later phase
```

`endgame_tilt = 30` and `endgame_band = 5` are now live defaults in `StrategyConfig` and
carry `ParameterStatus.FITTED` in every decision record. Being wired in is not evidence.

---

## What P4 delivered

`StrategyEngine.decide(state) -> DecisionResult`, pure and deterministic, composed from the
P1-P3 components.

- **Lifecycle** — PREARM / SETTLING / DONE emit nothing via a fast path that skips centre,
  base-lot, and grid work entirely. QUOTE builds the candidate quote. ENDGAME builds the
  **same** candidate quote and applies its gate on top.
- **No cancels.** P4 emits intent only; desired-none against a live order becomes a CANCEL
  in P7's reconciler (I09).
- **Favourite from the raw centre**, before quantization, by exact rational comparison.
  `centre == 0.50` → DOWN (A1).
- **Endgame gate** with strict inequalities, boundaries tested at one unit either side in
  both favourite directions.
- **One-sided `band_hard`** — the inward side always stays live, and it never touches a
  price (I17, A4).
- **Eligibility by intersection** with a typed `EligibilityReason` enum, feeding Detailed
  §35's `NOT_QUOTING` classification.
- **Economics on every decision** — both rebate views, straight from the P1 ledger.

### A5 is structural, not just tested

The candidate quote is built before the regime is consulted, so no branch exists in which
ENDGAME could resize or reprice. Candidates are recorded in telemetry whether or not they
were emitted, making the invariant checkable from the record.

### No invented economic gate

Canonical §17 says "monitor"; Detailed §28 says "must be visible"; Canonical §32 computes
the settlement edges and **returns them without gating on them**. No threshold rule exists in
either source, so none was invented. Recorded as `ARCHITECTURE_SSOT` §10 A10 and asserted by
a test: a market where both settlement branches are deeply negative still quotes both sides.

### Not present, by design

No `gamma`, no `band_skew`, no skew field of any kind, no flattening path, no SELL / HEDGE /
MERGE / SPLIT / CONVERT intent — `DesiredOrder` has exactly `{outcome, price, size}` and no
side field, because there is no other kind of order. No venue, transport, queue, or timing
data anywhere in the decision record.

### Measured

```text
decide() case                median
QUOTE                        16.6 us
ENDGAME                      20.3 us
non-quoting (fast path)       9.1 us
centre unavailable            9.8 us
```

**Investigated, as the P4 brief requires.** That is ~3.5× the P3 pricing+sizing pass
(~4.7 µs), and the fast path is floored at ~9 µs despite doing almost no work. The cause is
not logic: a frozen+slots dataclass assigns every field through `object.__setattr__`, costing
**~99 ns/field against ~11 ns/field mutable** (measured). The decision record is ~43 field
assignments across four frozen objects, so ~4-5 µs of every call *is* the immutable record
itself. Immutability is a hard requirement (I20, single-owner state), and the brief forbids
weakening validation for benchmark numbers, so nothing was changed. At realistic event rates
(≤100 Hz) 20 µs is under 0.3 % of one core. Formal latency work remains P8.

---

## Tests

| | |
|---|---|
| Status | **green** |
| Suite | 608 passed (472 at the P3 boundary; +136 in P4) |
| `ruff check` | clean |
| `ruff format --check` | clean, 80 files |
| `mypy` (strict) | clean, 73 files — `src/` and `tests/` |

P4 coverage highlights:

- **raw-vs-quantized favourite** — the mandatory `0.504 → quantized 0.50 → favourite UP`
  case, plus its mirror, plus below/at/above a half;
- **gate boundaries** — every boundary at one unit inside, exactly on, and one unit outside,
  for both favourite directions, both at the gate function and through `decide()`;
- **`band_hard`** — one-sided at and beyond the wall in both directions, with a test that the
  wall never changes a price;
- **A5** — candidate prices and sizes identical between QUOTE and ENDGAME across seven
  inventories;
- **economic regression** — `120 UP / $72`, `100 DOWN / $50` reaches decision telemetry as
  `−$2` / `−$22` with inventory `+20`, in every phase;
- **no invented gate** — both branches negative still quotes both sides;
- **configurability** — the engine runs under both O04 policies (and they still diverge
  through `decide()`), all three O13 tick policies (which genuinely change the quoted price
  at a tie), and all three base lots;
- **determinism** — a full-lifecycle stream decided at every event, asserted reproducible as
  a whole trajectory, plus an 18-way config cross-product;
- **structural absences** — no skew field on `StrategyConfig`, no side/action field on
  `DesiredOrder`, at most two intents.

---

## Verification ladder

Canonical §34.

```text
L0  arithmetic                 PASSED   (P1 - full L0 suite green)
L1  historical reconstruction  BLOCKED  (needs target-wallet ledger data, not in repo)
L2  offline replay             not started  (P5)
L3  live paper                 not started  (P13)
L4  minimum-size live          not started  (P14)
```

**L1 remains UNRUN and is not relabelled.** No reconstructed fill-level target-wallet ledger
exists in this repository. It is not passed and it is not waived. The same missing artefact
is what would close O04.

---

## Update ritual

At each accepted boundary, update: current phase, branch, boundary commit, last accepted
milestone, next milestone, blockers, open-item labels, and test status. Record the
implementation gate and the empirical status **separately**. If a gate was not actually
executed, say so — code existing is not completion
([`DEVELOPMENT_PLAN.md`](DEVELOPMENT_PLAN.md), rule 1).
