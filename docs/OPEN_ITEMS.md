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
- **P3 status:** the `QuoteCentre` protocol exists with `ClobMidCentre` as the reference
  candidate, labelled `ParameterStatus.OPEN` at runtime. `BINANCE_FV` and `BLEND` are
  declared in `CentreSource` but deliberately unimplemented — they need the TWAP model and
  O02's sigma. The CLOB midpoint is computed from the **UP** top of book only; deriving it
  from the DOWN book would fold Canonical §5.2's conditional mirror identity into the centre
  as an assumption.
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
- **P3 status:** the `BaseLotSelector` protocol exists with `ConfiguredBaseLotSelector`,
  which returns an explicitly configured lot and carries `ParameterStatus.OPEN`. It is
  deliberately inert — it does not consult market state at all, because inferring `L` from
  any candidate driver would close this item by assumption. Only `15 / 20 / 25` validate.
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
- **P3 implemented both, and closed nothing.** `maker5m.strategy.grid` ships
  `GridPolicy.CANONICAL_OFFSET` and `GridPolicy.OBSERVED_ADJACENT`. Each reproduces its
  documented worked example exactly, and the divergence is pinned by a regression test so it
  cannot quietly disappear. A further test asserts that **both policies satisfy the modular
  fingerprint for all 100 000 generated inventories** — direct confirmation that the
  fingerprint is not evidence for either reading. Two sub-behaviours remain inferred rather
  than evidenced, and are marked as such in the code: the Canonical policy's downward
  boundary correction (the sources give only the upward one), and the Observed policy's
  behaviour when inventory already sits exactly on the lattice (neither source shows it).
- **The grid-rounding tie rule is now explicit too.** `GridRounding` names `HALF_EVEN` /
  `HALF_UP` / `HALF_DOWN`; the built-in `round` is used nowhere. `HALF_EVEN` is the reference
  default because it matches what Canonical §12.1's literal `round(...)` would do in Python,
  which is a reading of the text, not evidence of intent.

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
- **P4 status:** implemented as `StrategyConfig.endgame_tilt`, defaulting to 30 shares and
  carrying `ParameterStatus.FITTED` in every decision record. The direction rule is separate
  from the magnitude, so a sweep changes only the number.
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
- **P4 status:** implemented as `StrategyConfig.endgame_band`, defaulting to 5 shares and
  carrying `ParameterStatus.FITTED`. Both gate inequalities are **strict**, so a side is
  blocked exactly at its boundary; the boundaries are regression-tested at one unit either
  side in both favourite directions.
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

### Residual requirement — **COMPLETED in P6** (2026-08-25)

> Verify real `btc-updown-5m-*` messages against the frozen scales before live execution.

P6 ran the check against real captured `btc-updown-5m-*` traffic. Every observed price and
size is exactly representable by the frozen six-decimal scales; the observed precisions and
tick sizes are recorded in `STATUS.md` under the P6 capture evidence, and asserted by
`tests/feeds/test_exactness_o10.py` against committed real fixtures.

The check is not a one-off audit: `maker5m.feeds.exactness` runs it on **every** parsed
message, raising `ExactnessError` rather than rounding, so a future venue change surfaces as
a halt instead of silent ledger corruption.

### Venue tick set expanded (P7, 2026-08-25) — O10 stays closed

`polymarket-client==0.6.0` declares
`TickSize = Literal["0.1", "0.01", "0.005", "0.0025", "0.001", "0.0001"]`. The venue added
`0.005` and `0.0025` after this repository's set was first written, and
`SUPPORTED_TICK_SIZES` has been corrected to match.

Both are **exactly representable** at `PRICE_SCALE = 1_000_000` — `5_000` and `2_500` price
units, each dividing the scale exactly — so this is a venue-capability correction, not a
numeric-scale failure. O10 is **not** reopened. It would only reopen if a real venue value
could not be represented, which remains false.

This does not change `MarketDefinition.tick = 0.01`: the replica's quote grid comes from the
frozen strategy evidence, not from what the venue happens to permit.

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

- **Status:** **CLOSED — self-describing representation retained** (P6, 2026-08-25)
- **Source:** derived requirement — Canonical §24.2; P2 event contracts

### Why this item existed

`PriceUnits` is a probability constrained to `[0, 1]` and cannot carry a BTC price. P2 needed
an exact representation and this repository held no evidence for the external feed's
precision, so freezing a global scale would have repeated the failure O10 exists to prevent.
P2 therefore made `BtcPrice` self-describing and deferred the question here.

### Evidence

Official metadata, `GET https://api.binance.com/api/v3/exchangeInfo?symbol=BTCUSDT`, captured
2026-08-25 (fixture `tests/feeds/fixtures/binance_exchangeinfo.json`):

```text
PRICE_FILTER.tickSize = "0.01000000"      minPrice = "0.01000000"
quotePrecision        = 8                 baseAssetPrecision = 8
```

Live messages, `wss://stream.binance.com:9443/ws/btcusdt@aggTrade`, same date (fixture
`tests/feeds/fixtures/binance_aggtrade.json`, plus the full capture run):

```text
price field `p` is a decimal string, e.g. "80190.01000000"
observed decimals: 8 in every sampled message (trailing zeros, tick is 0.01)
```

Every captured value round-trips exactly through `BtcPrice`, asserted by
`tests/feeds/test_binance_adapter.py`.

### Decision — **A. keep the self-describing representation**

`BtcPrice(units, scale_decimals)` stays permanent. This is a *closure*, not a deferral: a
dynamic exact representation is the answer, because

* precision is **symbol metadata**, not a global constant — `tickSize` and `quotePrecision`
  come from `exchangeInfo` and differ per symbol, so any hard-coded scale would be wrong for
  the next symbol or the next venue;
* the feed sends decimal strings, so the exact precision is available in the message itself;
  deriving the scale from the string is exact by construction and cannot silently round;
* a frozen scale would have to be chosen large enough for every future feed, which is the
  same guess this item existed to avoid.

**This is explicitly not "a fixed BTC scale was found".** The finding is that the value's
precision is data, so the representation carries it.

### Residual

None for the representation. A different external feed may of course publish different
precision; the adapter derives the scale per message, so that requires no change — only a
fixture and a conformance test for the new source.

---

---

## O13 — Quote-centre tick quantization / tie-breaking

- **Status:** OPEN · raised in P3 · reference policy in use, labelled OPEN
- **Source:** Canonical §8.1 (`bid_price = ask_price = round_tick(C)`), §32
  (`px_up = round_to_tick(centre)`); Detailed §10 ("Apply Exact Tick Rounding")

### Why it exists

The sources say "round" and give exactly one worked example:

```text
C_raw = 0.6274   ->   C_quote = 0.63
```

That example **rules out FLOOR/TRUNCATE** (which would have quoted `0.62`). It says nothing
whatsoever about a tie. At `tick = 0.01` a raw centre of `0.625` can legitimately quote
`0.62` or `0.63` depending on the rule.

This is not a formatting detail. Which tick the bot rests at determines its place in a
price-time queue, and fill rate collapses as queue depth ahead grows (Canonical §10.1) —
queue position is where this strategy's edge lives. A tie rule adopted by accident, in
particular by reaching for Python's `round` and inheriting banker's rounding, would be an
unexamined strategy decision.

### Candidate policies

```text
HALF_EVEN   tie to the even tick     implemented - reference default
HALF_UP     tie to the higher tick   implemented
HALF_DOWN   tie to the lower tick    implemented
FLOOR       excluded by the worked example; not offered
CEILING     not formally excluded by that one example, but excluded by the
            sources' own wording ("round"); not offered
```

### Why HALF_EVEN is the reference default

Canonical §32 rounds *both* sides independently from the raw centre:

```text
px_up   = round_to_tick(centre)
px_down = round_to_tick(1.0 - centre)
```

Under that construction a tie breaks the zero-spread property (I05) for `HALF_UP` and
`HALF_DOWN` at **every one** of the 100 tie points on the `0.01` grid — the two rounded
prices stop complementing each other and a one-tick synthetic spread appears. Under
`HALF_EVEN` it holds at every tie, because the two integer parts always have opposite parity
so exactly one of them rounds up. This is regression-tested.

That is a **consistency argument, not evidence about the target wallet**, so the policy stays
labelled `OPEN`.

Independently, `maker5m.strategy.prices` builds the DOWN price by complementing the
*already-quantized* centre rather than rounding `1 - C_raw` separately. That construction is
exact under **every** tie policy, so the unresolved default cannot silently become
load-bearing for zero spread.

### What must remain configurable

`TickRounding` as a named policy on every quantization call. No code path may use the
built-in `round` to decide a strategy price.

### Closing experiment

Recover raw centres and the quoted tick for reconstructed target-wallet markets and look for
a case where the raw centre lands exactly on a half tick. A single such observation settles
it. Failing that, live-paper A/B: run the candidate policies over the same recorded journals
and compare realised queue position and `AT_FRONT` rate (Detailed §35). Interacts with O01 —
a different centre source changes how often ties even occur.

---

## O14 — Strike chaining is unverified; no strike is published

- **Status:** OPEN · raised in P6 · blocks any strategy component that needs the strike
- **Source:** Canonical §6.2 (`coinPriceStart[N] == coinPriceEnd[N-1]`), §21; observed public
  Polymarket metadata, 2026-08-25

### The finding

The frozen strategy states that the strike is chained from the previous window, and treats
that as what makes pre-arm possible. **The public metadata publishes neither field.**

Checked on live `btc-updown-5m-*` markets, both the Gamma event payload and the CLOB market
payload: no `coinPriceStart`, no `coinPriceEnd`, and no strike field under any other name.
What the metadata *does* say is how resolution works:

```text
cryptoMarketConfig = {"id": "btc-5m-twap-60", "asset": "btc", "duration": "5m",
                      "twapEnabled": true, "twapLookbackSeconds": 60}
```

and the market description: resolves UP if the Chainlink BTC/USD **TWAP over the window** is
at or above *the price at the beginning of that range*. So a reference price certainly exists
— it is simply not exposed as a queryable field on either public endpoint.

Two things follow, and they are worth separating:

* the 60-second TWAP settlement of Canonical §7 is **corroborated by live venue metadata**
  (`twapLookbackSeconds: 60`), which is a genuine confirmation;
* the *chaining relationship* of Canonical §6.2 is **unverified**. It may well hold, but
  nothing observable here demonstrates it.

### What P6 did about it

Nothing was fabricated. `MarketDefinition.strike` is left `None` and
`DiscoveredMarket.strike_available` is `False`. Deriving a strike from the previous market's
outcome would be inventing a number the venue never published, and a wrong strike would feed
straight into any future fair-value model.

Pre-arm does not depend on it: token ids, `T0`, venue rules, and subscriptions are all
resolvable ahead of time, which is what the opening seconds actually need.

### What must remain configurable

The strike source. Nothing may assume the strike is available, and nothing may assume the
chaining relationship until it is demonstrated.

### Closing experiment

Read the Chainlink BTC/USD TWAP data stream named as the resolution source, sample the
reference price at successive window boundaries, and test whether market `N`'s reference
equals market `N-1`'s final value. That closes both the availability question and the
chaining question, and is a prerequisite for any centre model that needs the strike (O01
option 2, O02).

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
| O10 | Venue precision / numeric scale | **CLOSED** (kernel + P6 live traffic check) | — |
| O11 | Authoritative resolution source | OPEN | P10 |
| O12 | BTC spot fixed-point scale | **CLOSED** — self-describing representation retained | — |
| O13 | Quote-centre tick tie-breaking | OPEN | — (reference policy in use) |
| O14 | Strike chaining unverified / no strike published | OPEN | strike-dependent models |
