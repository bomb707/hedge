# Open Items

Genuinely unresolved questions extracted from the frozen strategy sources. **Nothing here
is resolved in this repository yet.** Each item states what must remain configurable and
what experiment closes it.

Rules:

- An `OPEN` item must never be hard-coded as an assumption (I18). It is a named config
  field carrying the label `OPEN`.
- A `FITTED` value may be used as a default but must stay configurable and stay labelled.
- Closing an item requires the recorded experiment, not a plausible argument. When one is
  closed, record the evidence and the date here and change the label.

Status legend: `OPEN` unresolved · `FITTED` provisional value in use · `BLOCKING` must be
answered before the named phase can be considered correct.

---

## O01 — Exact quote-centre source

- **Status:** OPEN · BLOCKING for P3 correctness claims (not for P3 delivery)
- **Source:** Canonical §9, §36 OPEN-1; Detailed §7
- **Why it matters:** The centre `C` determines both quote prices, the endgame favourite
  direction, and which price level the bot pre-empts. It is the single largest lever on
  queue position, and queue position is where the edge lives (Canonical §10.1).
- **Candidates:** `clob_mid` · `binance_fv` (TWAP fair value) · blended `w*F + (1-w)*M`.
  Canonical §9 suggests starting at `clob_mid` because it introduces the fewest unverified
  assumptions, while keeping the external feed on the decision path (I11).
- **Must remain configurable:** `centre_source`, `blend_weight`, the reaction threshold,
  and the whole `QuoteCentre` component behind one interface. Swapping the centre model
  must not touch execution or accounting code.
- **Closing experiment:** ≥200 live-paper markets (Canonical §36 OPEN-1). For each
  candidate, compare predicted quote level against the level the CLOB actually moved to,
  and compare realised queue position. Classify every quote as `AT_FRONT` /
  `PRICE_OK_BUT_DEEP` / `OFF_PRICE` / `NOT_QUOTING` (Canonical §34-L3, Detailed §35). The
  winner is the source with the best `AT_FRONT` rate at equal or better adverse selection.

---

## O02 — Exact volatility `sigma`

- **Status:** OPEN
- **Source:** Canonical §7, §36 OPEN-2; Detailed §8
- **Why it matters:** `sigma` feeds the TWAP-settlement fair value. The `twap_window = 60s`
  and the effective-variance form are CONFIRMED; the volatility input is not. It shapes how
  fast probability accelerates toward 0/1 in the final minute — exactly the window in which
  the profit-producing inventory is accumulated.
- **Must remain configurable:** `sigma` as an explicit input, plus the estimator behind it
  (realised window length, sampling frequency, update cadence). Canonical §7 is explicit:
  *do not ship a guessed sigma as though it were confirmed.*
- **Closing experiment:** fit realised BTC volatility over candidate windows against the
  observed CLOB probability path within markets; select the estimator minimising
  centre-vs-realised-move error. Only meaningful once O01 chooses a model that uses sigma.

---

## O03 — Exact base-lot `L` selection rule

- **Status:** OPEN (values CONFIRMED, rule OPEN)
- **Source:** Canonical §13, §36 OPEN-3; Detailed §4
- **Why it matters:** `L` sets the distance to both grid targets and therefore the whole
  order-size path and the terminal residual alignment. Replay indicates the correct `L`
  materially affects PnL, so it is a first-class strategy parameter, not a constant.
- **Observed values:** `15 / 20 / 25` per market. Why a given market uses one is unknown.
- **Candidate drivers to test (not to assume):** realised volatility, account equity, time
  of day, market liquidity, expected retail flow, previous market behaviour, queue
  conditions.
- **Must remain configurable:** `choose_base_lot(market_state)` as a replaceable strategy
  component. Canonical §13 is explicit: *do not permanently hard-code `L = 15`.* A safe
  default is acceptable for early phases; a frozen constant is not.
- **Closing experiment:** label historical markets by their apparent `L`, then test each
  candidate driver for separation across the three classes. Confirm by replaying with the
  fitted selector and comparing per-market size sequences against the target's.

---

## O04 — Grid-target selection behaviour  ⚠ source conflict

- **Status:** OPEN · **BLOCKING for P3** — the two frozen sources disagree numerically.
- **Source:** Canonical §12.1, §36 OPEN-4; Detailed §12; figure `d3_grid.png`
- **Why it matters:** It changes the outward order size directly, on every quote.

**The conflict**, using the worked example both documents share (`I = -28.63`, `L = 15`):

```text
                            target      size
Canonical §12.1 formula     UP   -15    13.63     <- both sources agree
Canonical §12.1 formula     DOWN -45    16.37     <- round_to_grid(I - L)
Detailed §12 + d3_grid.png  DOWN -30     1.37     <- nearest lattice point below I
```

The UP side agrees. The DOWN side does not: Canonical's `ask_target = round_to_grid(I - L)`
yields `-45`, while Detailed §12's worked example and the figure both show `-30`, the
lattice point immediately below `I`, with size `1.37`.

Critically, **the modular fingerprint (I04) does not disambiguate**: `16.37 mod 5` and
`1.37 mod 5` both equal `1.37`. The conformance test passes either way, so it cannot be
used as evidence for one reading.

Two sub-questions, both open:

1. **Which DOWN-side target is correct?** By the precedence rule, Canonical §12.1 wins and
   must be implemented first. But Detailed §12 and the figure are direct reconstructions of
   observed order sizes, which is exactly the kind of evidence that would overturn a
   formula. This needs deciding on evidence, not precedence, before P3 is trusted.
2. **The downward boundary correction is unspecified.** Canonical §12.1 gives the upward
   correction explicitly (`if t <= I: t += grid`) and says only "equivalent logic should be
   used for the downward target". Whether the mirror is `if t >= I: t -= grid` (strict
   symmetry) is an inference. Additionally, Python's `round()` is round-half-to-even, which
   changes the result whenever `(I ± L) / 5` lands exactly on `.5`; whether banker's
   rounding is intended or incidental is unstated.

Canonical §36 OPEN-4 independently flags that "target-step behavior may have additional
structure", which is consistent with this discrepancy being real rather than a typo.

- **Must remain configurable:** `GridSizer` behind one interface with the target-selection
  rule as a swappable policy, so both readings can be replayed against the same journal
  without touching anything else. Both must be implemented as named policies.
- **What is settled, and what is not.** The 5-share lattice and the modular fingerprint
  (I04) are CONFIRMED and are not in question here. What remains OPEN is only the
  *target-selection rule* that decides which lattice point each side aims at.
- **Precedence is not evidence.** The document precedence rule makes the Canonical formula
  the **default implementation choice** so that P3 has something to run. It does not make it
  the observed behaviour of the target wallet, and it must never be recorded as such.
  Neither reading may be declared the true target-selection rule until replay evidence
  closes this item. Both are carried as named, selectable policies:

```text
GridTargetPolicy.CANONICAL   Canonical §12.1 formula          (default, not proven)
GridTargetPolicy.OBSERVED    Detailed §12 / d3_grid.png       (alternative, not proven)
```

- **Closing experiment:** replay the target wallet's reconstructed per-market order-size
  sequences under each policy and compare size-by-size. The correct policy reproduces the
  observed sizes exactly; the wrong one diverges on the first fractional inventory state.
  Until then P3 runs the default, logs every state where the two policies disagree, and
  claims no correctness for either.
- **P1 did not touch this.** The numeric kernel contains no lattice logic of any kind. Its
  `quantize_order_size` helper is venue submission quantisation (2 decimals) and is
  explicitly not the 5-share grid.

---

## O05 — Exact endgame tilt magnitude

- **Status:** FITTED (`30` shares in use) · OPEN as a value
- **Source:** Canonical §15.1, §30, §36 OPEN-5; Detailed §25, §49
- **Why it matters:** The tilt sets the terminal residual, which is the entire Term 2
  profit mechanism. Too small and the residual cannot pay for the matched-pair loss; too
  large and the residual is acquired too expensively to be profitable (Canonical §17).
- **What is settled:** the *existence* of explicit favourite targeting is CONFIRMED, and
  that it does not emerge automatically from symmetric quoting (Canonical §15). Only the
  magnitude is fitted.
- **Must remain configurable:** `endgame_tilt`, labelled FITTED, with the direction rule
  (`favourite = UP if centre > 0.5`) separate from the magnitude.
- **Closing experiment:** sweep tilt across replayed markets, scoring on **full-cost
  settlement PnL** (I01), never on residual size. Cross-check the fitted value against the
  observed terminal-residual distribution (~25-40 shares, Canonical §14.1).

---

## O06 — Exact endgame gate magnitude

- **Status:** mechanism CONFIRMED · magnitude FITTED (`5` shares) · OPEN as a value
- **Source:** Canonical §15.2, §30; Detailed §26, §49
- **Why it matters:** Without a binding gate the target is inert (Canonical §15.2). The
  gate width controls how hard the bot refuses fills that move inventory away from the
  favourite target, and therefore how much late flow it forgoes.
- **Must remain configurable:** `endgame_band`, labelled FITTED, gate evaluated as
  `d = I - target_I; up_allowed = d < +band; down_allowed = d > -band`.
- **Closing experiment:** sweep gate width jointly with O05 — they interact, so a
  one-dimensional sweep of either is not sufficient. Score on full-cost settlement PnL and
  on how often the gate actually binds; a gate that never binds is not a gate.

---

## O07 — Fee / rebate calibration

- **Status:** OPEN (maker fee `0` is CONFIRMED; the rebate model is not)
- **Source:** Canonical §3, §30, §36 OPEN-6; Detailed §43; figure `d9_edge.png`
- **Why it matters:** The rebate is a material fraction of a very thin edge — the
  reconstruction attributes roughly `0.096c` of `0.255c` total per share to it. Getting the
  rebate model wrong biases every `pnl_if_up` / `pnl_if_down` the strategy reasons with,
  including the endgame economic guardrail. The `p(1-p)` weighting shape in `d9_edge.png`
  implies the rebate depends on the price at which a fill occurs, so it cannot be modelled
  as a flat per-share credit.
- **Must remain configurable:** the whole `RebateLedger` accrual model; `estimated_rebate`
  (live) and realised rebate (post-market) as **distinct fields**, never conflated (A6);
  the attribution rule for which market a rebate belongs to.
- **Closing experiment:** reconcile modelled `estimated_rebate` against actual received
  rebates across many settled markets; the residual must go to zero. Until it does, the
  strategy must be able to display PnL both with and without the estimate.

---

## O08 — Required latency distribution for queue dominance

- **Status:** OPEN
- **Source:** Canonical §10, §22, §27, §36 OPEN-7; Detailed §18, §33, §36
- **Why it matters:** Fill count collapses as shares-ahead grows — the reconstructed curve
  falls from 38 fills at zero ahead to 0 at 30 ahead. Whether the bot achieves `AT_FRONT`
  is therefore a latency question, and no target p50/p95/p99 has been established. Without
  a number there is no way to say whether the implementation is fast enough or to know when
  to stop optimising.
- **Must remain configurable / measurable:** every stage of the critical path
  (`feed receive → decode → state update → decide → diff → submit → venue ack`) must be
  separately timestamped at high resolution so the budget can be attributed, not just
  totalled.
- **Closing experiment:** in live paper, correlate measured end-to-end latency against
  achieved `queue_ahead` and against the `AT_FRONT` rate. The closing artefact is a
  latency-to-queue-position curve plus the threshold beyond which `AT_FRONT` degrades.

---

## O09 — Spot-to-next-CLOB-level timing model

- **Status:** OPEN
- **Source:** Canonical §9.2, §10, §29.7; Detailed §18
- **Why it matters:** This is the pre-emption mechanism itself: observe a BTC move, predict
  that Polymarket's touch will move to a specific new level, and rest there before anyone
  else. It requires both a *level* prediction and a *timing* prediction. Canonical §29.7
  calls pure CLOB-following "potentially fatal", so this model is load-bearing rather than
  a refinement — but its form is entirely unspecified.
- **Must remain configurable:** the mapping from spot move → predicted next level, the
  reaction threshold that decides a move is worth acting on, and whether the bot rests at
  the predicted level before the CLOB confirms it.
- **Closing experiment:** from recorded joint spot/CLOB journals, measure the distribution
  of lag between a qualifying spot move and the corresponding CLOB touch change, and the
  hit rate of level prediction. Depends on O01 and interacts with O08.

---

## O10 — Venue quantity and price precision (numeric scale selection)

- **Status:** **CLOSED FOR NUMERIC KERNEL** (P1, 2026-08-25) · residual validation open for P6
- **Source:** derived requirement — Canonical §12.3, §32 ("precision"); `ARCHITECTURE_SSOT`
  §6

### Why this item existed

`SHARE_SCALE`, `MONEY_SCALE`, and `PRICE_SCALE` are frozen at P1 and cannot change
afterwards without invalidating every recorded replay journal and every stored ledger. If
the chosen scale cannot exactly represent a venue-reported fill quantity, the ledger either
silently rounds — violating I03 — or halts. Choosing the scale from a guess rather than from
venue behaviour is precisely the failure the exactness contract exists to prevent, so P0
recorded it as blocking rather than picking a plausible number.

### Evidence used to close it

From Polymarket's official CLOB implementation:

```text
COLLATERAL_TOKEN_DECIMALS   = 6
CONDITIONAL_TOKEN_DECIMALS  = 6
supported tick sizes        = 0.1 | 0.01 | 0.001 | 0.0001
order builder size rounding = 2 decimal places
```

The critical distinction, which the decision turns on:

```text
ORDER INPUT QUANTIZATION  !=  AUTHORITATIVE LEDGER PRECISION
```

An order may be *submitted* with its size rounded to two decimals, but the resulting
position and collateral movements settle in 6-decimal atomic units. The ledger is
authoritative over what the venue actually moved, not over what we asked for. Sizing the
scales to the submission granularity would have silently truncated real fills.

### Decision

```text
SCALE_DECIMALS = 6

SHARE_SCALE = 1_000_000     1 share       = 1_000_000 ShareUnits
MONEY_SCALE = 1_000_000     1 USDC        = 1_000_000 MoneyUnits
PRICE_SCALE = 1_000_000     probability 1 = 1_000_000 PriceUnits
```

Six decimals matches the venue's atomic units exactly and represents every documented tick
size exactly — the finest, `0.0001`, is `100` price units, leaving two decimal digits of
headroom below it. Frozen in `src/maker5m/numeric/scales.py`; the two-decimal submission
quantisation lives separately in `numeric/ticks.py` and is never applied to a ledger input.

### Residual requirement — still open, owned by P6

> Verify real `btc-updown-5m-*` messages against the frozen scales before live execution.

The evidence above is upstream implementation constants, not observed traffic for this
specific market universe. P6 must confirm against recorded messages that (a) no fill or book
message carries more than six decimals of size or price, and (b) no market in the universe
uses a tick outside the documented set. The kernel already enforces this at runtime: a
non-representable value raises `NotRepresentableError` rather than rounding, so a wrong
assumption surfaces as a halt rather than as silent ledger corruption. That guard is what
makes it safe to proceed to P2 before the traffic check is done.

---

## O11 — Authoritative resolution source

- **Status:** OPEN · BLOCKING for P10
- **Source:** Canonical §18.1; Detailed §32
- **Why it matters:** Both sources say to prefer authoritative resolution evidence,
  "especially on-chain redemption / resolution evidence when available", and warn against
  relying blindly on unsynchronised cached market metadata — but neither names the specific
  source, the confirmation depth, or the timeout behaviour. Resolving to the wrong winner
  corrupts realised PnL and the redemption action itself.
- **Must remain configurable:** the resolution source, the fallback order, and the
  ambiguity timeout. Ambiguous resolution near settlement is already a kill-switch input
  (Canonical §28.1), so the ambiguous branch must be explicit rather than an else-case.
- **Closing experiment:** compare candidate sources across settled markets for agreement
  and for time-to-availability; select the earliest source that never disagrees with final
  on-chain state.

---

## O12 — BTC spot fixed-point scale

- **Status:** OPEN · BLOCKING for P6 · raised in P2
- **Source:** derived requirement — Canonical §24.2 (external pricing state); P2 event
  contracts

### Why it exists

`PriceUnits` cannot carry a BTC price: it is a probability constrained to `[0, 1]`. External
spot needs its own exact fixed-point representation, and that representation needs a scale.

This repository holds **no authoritative evidence** for the precision of the external BTC
feed — nothing equivalent to the published `COLLATERAL_TOKEN_DECIMALS` and tick set that
closed O10. Freezing a scale from a guess would repeat exactly the failure O10 existed to
prevent: a scale too coarse for a real feed value either rounds the input silently, which
corrupts every downstream comparison, or halts the bot in production.

### What P2 did instead of guessing

`maker5m.market.btc_price.BtcPrice` is **self-describing**: it carries its own
`scale_decimals` alongside its integer units. That keeps every value exact and float-free
while leaving the scale question genuinely open. Comparison across two different scales
normalises exactly rather than comparing raw integers, which would be meaningless.

The same type carries the market strike, which is the same kind of quantity.

Parsing goes through the one strict parser in the project
(`maker5m.numeric.units.parse_fixed_point`), so a feed value with more precision than the
chosen scale raises `NotRepresentableError` rather than rounding — the same fail-closed
property that makes a wrong scale surface as a halt instead of as silent corruption.

### What must remain configurable

The scale itself, per feed. Nothing in the deterministic core assumes a value.

### Closing experiment

Sample real Binance (or whichever external feed is selected) BTC messages and determine the
maximum decimal precision actually used for price. Then decide, explicitly, whether to
promote `BtcPrice` to a single frozen global scale — which would be cheaper per event and is
the right end state — or to keep it self-describing because more than one feed is in play.
Promoting it is a contract change that requires a replay-journal version bump.

---

## Summary table

| ID | Item | Status | Blocking |
|---|---|---|---|
| O01 | Quote-centre source | OPEN | P3 correctness |
| O02 | Volatility `sigma` | OPEN | — |
| O03 | Base-lot `L` selection rule | OPEN | — |
| O04 | Grid-target selection (**source conflict**) | OPEN | **P3** |
| O05 | Endgame tilt magnitude | FITTED / OPEN | — |
| O06 | Endgame gate magnitude | FITTED / OPEN | — |
| O07 | Fee / rebate calibration | OPEN | — |
| O08 | Latency distribution for queue dominance | OPEN | — |
| O09 | Spot-to-next-CLOB-level timing model | OPEN | — |
| O10 | Venue precision / numeric scale | **CLOSED for kernel** (P6 traffic check open) | — |
| O11 | Authoritative resolution source | OPEN | P10 |
| O12 | BTC spot fixed-point scale | OPEN | **P6** |
