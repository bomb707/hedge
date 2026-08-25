# Invariants

Rules that must hold in every build, every replay, and every live session.
A violation is a **defect**, not a tuning choice.

Each row cites the frozen strategy source that establishes it. Cited as
`C §n` = Canonical Strategy Spec, `D §n` = Detailed Strategy By Step and Role.
When in doubt, open the cited section — do not trust the summary here.

---

## 1. Strategy status vocabulary

Every strategy-relevant statement, parameter, and config field carries exactly one label
(C §1.2):

| Label | Meaning | Implementation consequence |
|---|---|---|
| `CONFIRMED` | Supported by the reconstructed evidence. | May be encoded as a fixed rule. |
| `FITTED` | Chosen by replay / small-sample fitting. Likely, not established. | Must be a named config value, labelled `FITTED`. |
| `OPEN` | Unresolved. | Must be config + must appear in `OPEN_ITEMS.md`. Never silently defaulted into an assumption. |
| `OPERATIONAL` | Engineering control, not proven target-wallet logic. | Must be a named config value, labelled `OPERATIONAL`. |

Every config field carrying a `FITTED`, `OPEN`, or `OPERATIONAL` value must expose that
label at runtime, so telemetry and the UI can show which numbers are not established.

---

## 2. Accounting invariants

### I01 — Complete dual-token accounting is authoritative
`C §3`, `C §35`, `D §22`

```text
total_cost  = cost_up + cost_down

pnl_if_up   = n_up   - total_cost - fees + rebates
pnl_if_down = n_down - total_cost - fees + rebates
```

Both hypothetical settlement values are **first-class live state**, recomputed on every
fill — not a post-trade report (`D §48`). Holding more of the eventual winner does **not**
imply profit (`C §3.1`). Profitability tests are only ever:

```text
UP profitable   iff n_up   > total_cost + fees - rebates
DOWN profitable iff n_down > total_cost + fees - rebates
```

The `Term1 + Term2` decomposition (`C §4`) is an **analytic view** and must reproduce the
same number as the exact settlement accounting, to the last unit, for every market. Any
divergence is a defect in one of the two. Note `C §4`'s literal `Term2 = R * (1 - a_W)` is
wrong when the bot ends holding more of the *loser*; use the corrected general form recorded
in `ARCHITECTURE_SSOT.md` §10 A9, which is what makes this invariant hold in both cases.

### I02 — Net inventory
`C §3.3`, `C §5.1`

```text
I = n_up - n_down
```

`I` is directional exposure. It is **not** an economic result and never substitutes for
`pnl_if_up` / `pnl_if_down`.

### I03 — Inventory uses true fractional filled quantities
`C §12.3`, `D §11`

Inventory is accumulated from actual fill sizes including partial fills. Inventory is
**never rounded to an integer**. Every fill applies `I += signed_fill_size` at full
precision. Fractional-looking order sizes such as `13.63` are the correct, intended output
of the strategy — not an error to clean up.

---

## 3. Sizing invariants

### I04 — 5-share grid sizing must preserve the modular fingerprint
`C §12`, `C §12.2`, `D §12`, `D §13`

Sizes are derived from inventory targets on a 5-share lattice, never from a fixed nominal
lot (`C §29.1`):

```text
bid_target = grid_target_above(I, L)
ask_target = grid_target_below(I, L)

bid_size = bid_target - I
ask_size = I - ask_target
```

Mandatory conformance test on every generated order:

```text
bid_size ≡ (-I) mod 5
ask_size ≡ (+I) mod 5
```

A generated order failing the fingerprint means the sizing engine is wrong. This is a
blocking unit-test invariant, not a warning.

---

## 4. Pricing and execution invariants

### I05 — Synthetic quoted spread is zero
`C §8.1`, `C §29.2`, `D §9`

```text
delta_ticks = 0
bid_price = ask_price = round_tick(C)
```

translated to venue orders as `BUY UP @ C` and `BUY DOWN @ (1 - C)`.

The only permitted deviation is post-only safety (I06): a quote may be **suppressed or
adjusted** to avoid crossing. Every such deviation must be recorded as a telemetry event
with a reason (`D §16`). A deliberate positive spread is forbidden.

The `0.11 - 0.89` price band is **soft** (`C §8.3`, `C §29.9`). Implementing it as a hard
cutoff is forbidden; it would remove endgame fills that create Term 2.

### I06 — `post_only = true` is a hard strategy constraint
`C §11`, `C §23`, `D §16`

Enforced locally before submission **and**, where the venue supports it, at the API level.
Where zero-spread intent conflicts with post-only safety, **post-only safety wins**.

### I07 — An intentional taker fill is forbidden
`C §11`

```text
TAKER FILL = EXECUTION BUG
```

A taker fill is a risk event: it must halt new quoting and raise an alert, not be absorbed
silently.

### I08 — A fill immediately changes authoritative state and recomputes BOTH sides
`C §19.1`, `C §23` (INVARIANT 8), `D §21`, `D §39`

```text
fill -> n_up/n_down -> cost basis -> I -> pnl_if_up/pnl_if_down
     -> recompute BOTH desired order sizes -> reconcile
```

A fill on one side changes the desired size of the other side. Recomputing only the filled
side is a defect.

### I09 — Unchanged valid orders must be KEPT
`C §20`, `C §33`, `D §17`

```text
desired price == live price
and desired size == live size
and order healthy
    -> KEEP  (queue priority preserved)
otherwise
    -> replace
```

Cancel-all-then-replace on every market-data event is forbidden. Every cancellation
destroys the queue timestamp, and queue position is load-bearing (`C §10.1`).

### I10 — Do not cancel/requote on a fixed timer
`C §20.1`, `C §29.6`, `D §37`

No `min_requote_ms` delay before replacement. Rate limiting must be a token bucket that is
free under normal activity and only constrains excess rate
(`max_requotes_per_sec ~ 8`, `OPERATIONAL`).

Related, and equally binding: `queue_improve_depth = 0` (`C §10.2`, `D §19`). The bot does
not routinely pay a tick to jump the queue; the intended advantage is speed into a new
price level.

### I11 — External BTC spot must be able to wake the decision path independently
`C §9.1`, `C §29.7`, `D §6`, `D §18`

The external feed must sit on the decision path even while the quote centre is still the
CLOB midpoint. Its established value is **latency / queue pre-emption**, not superior
directional prediction. A pure "follow the CLOB after it moves" design is potentially fatal
to the strategy.

---

## 5. Regime invariants

### I12 — Normal strategy uses no inventory skew
`C §14`, `C §29.3`, `D §14`

```text
gamma = 0
band_skew = 0
```

`C = fair_value - gamma * I` must not be implemented for the replica. Explicit restoring
skew damages the terminal residual mechanism. During QUOTE, inventory is allowed to roam
substantially (excursions to ~±100 are expected, `C §14.1`).

### I13 — ENDGAME is explicit and distinct from QUOTE
`C §15`, `C §31`, `D §24`

ENDGAME is a separate phase (~`T0+240s`) with its own decision rules. The correct favourite
residual does **not** emerge automatically from symmetric quoting.

### I14 — ENDGAME uses favourite target plus a binding gate
`C §15.1`, `C §15.2`, `D §25`, `D §26`

```text
favourite = UP if centre > 0.50 else DOWN
target_I  = +endgame_tilt if favourite == UP else -endgame_tilt

d = I - target_I
bid_allowed = d < +endgame_band
ask_allowed = d > -endgame_band
```

`endgame_tilt = 30` and `endgame_band = 5` are **FITTED** magnitudes (see `OPEN_ITEMS.md`
O05, O06). The *mechanism* is CONFIRMED; the numbers are not. Without a binding gate the
target is mathematically present but operationally inert.

ENDGAME modifies **order eligibility only**. It remains post-only, grid-sized, zero-spread,
and event-driven (`D §29`). It is never "market buy 30 favourite shares".

Endgame success is judged economically, not by `I` alone (`C §17`, `D §28`): a 30-share
favourite residual can still be unprofitable if the matched-pair leg cost too much.

---

## 6. Exit invariants

### I15 — Never flatten merely to reach zero inventory before settlement
`C §18`, `C §29.4`, `D §31`

No sell, hedge, merge, split, convert, or stop-loss as part of this strategy. Flattening
removes the main Term 2 profit mechanism.

### I16 — Exit is settlement / redemption
`C §18`, `C §31`, `D §30`, `D §33`

At `~T0+280s` cancel all orders and place no new ones. Hold both balances through
resolution, then redeem the winner at `$1.00`. Resolution must come from authoritative
information, preferring on-chain resolution/redemption evidence over unsynchronised cached
market metadata (`C §18.1`, `D §32`).

### I17 — Hard inventory/risk limits are safety walls, not mean-reversion controls
`C §14.1`, `C §28`, `D §15`

```text
band      ~ 40    observational soft region (not a control)
band_hard ~ 100   true safety wall
```

`band_hard` blocks the outward side only at the wall. It must not continuously fight
ordinary inventory movement. Do not confuse intra-market excursion (~±100) with terminal
residual (~25-40).

---

## 7. Engineering invariants

### I18 — OPEN/FITTED values remain configurable and explicitly labelled
`C §1.2`, `C §13`, `C §40`, `D §49`

They must not silently become assumptions. `L` in particular must never be permanently
hard-coded; `choose_base_lot(market_state)` is a replaceable strategy component
(`C §13`). Any code change that cannot be classified as CONFIRMED / FITTED / OPEN /
OPERATIONAL must not be introduced without explicit review.

### I19 — The trading hot path performs no blocking persistence or UI work
`C §22`, `D §36`

Forbidden synchronously on the path from event to order action:

```text
database persistence      dashboard rendering      historical analytics
heavy logging             blocking HTTP            pandas/dataframe work
slow serialization        synchronous disk I/O     research calculations
```

The UI and persistence planes must never take a lock the trading state depends on, and
must never block it. Rationale: total strategy edge is ~`0.255` cents/share against a
`1.000` cent tick (`C §27`) — a latency error on a minority of fills can invert the
strategy's sign.

### I20 — Deterministic replay
`C §34` (L2), `C §38`

The same ordered input event stream must produce the same strategy decisions. Replay must
execute the **same** `StrategyEngine.decide()` code as production — not a re-implementation.
This requires: no wall-clock reads inside strategy code (time arrives as event data), no
unordered-iteration dependence, and exact integer/fixed-point arithmetic in decisions
rather than accumulated binary floating point.

---

## 8. Venue-level execution invariants

From `C §23`, in addition to the above:

```text
maximum two live strategy orders per market
one BUY UP + one BUY DOWN in the normal two-sided state
```

---

## 9. Forbidden "improvements"

`C §29`, `C §37`, `D §44`, `D §50`. Never introduce these while replicating:

```text
positive quoted spread          conventional inventory skew
fixed nominal order sizes       pre-settlement flattening
hard 0.11-0.89 price cutoff     timer-based requoting
routine one-tick queue jumping  reward-farming logic
blind CLOB-following only       residual alone as profitability metric
```

**Replication correctness first. Latency and precision second. Optimization only after both
are proven.**
