# Polymarket BTC 5-Minute Maker Bot — Canonical Strategy System Specification

**Status:** Strategy systematization for implementation  
**Target market:** Polymarket `btc-updown-5m-*` binary markets  
**Target behavior:** Functional replication of the analyzed target wallet strategy  
**Primary implementation requirement:** Accuracy and completeness take priority over convenience. No strategy rule, accounting constraint, execution invariant, phase transition, or known uncertainty should be silently omitted or simplified.

---

## 0. Purpose

This document converts the reverse-engineered wallet behavior into a single implementation-oriented strategy specification.

It is intended to be the **canonical strategy reference** used before coding the bot.

The bot must reproduce the strategy as a system, not merely imitate individual trades. In particular, implementation must preserve:

1. the exact binary-market accounting,
2. the Up-space abstraction,
3. the 5-share inventory grid,
4. zero synthetic quoted spread,
5. post-only maker execution,
6. queue-position preservation and latency sensitivity,
7. event-driven quote reconciliation,
8. the distinction between ordinary quoting and the explicit endgame regime,
9. the fact that matched-pair market making can lose while the terminal residual creates the profit,
10. settlement-only exit and redemption,
11. all currently known open parameters and uncertainties.

A profitable result cannot be inferred merely from holding more shares of the eventual winner. The entire acquisition cost of both outcome tokens must be included in every profitability calculation.

---

# 1. Canonical Evidence and Precedence

The strategy is reconstructed from:

- the earlier **Polymarket 5-Minute BTC Maker — Strategy Specification**,
- the later **Target Wallet Strategy — Complete Specification**,
- the attached figures covering:
  - mirrored Up/Down books,
  - market lifecycle timing,
  - inventory grid behavior,
  - zero-spread fill relationships,
  - inventory-band behavior,
  - PnL decomposition,
  - complementary minting,
  - rebate weighting,
  - edge budget,
  - event-loop behavior.

## 1.1 Precedence rule

The later **Target Wallet Strategy — Complete Specification** explicitly supersedes earlier strategy specifications.

Therefore:

- if the later document confirms or changes an earlier conclusion, the later conclusion is canonical;
- the earlier document remains useful for its larger evidence samples and statistical measurements;
- unresolved items remain unresolved unless the later document explicitly closes them.

## 1.2 Confidence labels

Every implementation-relevant statement should be classified as one of:

- **CONFIRMED** — directly supported by the reconstructed data/evidence.
- **FITTED** — selected from replay or small-sample fitting; likely but not fully established.
- **OPEN** — still unresolved and must remain configurable or experimentally testable.
- **OPERATIONAL** — implementation choice required for engineering but not proven to be part of the target wallet's original logic.

The bot must never convert an `OPEN` item into a hard-coded assumption without explicit testing.

---

# 2. Strategy in One Sentence

The target strategy is a:

> **latency-sensitive, post-only, zero-spread two-sided maker strategy that uses a 5-share inventory lattice during the market, then explicitly tilts inventory toward the current favourite during the final minute and holds both outcome tokens to settlement, relying on the winning residual plus maker rebate to overcome losses from the matched-pair making leg.**

The strategy is **not** primarily:

- classic arbitrage,
- directional BTC prediction,
- conventional spread capture,
- liquidity-mining reward farming,
- stop-loss trading,
- inventory flattening,
- taker execution.

---

# 3. Exact Binary-Market Accounting — Non-Negotiable

This is the top-level accounting invariant.

Let:

- `n_up` = total UP shares held,
- `n_down` = total DOWN shares held,
- `cost_up` = total dollars spent acquiring UP,
- `cost_down` = total dollars spent acquiring DOWN,
- `fees` = total trading fees,
- `rebates` = total maker rebates attributed to the market.

Then:

```text
total_cost = cost_up + cost_down
```

If UP wins:

```text
gross_payout = n_up
PnL_if_UP = n_up - total_cost - fees + rebates
```

If DOWN wins:

```text
gross_payout = n_down
PnL_if_DOWN = n_down - total_cost - fees + rebates
```

Therefore:

```text
UP winning does NOT imply profit.
DOWN winning does NOT imply profit.
```

The exact profitability conditions are:

```text
UP profitable   iff n_up   > total_cost + fees - rebates
DOWN profitable iff n_down > total_cost + fees - rebates
```

Break-even is equality.

## 3.1 Example

Suppose:

```text
120 UP   @ 0.60 -> $72
100 DOWN @ 0.50 -> $50
total cost      -> $122
```

UP wins:

```text
UP payout = $120
DOWN payout = $0
PnL = 120 - 122 = -$2
```

The wallet correctly held more UP than DOWN and still lost money.

## 3.2 Required live accounting state

The bot must continuously maintain:

```text
n_up
n_down
cost_up
cost_down
fees
estimated_rebate
total_cost
pnl_if_up
pnl_if_down
```

The two hypothetical settlement values are:

```text
settlement_edge_up   = n_up   - total_cost - fees + estimated_rebate
settlement_edge_down = n_down - total_cost - fees + estimated_rebate
```

These are economically more informative than inventory alone.

## 3.3 Inventory versus economics

Net inventory:

```text
I = n_up - n_down
```

tells us **directional exposure**.

But:

```text
pnl_if_up
pnl_if_down
```

tell us **actual settlement economics**.

Both must be tracked.

---

# 4. Exact Term 1 / Term 2 PnL Decomposition

The strategy analysis also expresses PnL using matched pairs and terminal residual.

For the eventual winner `W` and loser `L`:

```text
M = min(n_W, n_L)
R = n_W - n_L
```

Let:

```text
a_W = average acquisition price of winner token
a_L = average acquisition price of loser token
```

Then:

```text
PnL =
M * (1 - a_W - a_L)
+
R * (1 - a_W)
```

where:

- **Term 1** = matched-pair market-making economics,
- **Term 2** = residual winner position.

This decomposition is algebraically equivalent to the exact settlement accounting above.

## 4.1 Example correspondence

Using:

```text
120 UP @ 0.60
100 DOWN @ 0.50
UP wins
```

we get:

```text
M = 100
R = 20
```

Term 1:

```text
100 * (1 - 0.60 - 0.50)
= -$10
```

Term 2:

```text
20 * (1 - 0.60)
= +$8
```

Total:

```text
-$10 + $8 = -$2
```

Exactly the same as:

```text
120 - (72 + 50) = -$2
```

## 4.2 Strategic meaning

A positive winner residual is **not sufficient**.

The residual must be large enough and cheap enough to overcome:

- adverse selection in matched pairs,
- any fees,
- execution error,
- stale pricing,
- queue-position degradation.

---

# 5. Core Binary-Market Abstraction: Up-Space

Because one UP token plus one DOWN token settles to exactly `$1.00`:

```text
BUY DOWN @ d  ≡  SELL UP @ (1 - d)
```

Therefore the two-token buy-only strategy can be represented as one synthetic UP book.

## 5.1 Translation

| Venue action | Up-space meaning | Inventory effect |
|---|---|---:|
| `BUY UP @ p`, size `q` | BID UP @ `p` | `I += q` |
| `BUY DOWN @ d`, size `q` | ASK UP @ `1-d` | `I -= q` |

Net inventory:

```text
I = n_up - n_down
```

This is the main managed inventory state.

## 5.2 Mirrored orderbook identity

The DOWN book contains no independent directional information if all levels map exactly:

```text
down_price = 1 - up_price
down_size  = corresponding up-side size
```

The strategy can therefore reason primarily in Up-space and translate orders back into Polymarket's two BUY-token books.

---

# 6. Market Lifecycle

Each market is a fixed 300-second BTC Up/Down window.

Canonical phases:

```text
PREARM
QUOTE
ENDGAME
SETTLING
DONE
```

## 6.1 Timing

Current canonical timing:

```text
T0 - previous window:
    discover / prepare next market

T0 + ~3 s:
    begin active quoting

T0 + 240 s:
    enter ENDGAME

T0 + 280 s:
    cancel all live orders

T0 + 300 s:
    window closes

after resolution:
    redeem winning token
```

Observed first fills are around `T0+3s` to `T0+7s`.

No logic should depend on an exact first-fill second.

## 6.2 Chained strike

The strike is chained:

```text
coinPriceStart[N] == coinPriceEnd[N-1]
```

Therefore the next market can be prepared before it starts.

The system should not waste the critical opening seconds waiting for information that can be known during the previous window.

---

# 7. Settlement Model

The later reconstruction states that settlement uses a 60-second TWAP rather than a single closing BTC tick.

This means endgame fair value should not be modeled as a simple endpoint-only digital option.

Proposed effective variance:

```text
tau = min(60, time_left)

Var_eff =
sigma^2 * (time_left - tau)
+
sigma^2 * tau / 3
```

Equivalent implementation form:

```python
def twap_fair_value(spot, strike, time_left, sigma, twap_window=60.0):
    tau = min(twap_window, time_left)
    rem = max(time_left - tau, 0.0)
    var = sigma * sigma * (rem + tau / 3.0)

    if var <= 1e-12:
        return 1.0 if spot > strike else 0.0

    d = (log(spot / strike) - 0.5 * var) / sqrt(var)
    return normal_cdf(d)
```

### Status

- `twap_window = 60s`: **CONFIRMED**
- exact live volatility `sigma`: **OPEN**

The bot must not ship a guessed `sigma` as though it were confirmed.

---

# 8. Quote Price Structure

## 8.1 Zero synthetic spread

The target's Up-space bid and ask sit at the same price:

```text
delta_ticks = 0
bid_price = ask_price = round_tick(C)
```

Translated back to venue orders:

```text
BUY UP   @ C
BUY DOWN @ (1 - C)
```

subject to tick rounding and post-only validation.

This is a core strategy property.

## 8.2 Tick

```text
tick = 0.01
```

All reconstructed fills sit on the one-cent grid.

## 8.3 Soft price band

Observed concentration:

```text
0.11 <= p <= 0.89
```

for most fills.

But the band is **soft**, not a hard prohibition.

Endgame fills have been observed outside it, including near extremes.

Therefore do not implement:

```text
if p < 0.11 or p > 0.89:
    stop quoting
```

A hard cutoff would remove some of the very endgame fills that help create Term 2.

---

# 9. Quote Centre

The exact quote centre remains the most important unresolved pricing component.

Candidate sources:

1. `clob_mid`
2. `binance_fv`
3. blended fair value

Suggested initial implementation:

```text
centre_source = clob_mid
```

while using Binance spot as a **wake-up / predictive timing signal**.

## 9.1 Important distinction

The external BTC feed is not established as a superior source of directional value.

Its likely advantage is:

```text
latency / queue pre-emption
```

rather than:

```text
better long-run prediction
```

The CLOB midpoint is already highly explained by BTC spot.

Therefore the external feed should be on the decision path even if the initial centre is still the CLOB midpoint.

## 9.2 Open pricing parameters

```text
centre_source
sigma
blend_weight
reaction threshold
latency mapping from spot move -> next Polymarket tick
```

All must remain configurable and measurable.

---

# 10. Queue Priority — Load-Bearing Execution Layer

Polymarket matching is price-time priority.

At the same price:

```text
earlier order fills first
```

A one-tick better order jumps the queue, but paying a full tick can exceed the entire expected edge.

The strategy therefore appears to rely on **pre-emption**:

```text
BTC spot moves
   ->
predict next Polymarket touch
   ->
place at the new level before others
   ->
own the front of a fresh queue
```

## 10.1 Why this matters

Replay evidence showed fill collapse as queue depth ahead increases.

Illustrative reconstructed result:

| Shares ahead | Fills |
|---:|---:|
| 0 | 38 |
| 5 | 30 |
| 15 | 3 |
| 30 | 0 |
| 55 | 0 |

This means queue position is not a secondary optimization.

It is a core part of the strategy.

## 10.2 No systematic tick-paying for priority

Canonical behavior:

```text
queue_improve_depth = 0
```

The bot should not routinely pay one tick merely to jump the queue.

The intended advantage is speed into a new price level.

---

# 11. Post-Only Maker Constraint

This is a hard invariant.

```text
post_only = true
```

The analyzed wallet is reported as:

```text
100% maker
maker fee = 0
```

A taker fill costs far more than the expected edge.

Therefore:

```text
TAKER FILL = EXECUTION BUG
```

unless a future verified strategy revision explicitly changes this rule.

## 11.1 Order placement safety

Before submission:

```text
if BUY UP would cross:
    do not submit at that price

if BUY DOWN would cross:
    do not submit at that price
```

The post-only guarantee must be enforced both locally and, if supported, at the venue API level.

---

# 12. Inventory Grid — 5-Share Lattice

Order sizes are not fixed nominal lots.

The bot sizes orders to land the resulting net inventory on a multiple-of-5 lattice.

Canonical:

```text
GRID = 5 shares
L in {15, 20, 25}
```

## 12.1 Core sizing rule

```text
bid_target = round_to_grid(I + L)
ask_target = round_to_grid(I - L)

bid_size = bid_target - I
ask_size = I - ask_target
```

With boundary correction:

```python
def bid_target(I, L, grid=5.0):
    t = round(I / grid + L / grid) * grid
    if t <= I:
        t += grid
    return t
```

Equivalent logic should be used for the downward target.

## 12.2 Modular fingerprint

Every generated order should satisfy:

```text
bid_size ≡ (-I) mod 5
ask_size ≡ (+I) mod 5
```

This is a mandatory conformance test.

If a generated order fails the fingerprint, the sizing engine is wrong.

## 12.3 True fractional inventory

Inventory must be maintained using actual fill sizes, including partial fills.

Never round inventory to an integer.

Every fill updates:

```text
I += signed_fill_size
```

Then both order sizes must be recomputed.

---

# 13. Base Lot `L`

Observed per-market base lot values:

```text
15
20
25
```

The selection rule is still **OPEN**.

Potential drivers to test:

- realized volatility,
- current account equity,
- time of day,
- market liquidity,
- expected retail flow,
- previous market behavior,
- queue conditions.

Because replay suggests correct `L` materially affects both PnL and winner-residual alignment, `L` must be treated as a first-class strategy parameter.

Do not permanently hard-code `L = 15`.

Initial implementation may use a safe default, but the architecture must expose:

```text
choose_base_lot(market_state)
```

as a replaceable strategy component.

---

# 14. Normal Inventory Management

The earlier analysis observed mean reversion, but the later specification corrected the implementation interpretation.

Canonical later settings:

```text
gamma = 0
band_skew = 0
```

Do not implement an ordinary market-maker inventory-skew rule such as:

```text
C = fair_value - gamma * I
```

as part of the replica.

Replay indicates that explicit restoring skew damages the terminal residual mechanism.

## 14.1 Excursion versus residual

Do not confuse:

```text
intra-market excursion
```

with:

```text
terminal residual
```

Observed inventory excursions can reach around:

```text
±100 shares
```

while typical terminal residual is around:

```text
25-40 shares
```

Current canonical controls:

```text
band ~ 40          observational soft region
band_hard ~ 100    real safety limit
```

The normal quoting regime should allow inventory to roam substantially.

---

# 15. Endgame — Main Profit-Generating Regime

The ENDGAME begins approximately:

```text
T0 + 240s
```

The strategy changes from largely symmetric two-sided quoting to explicit favourite inventory targeting.

This is not optional.

The later replay analysis rejects the idea that the correct favourite residual emerges automatically from symmetric quoting.

## 15.1 Favourite direction

If current UP fair value / quote centre is above 0.50:

```text
favourite = UP
target_I = +endgame_tilt
```

If below 0.50:

```text
favourite = DOWN
target_I = -endgame_tilt
```

Current fitted magnitude:

```text
endgame_tilt = 30 shares
```

Status:

```text
direction/mechanism: CONFIRMED
exact magnitude 30: FITTED
```

## 15.2 Endgame gate

The target must be coupled to an active gate.

Current value:

```text
endgame_band = 5 shares
```

Example logic:

```python
target = +30 if favourite == UP else -30

distance = I - target

bid_allowed = distance < +endgame_band
ask_allowed = distance > -endgame_band
```

In plain terms:

- if inventory is too far below the favourite target, suppress orders that push it further away;
- if inventory is too far above the target, suppress orders that push it further away.

Without a tight endgame gate, a target value can be mathematically present but operationally inert.

---

# 16. Complementary Minting — Structural Profit Mechanic

Near expiry, suppose:

```text
UP = 0.87
DOWN = 0.13
```

A retail trader may buy the cheap longshot:

```text
BUY DOWN @ 0.13
```

The exchange can pair it with:

```text
BUY UP @ 0.87
```

and mint:

```text
1 UP + 1 DOWN
```

from `$1.00` collateral.

No pre-existing seller is required.

This means late retail longshot demand can directly supply the maker with the expensive, likely winning favourite.

The endgame strategy exploits this structure by remaining positioned to receive the favourite through complementary matching.

---

# 17. The True Endgame Success Condition

The bot must not judge endgame success merely from:

```text
I near +30
```

or:

```text
I near -30
```

The real condition is economic.

If UP is favourite:

```text
pnl_if_up = n_up - total_cost - fees + rebate
```

If DOWN is favourite:

```text
pnl_if_down = n_down - total_cost - fees + rebate
```

Therefore the endgame engine should monitor at least:

```text
target direction
inventory distance to target
settlement_edge_favourite
settlement_edge_underdog
incremental cost of acquiring more favourite shares
```

A 30-share favourite residual can still be unprofitable if the preceding matched-pair cost is too large.

---

# 18. Stop Quoting and Settlement

At approximately:

```text
T0 + 280s
```

the bot should:

```text
cancel all remaining orders
place no new orders
hold both outcome balances
```

It should not:

```text
sell
flatten
hedge
merge
convert
stop-loss
```

After the market resolves:

```text
redeem the winning token at $1.00
```

Matched pairs are economically self-liquidating because one side wins and one loses.

## 18.1 Winner source

Do not rely blindly on unsynchronized cached market metadata.

Prefer authoritative resolution information, especially on-chain redemption / resolution evidence when available.

---

# 19. Event-Driven Architecture

The strategy is event-driven, not timer-driven.

Important events:

```text
new BTC spot update
new Polymarket CLOB update
own fill
partial fill
order acknowledgement
order cancellation acknowledgement
phase boundary
market resolution
connection health event
```

Every relevant event may change desired quotes.

## 19.1 Dirty-state / coalescing model

Recommended logical pattern:

```text
event arrives
    ->
update state
    ->
mark strategy dirty
    ->
coalesced decide()
    ->
reconcile desired orders with live orders
```

A fill is particularly important:

```text
fill
 ->
update n_up/n_down
 ->
update cost basis
 ->
update I
 ->
update pnl_if_up/pnl_if_down
 ->
recompute BOTH desired order sizes
 ->
reconcile
```

---

# 20. Queue-Preserving Reconciliation

Do not cancel both quotes after every event.

Every cancellation destroys queue priority.

For each side:

```text
if desired price == live price
and desired size == live size
and order is healthy:
    KEEP ORDER
else:
    replace
```

This is a state reconciliation problem, not a naive cancel-all loop.

## 20.1 No artificial requote throttle

Do not use:

```text
min_requote_ms
```

as a fixed delay before every replacement.

That directly adds latency to the requotes that matter most.

Use an operational rate limiter such as:

```text
token bucket
```

that is free under normal activity and only constrains excess rate.

Current operational suggestion:

```text
max_requotes_per_sec ~ 8
```

Status: **OPERATIONAL**, not proven strategy logic.

---

# 21. Pre-Arm Next Market

Because the market schedule and strike chain are predictable, use the prior window to prepare the next one.

During market `N`:

```text
discover N+1
resolve token IDs
subscribe to next-market feeds
cache metadata
prepare strike
prepare order templates
prepare signatures if architecture allows
warm network connections
```

At `T0`, the critical execution path should contain as little setup work as possible.

The purpose is to reduce:

```text
market-open -> first valid resting quote
```

latency.

---

# 22. Latency Requirements

No absolute target latency is yet proven, but the reconstructed queue behavior makes the following mandatory:

1. Binance / external BTC feed must be consumed asynchronously and immediately.
2. CLOB websocket processing must not be blocked by logging, disk I/O, or heavy analytics.
3. Strategy decision code must avoid unnecessary allocations and blocking calls.
4. Order cancel/replace requests must be issued without serialization where safe.
5. Connection reuse is mandatory.
6. Clock synchronization is mandatory.
7. Every decision, request, acknowledgement, and fill must receive high-resolution timestamps.
8. Slow persistence must be moved off the execution-critical path.
9. REST polling must not be the main live market-data path.
10. Feed reconnect and stale-feed detection are strategy-safety requirements.

The exact acceptable latency distribution is still **OPEN** and must be measured during live paper tests.

---

# 23. Order Execution Invariants

The following are hard invariants unless future evidence explicitly revises them.

```text
INVARIANT 1: post-only always
INVARIANT 2: no intentional taker fills
INVARIANT 3: maximum two live strategy orders per market
INVARIANT 4: one BUY UP + one BUY DOWN in normal two-sided state
INVARIANT 5: zero synthetic spread
INVARIANT 6: order sizes follow the 5-share grid fingerprint
INVARIANT 7: true fractional inventory is authoritative
INVARIANT 8: every fill recomputes both desired sizes
INVARIANT 9: unchanged live orders are preserved
INVARIANT 10: no conventional inventory skew in QUOTE
INVARIANT 11: ENDGAME uses explicit favourite targeting
INVARIANT 12: endgame target must have a binding gate
INVARIANT 13: no pre-settlement flattening
INVARIANT 14: exit by redemption
INVARIANT 15: complete market PnL uses cost of BOTH outcome tokens
```

---

# 24. Required Strategy State

A production implementation should maintain the following per market.

## 24.1 Market state

```text
market_id
slug
T0
window_end
phase
strike K
winner/resolution state
UP token id
DOWN token id
tick
```

## 24.2 External pricing state

```text
BTC spot
spot timestamp
spot sequence/version
CLOB best bid
CLOB best ask
CLOB midpoint
book timestamp
book sequence/version
quote centre C
fair-value confidence
sigma if applicable
```

## 24.3 Position and accounting state

```text
n_up
n_down
cost_up
cost_down
average_up_price
average_down_price
I = n_up - n_down

total_cost
fees
estimated_rebate
pnl_if_up
pnl_if_down
```

## 24.4 Strategy state

```text
grid
base_lot L
normal hard band
endgame_tilt
endgame_band
current favourite
current target_I
bid_allowed
ask_allowed
desired_bid_size
desired_ask_size
desired_up_price
desired_down_price
```

## 24.5 Execution state

```text
live UP order
live DOWN order
order IDs
client order IDs
submitted timestamps
ack timestamps
cancel timestamps
queue_ahead estimate
maker/taker status
replace reason
rate-limit budget
```

---

# 25. Required Telemetry

The strategy cannot be validated without detailed telemetry.

Log every decision with:

```text
market
phase
local monotonic timestamp
exchange timestamp if available
spot
spot age
CLOB best bid/ask
book age
centre
I
n_up
n_down
cost_up
cost_down
total_cost
pnl_if_up
pnl_if_down
favourite
target_I
L
desired UP order
desired DOWN order
existing UP order
existing DOWN order
queue_ahead estimate
action: KEEP / CANCEL / PLACE / REPLACE / SUPPRESS
reason
```

Log every fill with:

```text
side
token
price
size
maker/taker
fee
I before
I after
total cost before
total cost after
pnl_if_up before/after
pnl_if_down before/after
queue ahead before fill
spot at fill
book at fill
```

High-resolution telemetry is mandatory because quote-centre and latency questions remain open.

---

# 26. PnL Health Metrics

Do not use only:

```text
average UP price + average DOWN price
```

That measures matched-pair economics only.

Required per-market metrics:

```text
gross payout
total cost
fees
rebates
net PnL

Term 1
Term 2

n_up
n_down
terminal I
terminal residual side
terminal residual magnitude

pnl_if_up before settlement
pnl_if_down before settlement

maker fraction
taker fills
average queue ahead
fill count
quote count
cancel count
replacement count
stale quote count
```

---

# 27. Edge Budget

The reconstructed total strategy edge is much smaller than one tick.

Approximate evidence:

```text
strategy edge ~ 0.255 cents/share
tick          = 1.000 cents
```

Implication:

A one-tick execution error on a minority of fills can remove the entire expected edge.

Therefore:

```text
latency error
stale quote
taker fill
queue degradation
unnecessary tick improvement
```

are not minor implementation defects.

They can change a positive strategy into a negative one.

---

# 28. Risk Controls

The target strategy does not use a conventional stop-loss.

Current structural risk controls:

```text
post-only
finite order sizes
hard inventory safety bound
short five-minute market duration
no leverage
no pre-settlement forced flattening
controlled number of concurrent markets
```

Current proposed hard inventory limit:

```text
band_hard ~ 100 shares
```

The limit must function as a true safety wall, not as a normal mean-reversion mechanism.

## 28.1 Operational kill switches

A production bot should stop placing new orders if any of the following occurs:

```text
market data stale
external BTC feed stale
CLOB sequence gap unresolved
clock drift exceeds threshold
order state uncertain
maker-only guarantee cannot be established
position state inconsistent
cost ledger inconsistent
API errors exceed threshold
rate-limit state uncertain
market resolution state ambiguous near settlement
```

These are safety controls, not changes to the economic strategy.

---

# 29. Refuted / Forbidden Simplifications

The following should not be implemented as the target strategy.

## 29.1 Fixed order sizes

Wrong:

```text
always quote 15 UP and 15 DOWN
```

Correct:

```text
size to the next inventory-grid target
```

## 29.2 Positive quoted spread

Wrong:

```text
bid = C - delta
ask = C + delta
```

Canonical:

```text
delta = 0
```

## 29.3 Conventional inventory skew

Wrong for the current replica:

```text
C = C - gamma * I
```

Canonical:

```text
gamma = 0
band_skew = 0
```

## 29.4 End flat

Wrong:

```text
flatten inventory before expiry
```

This removes the main profit mechanism.

## 29.5 Residual alone as profitability metric

Wrong:

```text
winner residual positive -> profitable
```

Correct:

```text
winner shares - total cost - fees + rebates -> actual settlement result
```

## 29.6 Timer-based requoting

Wrong:

```text
cancel/replace every fixed interval
```

Correct:

```text
event-driven reconciliation
```

## 29.7 Blind CLOB-following only

Potentially fatal:

```text
wait until CLOB touch moves
then follow
```

The queue advantage appears to require external BTC spot on the decision path.

## 29.8 Routine one-tick queue jumping

Not target behavior.

The expected edge is too small to pay a tick casually.

## 29.9 Hard 0.11-0.89 price cutoff

Wrong.

That band is soft.

## 29.10 Reward-farming logic

Refuted.

No minute-boundary reward-farming strategy was supported.

---

# 30. Canonical Parameter Table

| Parameter | Current value | Status |
|---|---:|---|
| Market universe | `btc-updown-5m-*` | CONFIRMED |
| Market duration | 300 s | CONFIRMED |
| Grid | 5 shares | CONFIRMED |
| Base lot `L` | 15 / 20 / 25 | CONFIRMED |
| Base-lot selection rule | unknown | OPEN |
| Tick | 0.01 | CONFIRMED |
| Synthetic half-spread | 0 | CONFIRMED |
| Post-only | true | CONFIRMED |
| Maker fee | 0 | CONFIRMED |
| Normal inventory `gamma` | 0 | canonical newer spec |
| `band_skew` | 0 | canonical newer spec |
| Soft observed band | ~40 shares | CONFIRMED |
| Hard risk band | ~100 shares | CONFIRMED |
| Start offset | ~3 s | CONFIRMED |
| Endgame start | 240 s | CONFIRMED |
| Stop offset | 280 s | CONFIRMED |
| Endgame tilt | 30 shares | FITTED |
| Endgame gate | 5 shares | mechanism confirmed; magnitude FITTED |
| TWAP window | 60 s | CONFIRMED |
| Centre source | start with CLOB mid | OPEN |
| External BTC feed | required on decision path | architectural requirement |
| Volatility `sigma` | unknown | OPEN |
| Price band | 0.11-0.89 soft | CONFIRMED as soft |
| Queue improvement | 0 ticks | target behavior |
| Max strategy orders | 2 | CONFIRMED |
| Requote limiter | token bucket | OPERATIONAL |
| Example max requote rate | ~8/s | OPERATIONAL |
| Sell | never | CONFIRMED |
| Hedge | never | CONFIRMED |
| Stop-loss | none | CONFIRMED |
| Exit | redeem after resolution | CONFIRMED |
| Fee/rebate calibration | incomplete | OPEN |

---

# 31. Full Strategy State Machine

```text
                +----------------------+
                |        PREARM        |
                | discover next market |
                | chain strike         |
                | subscribe feeds      |
                | warm execution path  |
                +----------+-----------+
                           |
                           | T0 + ~3s
                           v
                +----------------------+
                |        QUOTE         |
                |                      |
                | post-only            |
                | zero spread          |
                | 5-share grid         |
                | gamma = 0            |
                | preserve queue       |
                | event-driven         |
                +----------+-----------+
                           |
                           | T0 + 240s
                           v
                +----------------------+
                |       ENDGAME        |
                |                      |
                | favourite target     |
                | target I ~= +/-30    |
                | gate ~= 5            |
                | still maker-only     |
                +----------+-----------+
                           |
                           | T0 + 280s
                           v
                +----------------------+
                |      SETTLING        |
                | cancel all orders    |
                | hold both balances   |
                +----------+-----------+
                           |
                           | oracle resolution
                           v
                +----------------------+
                |         DONE         |
                | redeem winner @ $1   |
                +----------------------+
```

---

# 32. Decision Function — Canonical Logical Form

```python
def decide(state):
    # 1. Determine phase
    phase = phase_from_time(state.time)

    # 2. Determine quote centre
    centre = compute_centre(
        clob=state.clob,
        spot=state.spot,
        strike=state.strike,
        time_left=state.time_left,
        sigma=state.sigma,
    )

    px_up = round_to_tick(centre)
    px_down = round_to_tick(1.0 - centre)

    # 3. Compute true inventory
    I = state.n_up - state.n_down

    # 4. Grid-based order sizes
    up_target = grid_target_above(I, state.L)
    down_target = grid_target_below(I, state.L)

    up_size = up_target - I
    down_size = I - down_target

    # 5. Eligibility
    up_allowed = True
    down_allowed = True

    if phase == ENDGAME:
        favourite_up = centre > 0.5
        target_I = (
            +state.endgame_tilt
            if favourite_up
            else -state.endgame_tilt
        )

        d = I - target_I

        up_allowed = d < state.endgame_band
        down_allowed = d > -state.endgame_band

    # 6. True hard risk limit
    if I >= state.band_hard:
        up_allowed = False

    if I <= -state.band_hard:
        down_allowed = False

    # 7. Post-only validation
    up_order = build_post_only_up_order(
        px_up, up_size
    ) if up_allowed else None

    down_order = build_post_only_down_order(
        px_down, down_size
    ) if down_allowed else None

    # 8. Economic state is recorded every decision
    settlement_edge_up = (
        state.n_up
        - state.total_cost
        - state.fees
        + state.estimated_rebate
    )

    settlement_edge_down = (
        state.n_down
        - state.total_cost
        - state.fees
        + state.estimated_rebate
    )

    return DesiredState(
        up_order=up_order,
        down_order=down_order,
        settlement_edge_up=settlement_edge_up,
        settlement_edge_down=settlement_edge_down,
    )
```

This is a logical specification, not production code.

The production implementation must additionally handle:

- partial fills,
- race conditions,
- stale acknowledgements,
- order replacement overlap,
- disconnects,
- sequence recovery,
- precision,
- idempotency,
- rate limits,
- client order state.

---

# 33. Execution Reconciliation

```python
def reconcile(desired, live):
    for side in ["UP", "DOWN"]:

        d = desired[side]
        l = live[side]

        if d is None and l is not None:
            cancel(l)
            continue

        if d is None and l is None:
            continue

        if same_price_size_and_valid(d, l):
            keep(l)
            continue

        safe_replace(l, d)
```

Critical rule:

```text
UNCHANGED ORDER -> KEEP QUEUE POSITION
```

Do not cancel merely because a new market-data event arrived.

---

# 34. Verification Ladder Before Real Capital

Implementation should not jump directly from coding to live trading.

## L0 — arithmetic

Must pass:

```text
exact settlement PnL
Term1 + Term2 identity
UP/DOWN hypothetical PnL
5-share modular fingerprint
grid rounding
partial-fill accounting
cost-basis accounting
```

## L1 — historical reconstruction

For known target rounds:

```text
reproduce n_up
reproduce n_down
reproduce cost_up
reproduce cost_down
reproduce Term 1
reproduce Term 2
reproduce total PnL
reproduce redemption amount
```

If arithmetic does not match the known ledger, stop.

## L2 — offline replay

Test:

```text
grid sizing
phase transitions
endgame tilt
endgame gate
band_hard
queue-depth sensitivity
maker-only enforcement
```

## L3 — live paper

Run against the real live market without real orders.

Every target quote/fill opportunity should be classified as:

```text
AT_FRONT
PRICE_OK_BUT_DEEP
OFF_PRICE
NOT_QUOTING
STALE
```

This stage is where the remaining quote-centre and latency questions are closed.

## L4 — minimum-size live

Only after paper execution demonstrates:

```text
maker-only
correct prices
correct grid sizes
stable state machine
low stale rate
reasonable queue position
correct accounting
```

Run one market at a time with minimum capital.

---

# 35. Mandatory Acceptance Criteria

The bot should not be considered strategy-complete until all of the following are true.

## Accounting

- [ ] `PnL_if_UP` and `PnL_if_DOWN` are exact from full dual-token cost.
- [ ] Cost basis survives partial fills and order replacements.
- [ ] Term 1 + Term 2 reproduces settlement PnL.
- [ ] Fees and rebates are separately accounted for.

## Inventory

- [ ] `I = n_up - n_down` uses true fractional shares.
- [ ] Every generated size satisfies the 5-share fingerprint.
- [ ] A fill on either side recomputes both desired sizes.

## Pricing

- [ ] Zero synthetic spread is preserved.
- [ ] Tick rounding is exact.
- [ ] Price band is soft, not hard.
- [ ] Quote centre implementation is configurable.
- [ ] External BTC spot can trigger a decision independently of CLOB events.

## Execution

- [ ] Orders are post-only.
- [ ] No intentional taker fill is possible.
- [ ] Unchanged orders are kept.
- [ ] Queue position is measured.
- [ ] No fixed requote delay is present.
- [ ] Rate limiting does not delay normal critical requotes.
- [ ] Execution path is asynchronous and non-blocking.

## Lifecycle

- [ ] Next market is pre-armed.
- [ ] QUOTE starts near T0+3.
- [ ] ENDGAME begins near T0+240.
- [ ] All orders are canceled near T0+280.
- [ ] Inventory is held through settlement.
- [ ] Winner is redeemed.

## Endgame

- [ ] Favourite direction is explicit.
- [ ] Target inventory is explicit.
- [ ] Endgame gate is active.
- [ ] Normal gamma/band skew are zero.
- [ ] Endgame profitability is evaluated using full total cost, not residual alone.

## Safety

- [ ] Feed staleness halts new orders.
- [ ] Order-state uncertainty halts new orders.
- [ ] Position/accounting inconsistency halts new orders.
- [ ] Hard inventory limit works.
- [ ] Clock/sequence monitoring works.

---

# 36. Highest-Priority Open Research Items

These must remain explicit in the implementation plan.

## OPEN-1 — Quote centre source

Question:

```text
clob_mid vs Binance-derived fair value vs blend?
```

Why it matters:

```text
determines next price level and queue pre-emption
```

Required test:

```text
>= 200 live-paper markets
compare predicted quotes and queue position
```

## OPEN-2 — Volatility `sigma`

Required for TWAP fair value.

Do not guess and freeze it.

## OPEN-3 — Base lot `L` selection

Observed:

```text
15 / 20 / 25
```

Selection rule unknown.

## OPEN-4 — Exact grid target selection

The lattice is confirmed, but target-step behavior may have additional structure.

## OPEN-5 — Exact endgame tilt

`30` is fitted, not universally proven.

## OPEN-6 — Fee/rebate calibration

Maker fee zero is confirmed.

Exact effective rebate model remains incomplete.

## OPEN-7 — Latency requirement

The strategy needs enough speed to pre-empt fresh price levels, but the exact required p50/p95/p99 latency has not yet been established.

---

# 37. Development Principle for the Claude Build

The implementation process should follow this rule:

> **No strategy optimization before replication correctness.**

Do not "improve" the strategy during the first implementation.

Examples of forbidden premature changes:

```text
adding a positive spread
adding inventory skew
flattening inventory
using fixed lots
buying queue priority with ticks
adding timer-based requoting
hard-clipping the price band
removing endgame extreme-price fills
ignoring dual-token cost accounting
```

First reproduce the strategy.

Only after replication metrics are validated should alternative versions be tested as separate experiments.

---

# 38. Recommended Implementation Module Boundaries

The bot should eventually separate at least these responsibilities:

```text
market_discovery/
    next-market discovery
    strike chaining
    token metadata

feeds/
    polymarket_clob_ws
    binance_spot_ws
    clock synchronization

strategy/
    phase machine
    quote centre
    inventory grid
    base-lot selector
    endgame target
    order eligibility

accounting/
    fill ledger
    cost basis
    hypothetical settlement PnL
    Term1/Term2 decomposition
    rebate accounting

execution/
    post-only validation
    live order state
    reconcile
    cancel/replace
    rate limiting
    retries/idempotency

risk/
    hard inventory bound
    stale-feed checks
    state-consistency checks
    kill switch

telemetry/
    event log
    decision log
    latency metrics
    queue metrics
    PnL metrics

replay/
    historical reconstruction
    deterministic event replay
    parameter experiments
```

Keeping these separate is important because several strategy parameters remain open and must be changed without destabilizing execution/accounting.

---

# 39. Final Canonical Interpretation

The target bot does not make money merely because it predicts the winner.

It also does not make money merely because it ends with more winning shares than losing shares.

The mechanism is:

```text
1. Quote both binary outcomes as a synthetic zero-spread Up-space market.
2. Remain post-only and use speed to obtain favourable queue priority.
3. Size every order from true inventory to a 5-share lattice target.
4. Allow substantial inventory movement during most of the market.
5. In the final minute, explicitly target roughly 30 shares of the current favourite.
6. Use the complementary-mint structure to acquire that favourite from opposing longshot flow.
7. Stop quoting before resolution but do not flatten.
8. Hold both outcome balances to settlement.
9. Redeem the winner.
10. Profit only if the winning payout exceeds the entire acquisition cost of BOTH outcome inventories, after fees and rebates.
```

The strategy's central economic test is therefore always:

```text
winner_shares
-
(cost_up + cost_down)
-
fees
+
rebates
```

not:

```text
winner residual > 0
```

and not:

```text
UP average price + DOWN average price < 1
```

That accounting invariant must remain visible throughout implementation, replay, paper trading, live execution, and post-market analysis.

---

# 40. Canonical Build Rule

For the Claude implementation phase, this document should be treated as the strategy source of truth.

Any future implementation decision should be classified as one of:

```text
CONFIRMED strategy rule
FITTED parameter
OPEN research item
OPERATIONAL engineering control
```

If a proposed code change cannot be mapped to one of those categories, it should not be introduced without explicit review.

**Replication correctness first. Latency and precision second. Optimization only after both are proven.**
