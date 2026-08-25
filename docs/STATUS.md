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

These are tracked separately for the rest of the project, and P3 is the first phase where
they visibly diverge.

| | |
|---|---|
| **Implementation gate** | The code does what this phase specified, and it is proven by tests. |
| **Empirical replication correctness** | The code does what the *target wallet* did. Only replay or live evidence can establish this. |

A green suite is evidence of the first and **no evidence at all** of the second. Nothing in
this repository may be described as "confirmed strategy behaviour" merely because tests pass.

---

## Current position

| | |
|---|---|
| **Current phase** | **P3 — Strategy core: Up-space + grid sizing** |
| P3 implementation gate | **PASSED** |
| P3 empirical replication | **UNPROVEN** — O04 open, O01 open, O13 open |
| Current branch | `feature/p3-strategy-core` |
| P3 boundary commit | recorded by the immediately following commit on this branch |
| Last accepted milestone | P2 — MarketState + events + phase machine (`fcd8ddf`, tip `c322ce5`) |
| Next milestone | **P4 — ENDGAME controller + strategy decision engine** |
| `main` | `c322ce5` — fast-forwarded to the accepted P2 HEAD, pushed |
| Remote | `origin` → `https://github.com/bomb707/hedge.git` |

### Branch policy

```text
main                            latest ACCEPTED milestone
feature/<phase>                 active phase development
bootstrap/phase-0               retained P0 boundary
feature/p1-numeric-accounting   retained P1 boundary
feature/p2-market-state-events  retained P2 boundary
```

Nothing merged, rebased, squashed, or force-pushed. `main` advances by fast-forward only.

---

## Blockers

| Blocker | Blocks | Detail |
|---|---|---|
| **O04** — the two sources give different DOWN-side grid targets | **P3 empirical correctness** | Both readings are now implemented as named policies and both reproduce their documented worked examples. Which one the wallet used is still unknown, and the modular fingerprint provably cannot arbitrate — now regression-tested over 100 000 inventories. Closes only on replay evidence. |
| **O12** — BTC spot fixed-point scale unknown | **P6** | No authoritative evidence for external-feed precision. `BtcPrice` is self-describing so nothing is frozen. |
| **O11** — authoritative resolution source unnamed | P10 | Both sources say "prefer on-chain" without naming a source, depth, or timeout. |

O01 and O13 do not block P4 from being built, but they do bound what P4's output can be
claimed to mean.

---

## Open strategy items

Full detail in [`OPEN_ITEMS.md`](OPEN_ITEMS.md). **P3 closed none of them and added one.**

```text
O01 quote-centre source            OPEN      O08 latency for queue dominance OPEN
O02 volatility sigma               OPEN      O09 spot-to-CLOB timing model   OPEN
O03 base-lot L selection rule      OPEN      O10 venue precision / scales    CLOSED for kernel
O04 grid-target selection          OPEN *    O11 resolution source           OPEN
O05 endgame tilt magnitude         FITTED    O12 BTC spot scale              OPEN * (P2)
O06 endgame gate magnitude         FITTED    O13 tick tie-breaking           OPEN   (new, P3)
O07 fee/rebate calibration         OPEN

* blocking
```

### O13 — quote-centre tick tie-breaking (new)

The sources say `round_tick(C)` and give one example, `0.6274 -> 0.63`. That excludes FLOOR
and says **nothing** about a tie: at `tick = 0.01` a raw centre of `0.625` can legitimately
quote `0.62` or `0.63`. Since the quoted tick determines queue position, and queue position
is where the edge lives, adopting a tie rule by reaching for Python's `round` would have been
an unexamined strategy decision. Three named policies are implemented; `HALF_EVEN` is the
reference default for a stated structural reason, and stays labelled `OPEN`.

---

## What P3 delivered

- **Up-space** — `complement(p) = PRICE_SCALE - p`, exact integer, involutive, endpoint-
  correct, and tick-alignment preserving; venue ↔ synthetic translation both ways.
- **Zero synthetic spread** — built by complementing the *quantized* centre, so the property
  holds under every tie policy. Asserted at construction.
- **Quote centre (O01)** — `QuoteCentre` protocol; `ClobMidCentre` computes an exact rational
  midpoint from the UP top of book, reports an explicit reason when unavailable, and never
  invents a midpoint from one side or from the DOWN book.
- **Tick quantization (O13)** — three named tie policies, no built-in `round`.
- **Base lot (O03)** — `BaseLotSelector` protocol; `ConfiguredBaseLotSelector` validates
  `15 / 20 / 25` and is deliberately inert.
- **Grid (O04)** — `GRID = 5 shares` exactly; both target-selection readings as named
  policies, plus named grid tie rules.
- Every replaceable component exposes a `ParameterStatus` at runtime (I18).

### Not present, by design

No `gamma`, no `band_skew`, no endgame, no favourite, no `endgame_tilt`, no `endgame_band`,
no `band_hard`, no eligibility gate, no `decide()`. The soft `0.11-0.89` band is **not**
enforced anywhere and is regression-tested as such. `MarketState` is read but never mutated,
and no strategy field was added to it.

### Measured

Full pricing + sizing pass (centre → quantize → prices → grid): **~4.7 µs**.

```text
complement              122 ns      ClobMidCentre.compute   1 332 ns
quantize_centre         315 ns      plan_grid CANONICAL     2 117 ns
build_quote_prices      943 ns      plan_grid OBSERVED      1 861 ns
```

Dominated by dataclass validation, which is worth keeping at these event rates (a few
thousand book updates per 300 s market). No pathological design; P8 owns real latency work.

---

## Tests

| | |
|---|---|
| Status | **green** |
| Suite | 472 passed (325 at the P2 boundary; +147 in P3) |
| `ruff check` | clean |
| `ruff format --check` | clean, 71 files |
| `mypy` (strict) | clean, 64 files — `src/` and `tests/` |
| Fingerprint corpus | **100 000 inventories × both O04 policies** (99 951 fractional) |

P3 coverage highlights:

- **fingerprint at scale** — the congruence itself (`up_size ≡ -I`, `down_size ≡ +I`, mod 5
  shares) asserted for 100 000 deterministic inventories under both policies, plus a
  cross-product sweep over all base lots and grid tie rules;
- **the O04 divergence is pinned** — both worked examples asserted exactly, and a test
  requires the policies to keep disagreeing on most inventories, so the conflict cannot
  vanish behind a green suite;
- **the fingerprint's blindness is asserted** — both policies pass it for every inventory,
  which is the reason O04 cannot be closed from the documents;
- **tie behaviour** — every tick and grid tie policy tested at exact half points, including
  the demonstration that independently rounding both sides breaks zero spread for `HALF_UP`
  and `HALF_DOWN` at all 100 tie points and never for `HALF_EVEN`;
- **zero spread** — asserted at every price on every supported tick grid, and a hand-built
  spread is rejected at construction;
- **purity** — the P0/P2 static guard extends unchanged over the new `strategy/` modules; no
  exemption was added.

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
exists in this repository, so that half of the P1 gate has never been executed. It is not
passed and it is not waived.

L1 is also what would close O04. The same missing artefact blocks both.

---

## Update ritual

At each accepted boundary, update: current phase, branch, boundary commit, last accepted
milestone, next milestone, blockers, open-item labels, and test status. Record the
implementation gate and the empirical status **separately**. If a gate was not actually
executed, say so — code existing is not completion
([`DEVELOPMENT_PLAN.md`](DEVELOPMENT_PLAN.md), rule 1).
