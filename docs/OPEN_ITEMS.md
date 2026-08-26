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
- **P8 status — measurable now, still OPEN.** The instrumentation exists and has been run
  against one real market: per-stage timings on a monotonic high-resolution clock, with
  SpotTick and CLOB paths kept in separate distributions, and a shadow queue estimator feeding
  the `AT_FRONT` / `PRICE_OK_BUT_DEEP` classifier. What is now available is the *measurement*;
  what is still missing is the *answer*.
- **Why one market does not close it.** A single window is one market regime, one time of day,
  one machine, and — critically — a **shadow** estimate rather than a real queue position, since
  no order was sent. The queue model also carries a known optimistic bias (see
  `ARCHITECTURE_SSOT` §4.3) that a real-order comparison would need to quantify.
- **Closing experiment:** in live paper, correlate measured end-to-end latency against
  achieved `queue_ahead` and against the `AT_FRONT` rate. The closing artefact is a
  latency-to-queue-position curve plus the threshold beyond which `AT_FRONT` degrades. Real
  venue order round-trip time remains **unmeasured** until P14.

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
- **P8 status — timeline available, model not attempted.** Spot and CLOB events now carry
  synchronized latency-clock readings in the same trace stream, which is the raw material the
  closing experiment needs. **No causal claim is made**: nothing in P8 asserts that a given
  SpotTick caused a given CLOB change, because no causal matching rule has been established.
  Building and fitting that model is P15.
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
MONEY_SCALE = 1_000_000     1 collateral  = 1_000_000 MoneyUnits
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

- **Status:** **CLOSED** — P10A, 2026-08-26. Evidence:
  [`evidence/P10A-O11-RESOLUTION-RESEARCH.md`](evidence/P10A-O11-RESOLUTION-RESEARCH.md).
- **Source:** Canonical §18.1; Detailed §32.

### What closed it

55 **consecutive** real settled `btc-updown-5m` markets (2026-08-25T19:35Z → 2026-08-26T00:05Z),
plus 6 **consecutive** settlements watched live from before they ended, plus on-chain state read
from four independent Polygon RPC providers. No synthetic market was used.

### The precedence, with the three concepts kept apart

```text
AUTHORITATIVE FINAL   Conditional Tokens payout vector on Polygon (chain id 137)
                      0x4D97DCd97eC945f40cF65F87097ACe5EA0476045
                      payoutDenominator(conditionId) > 0 gates redeemability;
                      payoutNumerators is the payout vector.

ADVISORY CROSS-CHECK  Gamma outcomePrices, CLOB tokens[].winner
                      55/55 agreement, 0 disagreements, 0 missing - but more than
                      two minutes late, and capable of being absent.

RULE SOURCE           Chainlink BTC/USD TWAP 60 s data stream, named by the markets
                      themselves in 55/55. NOT independently recomputed: the Data
                      Streams API is credentialed. A documented rule, not a
                      verified calculation.

PRE-ON-CHAIN          payoutDenominator == 0 is "not yet authoritative for
                      redemption", whatever any venue currently says.
```

**Outcome-index mapping, proven in both directions:** slot 0 = `Up` (27 markets), slot 1 =
`Down` (28 markets), unanimous across the corpus. `clobTokenIds` follows `outcomes` order.

**Time to availability:** the chain is not only authoritative but *earliest* — CTF payout at p50
**+85.6 s** after market end (min 54.3, max 86.6), while Gamma and the CLOB had not reflected the
outcome in any of the six markets within the ~206 s observation window. Correctness and speed
point the same way, so nothing is bought by preferring a faster source.

### Ambiguous branch — explicit and fail-closed

`AMBIGUOUS`, and no redemption authorised, if an advisory source names a different winner than
the payout vector; if two RPC providers disagree at comparable finality; if the payout vector is
not exactly one non-zero slot summing to the denominator (fractional, tied, or a slot count other
than two); or if the token mapping disagrees with the market metadata. This feeds P9's existing
`RESOLUTION_AMBIGUOUS` kill switch. There is no else-branch that picks a winner.

### Still OPERATIONAL

**Confirmation depth.** No Polymarket-specified requirement was found; measured Polygon finality
lag was 1–4 blocks (1–6 s) across three providers. P10 must expose it as configuration. The
source decision and the confirmation policy are separate.

### Recorded discrepancies

- The resolver for these markets is `0x58e1745bedda7312c4cddb72618923da1b90efde` in 55 of 55 —
  **not** the officially documented UMA Adapter. Address verified from real `ConditionResolution`
  events; official name unverified and not guessed. Gamma's `umaResolutionStatus: "resolved"` is
  a field name, not evidence of the UMA path.
- Published collateral is now **pUSD**, not the USDC an archived example would use.
- Press coverage says 5-minute markets use a 30 s TWAP window; the markets' own metadata says
  60 s in three independent places. Real data taken over the article.

### What this does not close

O14 (strike/start-price chaining) remains **OPEN**. The `twapLookbackSeconds: 60` observation is
suggestive and is not the evidence O14 requires.

---

### Collateral migration does not reopen O10  *(noted 2026-08-26, P10)*

Polymarket's published collateral is now **pUSD** (`0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB`),
not the USDC that older examples assume. pUSD is **6 decimals**, exactly as USDC was, so
`MONEY_SCALE = 1_000_000` still represents collateral exactly and O10 stays **CLOSED**. What the
migration changes is a contract address and a name, not the numeric representation — which is
why `MoneyUnits` is documented as naming the *unit* rather than the token.

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

## O15 — `LiveOrderTable.current()` is linear in orders ever placed

**Status: CLOSED.** Measured in P8, corrected and confirmed on real market data in
`fix/p8-measurement-hotpath-closure`.

`LiveOrderTable.current(outcome)` delegated to `occupying(outcome)`, which filtered and sorted
**every order the table had ever held** — terminal orders included — on every call. The
reconciler calls it once per side, so twice per cycle.

### Evidence

One live order plus N retained terminal orders, nanoseconds per cycle for two `current()`
calls. Both implementations measured on the *same* table so the comparison is like-for-like
(`tools/live_order_lookup_bench.py`):

| Retained terminal orders | before | after |
| ---: | ---: | ---: |
| 0 | 1,311 | 477 |
| 200 | 52,097 | 467 |
| 1,049 | 251,406 | 498 |
| 10,000 | 2,512,039 | 461 |

Least-squares slope: **253.5 ns per retained order before, −0.0011 after** — flat.

The instrumented market run `btc-updown-5m-1787652900` placed **1,049** orders in a single
5-minute market, so late-market cycles were spending on the order of 251 µs re-scanning dead
orders. That is larger than the whole measured `receive_to_reconcile` p50 of 323 µs and
explains why `reconcile_duration` (p50 171 µs) dominated the critical path.

### The fix, and what it deliberately does not do

History is **not** pruned. Terminal orders remain retained for idempotency, late
acknowledgements, and auditability. The table additionally keeps an incremental per-outcome
index of the orders currently occupying each side, updated transactionally on every lifecycle
transition and never rebuilt by rescanning. Index membership is recomputed from the order's
status rather than toggled, so an unusual transition back into an occupying state restores the
index correctly instead of leaving it silently wrong.

Replacement-race semantics are unchanged: where several orders occupy one side, the earliest by
client order id is returned, now as an explicit total order over a small set.

### Closure conditions (§12 of the correction brief)

| # | Condition | Status |
| --- | --- | --- |
| 1 | Full historical order retention remains | **MET** — 10,000 terminal orders still addressable |
| 2 | `current()` no longer O(history) | **MET** — structural test proves it never walks `orders` |
| 3 | Race / multiple-occupying semantics deterministic | **MET** — earliest-by-id, tested repeatedly |
| 4 | 10k-terminal-history regression passes | **MET** |
| 5 | Measured cost no longer grows with retained history | **MET** — slope −0.0011 ns/order |
| 6 | Fresh real run: reconcile latency no longer dominated by the lookup | **MET** — see below |

### Condition 6: confirmed on a fresh real market

`btc-updown-5m-1787658900`, 117,772 cycles, `live_trading_enabled: false`, 0 orders sent:

| Stage p50 (ns) | Before | After | Change |
| --- | ---: | ---: | ---: |
| `reconcile_duration` | 171,659 | **14,882** | **−91.3%** |
| `receive_to_reconcile` | 323,138 | **240,367** | −25.6% |

Reconciliation has gone from the largest stage on the critical path to the smallest — it is now
below both `decide_duration` (66,105) and `prepare_duration` (17,764). All six conditions are
met, so **O15 is CLOSED**. Full evidence:
[`evidence/P8B-MEASUREMENT.md`](evidence/P8B-MEASUREMENT.md).

---

## P9 added no open item, and closed none

Operational safety observations do not resolve strategy parameters. The risk thresholds — feed
staleness, clock drift, API error window and count, recovery confirmations — are `OPERATIONAL`
engineering configuration under invariant I18, not reconstructed constants, so they need no
open item either. The frozen sources name the *conditions* in Canonical §28.1 and establish no
numbers at all for them.

Two P9 facts belong in the record without being open items:

* **Authenticated reconciliation is UNRUN, not unresolved.** Real taker fills, real order-state
  uncertainty, real account position and cost reconciliation, and real write-API behaviour are
  all **DEFERRED TO P14**. No credential exists and no order has been sent, so there is nothing
  to be uncertain *about* yet — this is an unrun experiment, not an unanswered question about
  the strategy.
* **`OFF_PRICE` remains structurally unreachable in shadow mode** (recorded in the P8C
  evidence), for the same reason: it needs real dispatch latency.

---

## P10 added one open item (O16) and closed none of the strategy items

Settlement is `OPERATIONAL` machinery, so almost nothing in it is a strategy question. Two P10
facts belong in the record:

* **The finality-lag tolerance is a decided engineering question, not an open one.** Real data
  answered it: `SettlementPolicy.tolerate_provider_block_lag` defaults to `True` because the
  strict reading halted 3 of the first 9 live markets on ordinary skew between providers'
  `finalized` heads. The strict behaviour is still reachable by configuration and both are
  tested. See `docs/evidence/P10-SETTLEMENT-REAL-MARKET.md`.
* **Authenticated redemption is UNRUN, not unresolved** — `DEFERRED TO P14`, like the rest of
  the authenticated surface. The transaction plan and its encoding are validated against the
  real contract by `eth_call`; what is unrun is submitting one.

### O16 — should `RESOLUTION_AMBIGUOUS` latch?

**Status: OPEN.** P9 does not list `RESOLUTION_AMBIGUOUS` in `REQUIRES_RECONCILIATION`, so
unlike `POSITION_MISMATCH` or `ORDER_STATE_UNCERTAIN` it clears as soon as anything sets its
flag to `False`. P10 does not exploit that: `maker5m.settlement.safety` never emits
`flag=False`, so the halt is sticky in practice and only a deliberate operator signal lifts it.

That makes the current behaviour safe but load-bearing on one module's restraint rather than on
the risk engine's own contract. Whether P9 should latch it instead is a P9 design question and
is **not** being answered here by quietly editing `REQUIRES_RECONCILIATION`.

**Closing experiment:** decide whether an ambiguous settlement is a condition that can ever be
observed to have passed (like a stale feed, which recovers) or one that requires positive
evidence (like a position mismatch, which does not). The distinction is only testable against a
real ambiguous settlement, which has not yet occurred: in 70 real markets the chain never
contradicted itself, and every ambiguity observed was either injected by us or ordinary
provider lag. **UNRUN pending a genuine venue ambiguity.**

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
| O11 | Authoritative resolution source | **CLOSED** — CTF payout vector, P10A real-market evidence | — |
| O12 | BTC spot fixed-point scale | **CLOSED** — self-describing representation retained | — |
| O13 | Quote-centre tick tie-breaking | OPEN | — (reference policy in use) |
| O14 | Strike chaining unverified / no strike published | OPEN | strike-dependent models |
| O15 | `LiveOrderTable.current()` linear in orders ever placed | **CLOSED** — indexed, confirmed on real data | — |
| O16 | Should `RESOLUTION_AMBIGUOUS` latch (P9 `REQUIRES_RECONCILIATION`)? | OPEN | — (halt is sticky by construction) |
