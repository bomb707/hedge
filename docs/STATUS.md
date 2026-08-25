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
| **Current phase** | **P1 — fixed-point numeric kernel + exact accounting** |
| Current branch | `feature/p1-numeric-accounting` |
| P1 boundary commit | recorded by the immediately following commit on this branch |
| Last accepted milestone | P0 — architecture, invariants, open items, plan, and skeleton frozen (`acc7ab2`, tip `f77b34d` on `bootstrap/phase-0`) |
| Next milestone | **P2 — MarketState + event contracts + phase machine** |
| Baseline commit | `0de1f7c` — pristine import of the supplied strategy sources; `main` still points here, untouched |
| Remote | `origin` → `https://github.com/bomb707/hedge.git` |

### Branch policy

```text
main                        accepted milestones only (currently still at the baseline)
bootstrap/phase-0           accepted P0 boundary
feature/<phase>             active phase development
```

No branch has been merged, rebased, squashed, or force-pushed.

---

## Blockers

| Blocker | Blocks | Detail |
|---|---|---|
| **O04** — the two frozen sources give different DOWN-side grid targets | **P3** | Canonical §12.1 yields `-45` (size `16.37`); Detailed §12 and `d3_grid.png` show `-30` (size `1.37`) for the same worked example. The modular fingerprint passes either way, so it cannot arbitrate. **Precedence is not empirical proof** — P3 carries both as named policies and claims correctness for neither until replay evidence decides. Untouched by P1. |
| **O11** — authoritative resolution source unnamed | P10 | Both sources say "prefer on-chain" without naming a source, confirmation depth, or timeout. |

`O10` is no longer a blocker — see below. Nothing blocks P2.

---

## Open strategy items

Full detail in [`OPEN_ITEMS.md`](OPEN_ITEMS.md).

```text
O01 quote-centre source            OPEN      O07 fee/rebate calibration      OPEN
O02 volatility sigma               OPEN      O08 latency for queue dominance OPEN
O03 base-lot L selection rule      OPEN      O09 spot-to-CLOB timing model   OPEN
O04 grid-target selection          OPEN *    O10 venue precision / scales    CLOSED for kernel
O05 endgame tilt magnitude         FITTED    O11 resolution source           OPEN
O06 endgame gate magnitude         FITTED

* blocking
```

Nothing here may be hard-coded as an assumption (I18).

### O10 — closed for the numeric kernel (P1, 2026-08-25)

Closed on upstream evidence, not on a guess: Polymarket's official CLOB implementation uses
`COLLATERAL_TOKEN_DECIMALS = 6` and `CONDITIONAL_TOKEN_DECIMALS = 6`, and supports tick
sizes `0.1 / 0.01 / 0.001 / 0.0001`. Its two-decimal order-size rounding is *submission*
quantisation and deliberately did **not** drive the choice, because
`ORDER INPUT QUANTIZATION != AUTHORITATIVE LEDGER PRECISION`.

```text
SHARE_SCALE = MONEY_SCALE = PRICE_SCALE = 1_000_000        (frozen)
```

One residual requirement stays open and is owned by **P6**: verify real `btc-updown-5m-*`
messages against the frozen scales before live execution. The kernel raises
`NotRepresentableError` rather than rounding, so a wrong assumption would surface as a halt
rather than as silent ledger corruption — which is why P2 may proceed first.

---

## Tests

| | |
|---|---|
| Status | **green** |
| Suite | 183 passed (28 at the P0 boundary; +155 in P1) |
| `ruff check` | clean |
| `ruff format --check` | clean |
| `mypy` (strict) | clean, 31 files — now covers `tests/` as well as `src/` |

P1 test coverage:

- **numeric primitives** — exact parsing of zero, positive, negative, and six-decimal
  values; trailing-zero normalisation accepted; non-representable precision rejected rather
  than rounded; malformed input rejected (exponents, underscores, whitespace, bare dot,
  non-ASCII digits, non-string input); exact tick alignment and conversion; order-size
  quantisation truncating toward zero;
- **ledger** — empty, UP-only, DOWN-only, interleaved, fractional partial fills, 1 000
  accumulated small fills with no drift, multiple costs, fees, immutability, order
  independence, and the full validation surface;
- **rebates** — estimated and realised kept distinct, neither accrual touching the other,
  estimate replaceable by a recomputing model, and PnL differing correctly per mode with no
  default mode anywhere;
- **settlement** — UP wins, DOWN wins, break-even, both branches negative, winner
  profitable, winner still losing;
- **mandatory regression** — `120 UP / cost 72`, `100 DOWN / cost 50` ⇒ `-$2` if UP wins and
  `-$22` if DOWN wins, asserting explicitly that holding 20 more shares of the winning
  outcome is still a losing market;
- **Term1 / Term2** — exact identity for both winners, unequal and equal inventories,
  one-sided and empty markets, fractional inventories, exact rationals with no float;
- **property tests** — 400 seeded random ledgers × both winners × all three rebate modes,
  asserting the PnL identities, the term identity, settlement agreement, integer-only state,
  and fill-order independence. Seeded `random.Random`, not Hypothesis: P1 adds no
  dependency, and a fixed seed keeps failures reproducible. Adding Hypothesis as a dev-only
  dependency is the right fix if this input space ever proves too narrow.

Carried forward from P0 and still green: frozen-source checksums, package import,
live-trading-disabled, required documents, and the static Plane 2 purity guard (which now
has real modules to check — `numeric`, `market`, and `accounting` import no clock, no I/O,
no networking, and no source of nondeterminism).

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

**L1 is not executable here.** `DEVELOPMENT_PLAN.md` sets P1's gate as "L0 green **and** the
arithmetic reproduces a known target-wallet market ledger exactly". No reconstructed
target-wallet ledger exists in this repository, so that half of the gate has not been run —
it is not passed, and it is not waived. It needs either the reconstructed per-market ledgers
or the first recorded live-paper data from P13. The kernel is structured to make the check a
pure data exercise when that arrives: feed the fills, compare `n_up`, `n_down`, `cost_up`,
`cost_down`, `Term1`, `Term2`, and net PnL.

---

## Update ritual

At each accepted boundary, update: current phase, branch, boundary commit, last accepted
milestone, next milestone, blockers, open-item labels, and test status. If a phase gate was
not actually executed, say so — code existing is not completion
([`DEVELOPMENT_PLAN.md`](DEVELOPMENT_PLAN.md), rule 1).
