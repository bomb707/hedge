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
| **Current phase** | **P2 — MarketState + event contracts + phase machine** |
| Current branch | `feature/p2-market-state-events` |
| P2 boundary commit | `fcd8ddf` — `feat: add deterministic market state and event model` |
| Last accepted milestone | P1 — fixed-point numeric kernel + exact accounting (`89f7178`, tip `3966978`) |
| Next milestone | **P3 — Strategy core: Up-space + grid sizing** (blocked on O04, see below) |
| `main` | `3966978` — fast-forwarded to the accepted P1 HEAD, pushed. No history rewritten |
| Remote | `origin` → `https://github.com/bomb707/hedge.git` |

### Branch policy

```text
main                            latest ACCEPTED milestone
feature/<phase>                 active phase development
bootstrap/phase-0               retained P0 boundary
feature/p1-numeric-accounting   retained P1 boundary
```

Nothing has been merged, rebased, squashed, or force-pushed. `main` reached P1 by
fast-forward only.

---

## Blockers

| Blocker | Blocks | Detail |
|---|---|---|
| **O04** — the two frozen sources give different DOWN-side grid targets | **P3** | Canonical §12.1 yields `-45` (size `16.37`); Detailed §12 and `d3_grid.png` show `-30` (size `1.37`) for the same worked example. The modular fingerprint passes either way, so it cannot arbitrate. **Precedence is not empirical proof** — P3 carries both as named policies and claims correctness for neither until replay evidence decides. Untouched by P1 and P2. |
| **O12** — BTC spot fixed-point scale unknown | **P6** | New, raised in P2. No authoritative evidence for external-feed precision exists here. P2 avoided guessing by making `BtcPrice` self-describing; the scale is chosen from observed traffic at P6. |
| **O11** — authoritative resolution source unnamed | P10 | Both sources say "prefer on-chain" without naming a source, confirmation depth, or timeout. |

Nothing blocks P3 from *starting*; O04 blocks P3 from claiming correctness.

---

## Open strategy items

Full detail in [`OPEN_ITEMS.md`](OPEN_ITEMS.md). **P2 resolved none of them.**

```text
O01 quote-centre source            OPEN      O07 fee/rebate calibration      OPEN
O02 volatility sigma               OPEN      O08 latency for queue dominance OPEN
O03 base-lot L selection rule      OPEN      O09 spot-to-CLOB timing model   OPEN
O04 grid-target selection          OPEN *    O10 venue precision / scales    CLOSED for kernel
O05 endgame tilt magnitude         FITTED    O11 resolution source           OPEN
O06 endgame gate magnitude         FITTED    O12 BTC spot scale              OPEN * (new)

* blocking
```

Nothing here may be hard-coded as an assumption (I18). The P1 A9 Term2 correction remains
accepted; O10 remains closed for the numeric kernel with its P6 traffic-check note.

---

## What P2 delivered

- **Time as data.** `TimestampNs` / `DurationNs`, integer nanoseconds. No Plane 2 module
  imports a clock — enforced statically, not by convention.
- **Phase machine.** `PREARM → QUOTE → ENDGAME → SETTLING → DONE`, half-open integer
  boundaries at `T0+3 / +240 / +280 / +300`. **The phase is derived, never stored**, so it
  cannot drift out of agreement with the event stream. `PhaseEvent` journals a boundary and
  is validated against the derived phase.
- **Six normalized event contracts** — `SpotTick`, `BookUpdate`, `OwnFill`,
  `OrderStateEvent`, `PhaseEvent`, `HealthEvent` — all `frozen=True, slots=True`.
- **Explicit ordering.** `ingress_ordinal` is the total order; timestamps are data. Both are
  enforced and fail closed. See `ARCHITECTURE_SSOT.md` §3.4.
- **Fill idempotency.** A repeated fill raises rather than double-accounting (§3.5).
- **`MarketState`** — single-owner, frozen, embedding the accepted P1 `LedgerState`.
- **`MarketSnapshot`** — immutable Plane 3 view, deterministically produced.
- **`BtcPrice`** — exact, float-free, self-describing while O12 is open.

### Architectural correction made during P2

P0 documented `accounting → market`; P1 placed `Outcome` in `market` to honour it. P2 showed
the real direction is `market → accounting` (the event stream carries `Fill`, `MarketState`
embeds `LedgerState`), which produced a genuine import cycle. `Outcome` moved to the leaf
module `maker5m/domain.py`; both packages re-export it, so no call site changed. Recorded in
`ARCHITECTURE_SSOT.md` §8. **No P1 arithmetic was modified.**

### One P1 bug fixed

`parse_fixed_point` raised `ValueError` for `decimals=0` with no fractional part
(`int("")`). Unreachable from P1, where the only scale is 6 decimals; reachable once the
parser became public for `BtcPrice`. Fixed and regression-tested. Behaviour at every
previously-reachable scale is unchanged.

---

## Tests

| | |
|---|---|
| Status | **green** |
| Suite | 325 passed (183 at the P1 boundary; +142 in P2) |
| `ruff check` | clean |
| `ruff format --check` | clean, 57 files |
| `mypy` (strict) | clean, 50 files — covers `src/` and `tests/` |

P2 coverage:

- **phase boundaries** — every boundary tested at `−1 ns`, exactly on it, and `+1 ns`;
  representative offsets across the window; monotonicity across 310 seconds; independence
  from the absolute epoch; full config validation;
- **determinism** — a >100-event stream spanning all five phases and every event type,
  asserted to give identical final state, identical snapshots, and an identical
  *step-by-step trajectory* across repeated runs, with stepwise reduction equal to the fold;
- **accounting integration** — the mandatory `120/100 → −$2 / −$22` example driven through
  the event stream; each fill counted exactly once; duplicate fill rejected with prior state
  intact; identical payloads with distinct ids both applying;
- **ordering** — strictly increasing ordinal enforced; equal timestamps ordered by ordinal;
  decreasing timestamp rejected; reversed order rejected rather than silently reinterpreted;
- **immutability** — every event, `MarketState`, `MarketDefinition`, `PhaseConfig`, and
  `MarketSnapshot`; prior states unaffected by later transitions; the order map read-only;
  every non-scalar snapshot field proven frozen by attempting mutation;
- **validation** — wrong market, illegal phase claim, duplicate fill, malformed ordering,
  negative timestamps, empty identities, identical up/down tokens, non-positive tick,
  unknown token, out-of-range prices and sizes;
- **purity** — the static guard now also sweeps top-level Plane 2 modules and forbids
  filesystem, process, and persistence imports (`os`, `io`, `pathlib`, `subprocess`,
  `threading`, `pickle`, …) alongside the existing clock, network, and nondeterminism bans.
  No exemption was added to make anything pass.

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

**L1 remains UNRUN and is not relabelled.** `DEVELOPMENT_PLAN.md` sets P1's gate as "L0
green **and** the arithmetic reproduces a known target-wallet market ledger exactly". No
reconstructed fill-level target-wallet ledger exists in this repository, so that half of the
P1 gate has never been executed. It is not passed and it is not waived. It needs either the
reconstructed per-market ledgers or the first recorded live-paper data from P13.

P2's own gate — "phase machine is a pure function of the event stream, and a static check
proves Plane 2 modules import no clock, no I/O, and no network module" — **was executed and
passed**.

---

## Update ritual

At each accepted boundary, update: current phase, branch, boundary commit, last accepted
milestone, next milestone, blockers, open-item labels, and test status. If a phase gate was
not actually executed, say so — code existing is not completion
([`DEVELOPMENT_PLAN.md`](DEVELOPMENT_PLAN.md), rule 1).
