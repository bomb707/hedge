# Polymarket BTC 5-Minute Maker Bot — Detailed Trading Strategy by Step and Role

**Purpose:** Systematically describe the trading strategy as an implementation-ready trading system, organized by each operational step and each component role.

**Primary implementation principle:** Replicate the observed strategy accurately and completely before attempting any optimization.

---

# 1. Strategy at the Highest Level

The bot is not fundamentally trying to earn a normal bid/ask spread.

Its process is:

```text
prepare market early
      ↓
quote both outcomes continuously as maker
      ↓
use fast price updates to obtain queue priority
      ↓
use inventory-grid sizing after every fill
      ↓
accept that the matched-pair leg may lose slightly
      ↓
during final ~60 s, intentionally accumulate the favourite
      ↓
stop quoting before expiry
      ↓
hold both sides to resolution
      ↓
redeem winner
      ↓
profit only if winner payout > total cost of BOTH sides
```

The central economic equations are:

If UP wins:

```text
PnL_UP =
n_UP
-
(cost_UP + cost_DOWN)
-
fees
+
rebates
```

If DOWN wins:

```text
PnL_DOWN =
n_DOWN
-
(cost_UP + cost_DOWN)
-
fees
+
rebates
```

Therefore, the bot is **not** successful merely because it holds more shares of the winning side.

---

# 2. Roles Inside the Trading System

The cleanest implementation is to treat the bot as a set of specialized roles.

| Role | Main Responsibility |
|---|---|
| Market Discovery | Find and prepare the next 5-minute market early |
| Market State | Maintain strike, clock, phase, token IDs, resolution state |
| BTC Feed | Detect external BTC movement as quickly as possible |
| CLOB Feed | Maintain the actual Polymarket order book |
| Quote-Centre Engine | Calculate the synthetic UP-space price `C` |
| Inventory Engine | Maintain `I = n_UP - n_DOWN` |
| Grid-Sizing Engine | Convert `I` into exact UP/DOWN order sizes |
| Endgame Engine | Bias inventory toward the current favourite |
| Execution Engine | Maintain post-only orders and preserve queue priority |
| Fill Engine | Process partial/full fills and immediately update state |
| Accounting Engine | Track full dual-token cost and hypothetical settlement PnL |
| Risk Engine | Prevent stale-data trading and extreme inventory |
| Settlement Engine | Cancel, hold, resolve, redeem |
| Telemetry / Research | Measure latency, queue position, PnL, and unresolved parameters |

These roles should be separate in code because several strategy parameters remain unresolved and need independent testing.

---

# 3. Step 0 — Prepare the Next Market Before It Begins

## Role: Market Discovery / Pre-Arm Engine

The bot should not wait for the next market to begin and then start discovering everything.

The markets occur on predictable 300-second boundaries.

The reconstructed strategy also found that:

```text
coinPriceStart[N] = coinPriceEnd[N-1]
```

so the next strike is chained from the preceding market.

## Inputs

The pre-arm component needs:

```text
current market
next market epoch
next market slug
UP token ID
DOWN token ID
strike K
market start T0
market end T0 + 300
CLOB endpoints/subscriptions
```

## Actions During the Previous Market

Before `N+1` begins:

```text
discover market N+1
resolve market/token IDs
prepare subscriptions
prepare strike
warm CLOB connection
warm BTC price connection
prepare internal market state
prepare order structures
prepare signatures where possible
```

## Why This Matters

The target wallet gets its first fills only a few seconds after market open.

Observed first-fill timing is approximately:

```text
T0 + 3 to T0 + 7 seconds
```

Initialization cannot consume those first few seconds.

## Output

At `T0`, the trading system should already have:

```text
MarketState.ready = true
FeedState.ready = true
ExecutionState.ready = true
```

The only remaining work should be live price calculation and order submission.

---

# 4. Step 1 — Initialize the New Market

## Role: Market State Manager

At the start of every five-minute market:

```text
T0 = market start
T_end = T0 + 300
K = strike
```

Initialize positions:

```text
n_UP = 0
n_DOWN = 0

cost_UP = 0
cost_DOWN = 0

I = 0
```

Choose a base lot:

```text
L ∈ {15, 20, 25}
```

The existence of those lot values is confirmed, but the selection rule remains unresolved.

So the architecture should contain:

```python
L = choose_base_lot(market_state)
```

rather than permanently:

```python
L = 15
```

## Current Status of `L`

Confirmed:

```text
L values = 15 / 20 / 25
```

Open:

```text
why one market uses 15
why another uses 20
why another uses 25
```

Potential drivers should be tested later rather than assumed.

---

# 5. Step 2 — Convert the Market into Up-Space

## Role: Synthetic Book / Strategy Abstraction

Polymarket provides two tokens:

```text
UP
DOWN
```

Because:

```text
UP + DOWN = $1 at resolution
```

the system should internally use:

```text
BUY DOWN @ d ≡ SELL UP @ (1 - d)
```

Therefore:

```text
BUY UP @ p
```

means:

```text
synthetic BID UP @ p
```

and:

```text
BUY DOWN @ d
```

means:

```text
synthetic ASK UP @ (1 - d)
```

Net inventory becomes:

```text
I = n_UP - n_DOWN
```

This is the main inventory state.

## Inventory Effect

If an UP order fills:

```text
I ← I + q
```

If a DOWN order fills:

```text
I ← I - q
```

The bot therefore manages one synthetic position rather than two unrelated token inventories.

---

# 6. Step 3 — Receive Two Independent Live Price Feeds

Two feeds play different roles.

## Role A: Polymarket CLOB Feed

The CLOB gives:

```text
best UP bid
best UP ask
book depth
queue depth
price changes
```

The bot must maintain the book continuously through streaming market data rather than slow polling.

## Role B: External BTC Feed

The external BTC feed, such as Binance, is required on the decision path.

Its role is not primarily:

```text
predict BTC direction better
```

The main observed value appears to be:

```text
predict where the Polymarket price will move next
before the Polymarket CLOB visibly moves
```

That is a **latency / queue advantage**.

---

# 7. Step 4 — Calculate the Quote Centre

## Role: Quote-Centre Engine

The system calculates one synthetic UP probability:

```text
C
```

Everything else is derived from `C`.

Three candidate models remain.

## Option 1 — CLOB Midpoint

```text
C = (bestBid_UP + bestAsk_UP) / 2
```

This is the recommended initial implementation because it introduces the fewest unverified assumptions.

## Option 2 — BTC / TWAP Fair Value

Potentially:

```text
C = f(S, K, T, sigma)
```

where:

```text
S = BTC spot
K = strike
T = time remaining
sigma = volatility
```

## Option 3 — Blend

```text
C = w * F + (1 - w) * M
```

where:

```text
F = BTC-derived fair value
M = CLOB midpoint
```

The exact quote-centre method remains one of the largest open items.

---

# 8. Step 5 — Account for TWAP Settlement

## Role: Settlement-Aware Fair-Value Model

The later reconstruction states that the five-minute market settles using a 60-second TWAP mechanism rather than simply the last BTC tick.

Therefore the final minute has less endpoint variance than a normal binary option.

The proposed effective variance is:

```text
Var_eff =
sigma^2 * (T - t - tau)
+
sigma^2 * tau / 3
```

with:

```text
tau = min(60, T - t)
```

Conceptually:

```text
earlier in market:
future endpoint variance dominates

final minute:
TWAP averaging reduces effective variance

near settlement:
probability accelerates toward 0 or 1
```

This matters because the profit-producing inventory is accumulated during this period.

`sigma` remains OPEN and must be fitted.

---

# 9. Step 6 — Generate a Zero-Spread Synthetic Quote

## Role: Pricing Strategy

The canonical target behavior is:

```text
bid_UP = ask_UP = C
```

Therefore:

```text
synthetic spread = 0
```

Translated to venue orders:

```text
BUY UP   @ C
BUY DOWN @ (1 - C)
```

Example:

```text
C = 0.63

BUY UP   @ 0.63
BUY DOWN @ 0.37
```

because:

```text
1 - 0.37 = 0.63
```

The target has therefore created:

```text
synthetic BID UP = 0.63
synthetic ASK UP = 0.63
```

No conventional spread.

---

# 10. Step 7 — Apply Exact Tick Rounding

## Role: Price Normalization

Prices must land on:

```text
tick = 0.01
```

Example:

```text
C_raw = 0.6274
C_quote = 0.63
```

Then:

```text
BUY UP @ 0.63
BUY DOWN @ 0.37
```

---

# 11. Step 8 — Calculate Inventory

## Role: Inventory Engine

The bot must maintain exact fractional inventory.

Never:

```text
I = integer approximation
```

Always:

```text
I = n_UP - n_DOWN
```

Example:

```text
n_UP   = 128.63
n_DOWN = 157.26

I = -28.63
```

Partial fills matter.

If:

```text
BUY UP 13.63
```

fills:

```text
I = -28.63 + 13.63
  = -15.00
```

This behavior is fundamental to the observed order-size fingerprint.

---

# 12. Step 9 — Calculate Grid-Based Order Sizes

## Role: Inventory Grid Engine

This is one of the most strongly confirmed strategy components.

The bot does not normally say:

```text
buy 15 shares
buy 15 shares
```

Instead it asks:

> What order size will move the resulting inventory to a valid 5-share lattice point?

Grid:

```text
GRID = 5
```

Base lot:

```text
L = 15 / 20 / 25
```

Targets:

```text
T_up   = roundTo5(I + L)
T_down = roundTo5(I - L)
```

Sizes:

```text
bidSize = T_up - I
askSize = I - T_down
```

## Example

Current:

```text
I = -28.63
L = 15
```

Candidate upward target:

```text
-15
```

Then:

```text
UP BUY size = -15 - (-28.63)
            = 13.63
```

For DOWN, candidate lower target could be:

```text
-30
```

Then:

```text
DOWN BUY size = -28.63 - (-30)
              = 1.37
```

The unusual decimal quantities are intentional.

---

# 13. Step 10 — Enforce the Modular Fingerprint

## Role: Internal Strategy Validator

Every desired order should satisfy:

```text
bidSize ≡ (-I) mod 5
askSize ≡ (+I) mod 5
```

Example:

```text
I = -28.63
```

Possible UP sizes:

```text
3.63
8.63
13.63
18.63
...
```

Possible DOWN sizes:

```text
1.37
6.37
11.37
16.37
...
```

This should become a unit-test invariant.

If the bot generates:

```text
UP size = 15.00
```

when `I = -28.63`, it has probably implemented the strategy incorrectly.

---

# 14. Step 11 — Apply Normal Inventory Policy

## Role: Normal-Phase Inventory Controller

The observed inventory does show mean-reverting behavior.

However, the later replay analysis suggested that explicitly implementing it with:

```text
C = C - gamma * I
```

damages the terminal profit mechanism.

Therefore canonical newer settings are:

```text
gamma = 0
band_skew = 0
```

During the normal phase, do not aggressively force inventory back toward zero.

---

# 15. Step 12 — Maintain Only a True Hard Risk Boundary

## Role: Risk Engine

The bot distinguishes two different concepts.

### Typical inventory region

Approximately:

```text
±40
```

### Actual observed excursions

Potentially around:

```text
±100
```

### Typical terminal residual

Approximately:

```text
25-40
```

These must not be confused.

The terminal residual is **not** the inventory risk limit.

A reasonable interpretation is:

```text
band ~ 40       observational soft region
band_hard ~ 100 actual safety wall
```

The hard boundary prevents pathological accumulation.

It should not continuously fight ordinary inventory movement.

---

# 16. Step 13 — Check Post-Only Validity

## Role: Execution Safety

Every order must remain maker.

Canonical invariant:

```text
post_only = true
```

Target behavior reportedly shows:

```text
100% Maker
fee = 0
```

Before placing an UP BUY:

```text
ensure it will not cross the UP ask
```

Before placing a DOWN BUY:

```text
ensure it will not cross the DOWN ask
```

If zero-spread strategy intent conflicts with post-only safety:

```text
post-only safety wins
```

The system should either adjust appropriately or refrain from placing that order, and record the deviation.

A taker fill should be treated as a serious execution failure.

---

# 17. Step 14 — Preserve Queue Priority

## Role: Order Reconciliation Engine

Suppose the bot already has:

```text
BUY UP @ 0.63 size 13.63
```

resting in the queue.

A new BTC tick arrives, but the desired quote remains:

```text
0.63 / size 13.63
```

Do not:

```text
cancel
replace
```

Keep it.

Reason:

```text
cancel = lose queue timestamp
```

The correct reconciliation is:

```text
desired == live
    -> KEEP

desired != live
    -> REPLACE
```

This is a critical difference from a simple:

```text
every event -> cancel everything -> replace everything
```

loop.

---

# 18. Step 15 — Use Latency to Pre-Empt New Price Levels

## Role: Queue / Latency Engine

Imagine current CLOB price:

```text
0.62
```

BTC moves.

The bot predicts that Polymarket will soon move to:

```text
0.63
```

At that instant, the new `0.63` queue may still be empty.

If the bot places there first:

```text
queue_ahead ≈ 0
```

When other makers react:

```text
they join behind it
```

This is much more valuable than simply following the CLOB after it changes.

Queue position is therefore part of the strategy, not just an implementation optimization.

---

# 19. Step 16 — Do Not Routinely Pay One Tick for Queue Priority

## Role: Execution Economics

Another way to jump the queue would be:

```text
best bid = 0.62
our bid = 0.63
```

But the reconstructed strategy does not appear to rely on systematically buying priority this way.

Why?

Because:

```text
one tick = 1 cent/share
```

while estimated strategy edge is approximately:

```text
0.255 cents/share
```

So casually paying a tick can consume several times the strategy's expected edge.

Canonical:

```text
queue_improve_depth = 0
```

The bot should attempt to win on **time**, not price concession.

---

# 20. Step 17 — Event-Driven Quote Loop

## Role: Strategy Coordinator

The bot should wake on meaningful events:

```text
BTC spot changes
CLOB changes
our fill arrives
order state changes
phase changes
connection state changes
```

Not:

```text
every fixed 100 ms
every second
every minute
```

Logical structure:

```text
event
 ↓
update state
 ↓
mark dirty
 ↓
decide()
 ↓
reconcile()
```

---

# 21. Step 18 — Process Every Fill Immediately

## Role: Fill Engine

Suppose an UP order partially fills:

```text
price = 0.63
size = 4.72
```

Immediately update:

```text
n_UP += 4.72
cost_UP += 4.72 * 0.63
I += 4.72
```

Then immediately recompute:

```text
total_cost
pnl_if_up
pnl_if_down
new UP target
new DOWN target
new UP size
new DOWN size
```

A fill on one side changes the desired size of **both** orders.

This is a closed inventory-control loop.

---

# 22. Step 19 — Maintain Exact Economic Accounting Continuously

## Role: Accounting Engine

After every fill:

```text
total_cost = cost_UP + cost_DOWN
```

Then:

```text
PnL_UP =
n_UP
-
total_cost
-
fees
+
rebates
```

```text
PnL_DOWN =
n_DOWN
-
total_cost
-
fees
+
rebates
```

Example:

```text
n_UP       = 120
n_DOWN     = 100
cost_UP    = $72
cost_DOWN  = $50

total_cost = $122
```

Then:

```text
UP wins   -> -$2
DOWN wins -> -$22
```

That is the true economic state.

Inventory:

```text
I = +20
```

alone would hide the fact that both settlement states are currently losing.

---

# 23. Step 20 — Track Term 1 and Term 2 Separately

## Role: Strategy Analytics

At settlement:

```text
M = min(n_winner, n_loser)
R = n_winner - n_loser
```

Then:

```text
Term1 =
M * (1 - avgPriceWinner - avgPriceLoser)
```

```text
Term2 =
R * (1 - avgPriceWinner)
```

Total:

```text
PnL = Term1 + Term2
```

The analyzed strategy frequently has:

```text
Term 1 < 0
Term 2 > 0
```

This is why optimizing only pair price or spread would misunderstand the strategy.

---

# 24. Step 21 — Enter ENDGAME Around T0+240

## Role: Phase Controller

At approximately:

```text
t = 240 s
```

switch:

```text
QUOTE -> ENDGAME
```

Normal market making no longer fully describes the goal.

The objective becomes:

> Enter settlement holding a controlled residual of the current favourite.

---

# 25. Step 22 — Determine the Current Favourite

## Role: Endgame Direction Engine

Using the current quote centre / fair value:

```text
if C > 0.50:
    favourite = UP

if C < 0.50:
    favourite = DOWN
```

Then define:

```text
target_I = +30 for UP
target_I = -30 for DOWN
```

Current fitted parameter:

```text
endgame_tilt = 30
```

The exact value is FITTED, not fully confirmed across a huge sample.

The existence of explicit favourite targeting is the important part.

---

# 26. Step 23 — Apply the Endgame Gate

## Role: Endgame Inventory Controller

A target without a gate does nothing.

Suppose:

```text
favourite = UP
target_I = +30
endgame_band = 5
```

The desirable region is approximately:

```text
+25 <= I <= +35
```

If:

```text
I = +5
```

allow UP-buying pressure, but restrict activity that pushes inventory further negative.

If:

```text
I = +38
```

suppress further UP accumulation and allow DOWN fills to pull inventory back.

Conceptually:

```python
d = I - target_I

up_allowed   = d < +5
down_allowed = d > -5
```

This gate is the mechanism that makes the target operational.

---

# 27. Step 24 — Use Complementary Mint Flow

## Role: Venue-Microstructure Profit Engine

Near expiry:

```text
UP   = 0.92
DOWN = 0.08
```

Retail traders may buy:

```text
DOWN @ 0.08
```

as a longshot.

Polymarket can match this with:

```text
UP BUY @ 0.92
```

and mint a new UP/DOWN pair from `$1`.

The maker therefore receives the likely winner without requiring an existing UP seller.

Flow:

```text
retail buys cheap loser
        ↓
pair is minted
        ↓
bot receives expensive favourite
        ↓
favourite settles at $1
```

This complementary minting is central to the late-market inventory accumulation.

---

# 28. Step 25 — Do Not Mistake Favourite Inventory for Guaranteed Profit

## Role: Economic Guardrail

Suppose ENDGAME reaches:

```text
n_UP - n_DOWN = +30
```

and UP wins.

That does **not** imply success.

The only valid calculation is:

```text
PnL =
n_UP
-
cost_UP
-
cost_DOWN
-
fees
+
rebates
```

Example:

```text
n_UP = 130
n_DOWN = 100

total cost = $131
```

UP wins:

```text
payout = $130
PnL = -$1
```

So endgame control has two separate goals.

### Inventory Objective

```text
move I toward favourite target
```

### Economic Objective

```text
do not acquire that residual so expensively that total market PnL becomes negative
```

The full dual-token cost must always be visible.

---

# 29. Step 26 — Continue Maker-Only Execution During Endgame

ENDGAME is not:

```text
market buy 30 favourite shares
```

That would fundamentally change the strategy.

The bot remains:

```text
post-only
maker
grid-based
event-driven
```

but selectively suppresses the side that moves inventory away from the endgame target.

The endgame modifies **order eligibility**, not the basic execution style.

---

# 30. Step 27 — Stop Quoting Around T0+280

## Role: Phase Controller / Execution Engine

At approximately:

```text
T0 + 280 s
```

cancel outstanding orders.

Transition:

```text
ENDGAME -> SETTLING
```

Do not submit new orders.

The bot now holds:

```text
n_UP
n_DOWN
```

through resolution.

---

# 31. Step 28 — Never Flatten Before Settlement

## Role: Settlement Strategy

The target reportedly does not:

```text
SELL
hedge
merge
split
conversion
stop-loss
```

during this strategy.

The correct behavior is:

```text
cancel orders
hold balances
wait
```

Why?

Matched pairs are naturally self-liquidating economically:

```text
1 UP + 1 DOWN
```

always pays:

```text
$1 total
```

Only the residual determines directional settlement exposure.

Flattening would remove the main Term 2 mechanism.

---

# 32. Step 29 — Resolve the Market Safely

## Role: Resolution Engine

The system must determine:

```text
UP wins
or
DOWN wins
```

Do not rely blindly on stale or unsynchronized cached market metadata.

Resolution authority should favor reliable final/on-chain information.

---

# 33. Step 30 — Redeem the Winner

## Role: Settlement Engine

If UP wins:

```text
UP tokens -> $1 each
DOWN -> $0
```

If DOWN wins:

```text
DOWN tokens -> $1 each
UP -> $0
```

Then compute exact realized result:

```text
winner payout
-
cost_UP
-
cost_DOWN
-
fees
+
rebates
```

No alternative PnL equation should be used.

---

# 34. Step 31 — Post-Market Decomposition

## Role: Analytics / Research

For each market record:

```text
n_UP
n_DOWN
cost_UP
cost_DOWN
total_cost

winner

Term 1
Term 2

gross payout
fees
rebates
net PnL

terminal I
terminal residual
residual winner/loser

number of fills
maker percentage
queue ahead
requotes
cancel count
latency
stale fills
```

A profitable or losing market by itself does not tell you why.

---

# 35. Step 32 — Measure Execution Quality Separately from Strategy Quality

A strategy can calculate correct prices and still lose because execution is poor.

Use live-paper classifications such as:

## `AT_FRONT`

Correct price and strong queue position.

Best outcome.

## `PRICE_OK_BUT_DEEP`

Strategy calculation is right.

Execution / latency is wrong.

## `OFF_PRICE`

Pricing model is wrong.

Investigate:

```text
quote centre
sigma
spot-to-CLOB mapping
```

## `NOT_QUOTING`

Strategy gate, phase, or risk logic prevented participation.

This separation is extremely useful during development.

---

# 36. Step 33 — Latency Role in Practical Implementation

## Role: Fast Execution Infrastructure

The critical path is:

```text
BTC update received
        ↓
decode
        ↓
update fair state
        ↓
decide()
        ↓
generate desired order
        ↓
compare with live order
        ↓
submit replace
        ↓
venue receives
```

Every unnecessary millisecond belongs here.

Things that should **not** sit on this path:

```text
database writes
verbose logging
historical analytics
heavy dataframe operations
blocking HTTP
slow JSON transformations
synchronous disk I/O
```

They should happen asynchronously.

---

# 37. Step 34 — Rate Limiting Without Intentionally Adding Latency

## Role: Transport Control

Do not use a fixed:

```text
min_requote_ms
```

delay before every replacement.

That penalizes every important price change.

Instead use something like:

```text
token bucket
```

that only limits activity when the actual venue budget is exceeded.

Example operational setting:

```text
~8 replacements / second
```

This number is operational, not a proven target-wallet constant.

---

# 38. Step 35 — Risk Engine Must Detect Stale State

The bot should refuse to create new risk when it cannot trust its own state.

Examples:

```text
BTC feed stale
CLOB feed stale
CLOB sequence gap
clock drift
unknown open-order state
position mismatch
cost ledger mismatch
unexpected taker fill
rate-limit uncertainty
network disconnect
resolution ambiguity
```

If one occurs:

```text
stop new quoting
reconcile state
resume only when safe
```

Accuracy is more important than continuing to trade.

---

# 39. Role Interaction During a Normal Fill

A single fill shows how the whole architecture connects.

Suppose:

```text
I = -28.63
L = 15
C = 0.62
```

The sizing engine creates:

```text
UP size = 13.63
```

and it fills at:

```text
UP @ 0.62
```

## Fill Engine

Updates:

```text
n_UP += 13.63
cost_UP += 13.63 * 0.62
```

## Inventory Engine

Updates:

```text
I:
-28.63 -> -15.00
```

## Accounting Engine

Recomputes:

```text
total_cost
pnl_if_UP
pnl_if_DOWN
```

## Grid Engine

Recomputes both:

```text
next UP size
next DOWN size
```

## Pricing Engine

Checks whether `C` changed.

## Execution Engine

For each existing order:

```text
unchanged -> KEEP
changed -> REPLACE
```

## Queue Engine

Measures how much queue priority was preserved or lost.

All of this should happen from one fill event.

---

# 40. Role Interaction During ENDGAME

Suppose:

```text
t = 255 s
C = 0.91
UP favourite
I = +12
target = +30
gate = 5
```

The target region is:

```text
+25 to +35
```

The bot is too far below it.

## Endgame Engine

Produces:

```text
favourite = UP
target_I = +30
```

## Eligibility Engine

Allows inventory to rise.

May suppress the side that would push inventory further negative.

## Pricing Engine

Still calculates the same zero-spread synthetic centre.

## Grid Engine

Still sizes orders to inventory targets.

## Execution Engine

Still uses post-only orders.

## Accounting Engine

Continuously calculates:

```text
pnl_if_UP
pnl_if_DOWN
```

The strategy is therefore still a maker, but now with explicit terminal inventory intent.

---

# 41. Economic Purpose of Each Major Subsystem

## Quote-Centre Engine

Purpose:

```text
be at the correct price
```

Without it:

```text
adverse selection
stale fills
wrong queue levels
```

## Latency Engine

Purpose:

```text
arrive before competing makers
```

Without it:

```text
correct price but poor fills
```

## Inventory Grid

Purpose:

```text
control the path of inventory in precise increments
```

Without it:

```text
state diverges after partial fills
```

## Endgame Controller

Purpose:

```text
create Term 2
```

Without it:

```text
bot tends toward matched-pair economics only
```

## Post-Only Execution

Purpose:

```text
avoid destroying the tiny edge with taker costs
```

## Accounting Engine

Purpose:

```text
know whether the position is actually profitable
```

Without it:

```text
"winner residual" can be mistaken for profit
```

## Settlement Engine

Purpose:

```text
preserve and realize Term 2
```

Flattening too early destroys it.

---

# 42. What the Bot Is Not Doing

Based on the source reconstruction, do not reinterpret the strategy as:

## Traditional Arbitrage

Not simply:

```text
UP price + DOWN price < 1
```

## Classic Spread Capture

The matched-pair leg can lose.

## Directional BTC Forecasting

There is no strong evidence that the bot possesses a superior directional BTC model.

## Normal Inventory-Neutral Market Making

It intentionally allows nonzero terminal inventory.

## Reward Farming

Earlier reward/minute-boundary hypotheses were refuted.

## Stop-Loss Trading

No conventional stop loss was observed.

## Hedging

The target holds both token balances to settlement.

---

# 43. The Three Most Important Sources of Edge

From the reconstruction, rank them conceptually as:

## 1. Queue Position / Execution Speed

If you arrive behind existing liquidity, the strategy can lose its fill flow.

## 2. Endgame Favourite Residual

This creates the positive Term 2 that compensates for the weak or negative matched-pair leg.

## 3. Maker Economics / Rebate

The maker rebate adds incremental profitability and maker-only execution avoids large taker costs.

The strategy does **not** appear to have a large per-trade mathematical margin.

Its edge is very thin.

---

# 44. The Three Most Dangerous Implementation Errors

## Error 1 — Correct Strategy, Slow Execution

Result:

```text
PRICE_OK_BUT_DEEP
```

You obtain the worst subset of fills or none.

## Error 2 — Correct Winner, Wrong Cost Accounting

Result:

```text
hold winner
still lose money
```

because:

```text
winner payout < cost_UP + cost_DOWN
```

## Error 3 — Conventional "Risk Improvement"

Examples:

```text
flatten to zero
inventory skew
positive spread
hard price band
```

These may look sensible in ordinary market making but alter the observed strategy.

---

# 45. Recommended Role Hierarchy for Implementation

```text
Bot
│
├── MarketCoordinator
│   ├── MarketDiscovery
│   ├── StrikeChain
│   └── PhaseMachine
│
├── MarketData
│   ├── PolymarketBookFeed
│   ├── BinanceSpotFeed
│   └── ClockSynchronizer
│
├── StrategyEngine
│   ├── QuoteCentre
│   ├── TWAPFairValue
│   ├── InventoryState
│   ├── GridSizer
│   ├── BaseLotSelector
│   └── EndgameController
│
├── AccountingEngine
│   ├── PositionLedger
│   ├── CostBasis
│   ├── SettlementPnL
│   ├── Term1Term2
│   └── RebateLedger
│
├── ExecutionEngine
│   ├── PostOnlyGuard
│   ├── DesiredOrderBuilder
│   ├── OrderReconciler
│   ├── QueueTracker
│   ├── CancelReplace
│   └── RateLimiter
│
├── RiskEngine
│   ├── HardInventoryLimit
│   ├── FeedStaleness
│   ├── PositionConsistency
│   ├── ClockHealth
│   └── KillSwitch
│
├── SettlementEngine
│   ├── ResolutionVerifier
│   └── Redeemer
│
└── Telemetry
    ├── DecisionLog
    ├── FillLog
    ├── LatencyMetrics
    ├── QueueMetrics
    ├── PnLMetrics
    └── ReplayRecorder
```

That separation is important because strategy logic and execution mechanics must not become tangled together.

---

# 46. Complete Five-Minute Round in Compact Form

A full round should effectively look like this:

```text
T0-previous window
    discover next market
    know strike
    warm feeds/connections

T0
    market starts

T0+~3s
    initialize I=0
    choose L
    compute centre C
    generate grid-sized UP/DOWN orders
    enforce post-only
    place both

T0+3s ... T0+240s
    BTC/CLOB event
        -> recompute C if needed
        -> recompute desired prices
        -> preserve unchanged queue slots

    fill
        -> update n_UP/n_DOWN
        -> update costs
        -> update I
        -> update pnl_if_UP/pnl_if_DOWN
        -> recompute BOTH order sizes
        -> reconcile

T0+240s
    enter ENDGAME
    determine favourite
    target I ≈ +/-30
    activate ~5-share gate

T0+240 ... 280s
    continue maker quoting
    preferentially allow fills moving I toward favourite target
    continue full-cost PnL monitoring

T0+280s
    cancel all

T0+280 ... resolution
    hold UP + DOWN
    no flatten
    no hedge

resolution
    verify winner
    redeem

post-market
    calculate exact PnL
    calculate Term1 / Term2
    analyze latency
    analyze queue
    analyze deviations
```

---

# 47. Central Strategy Logic

If you strip away all infrastructure, the core decision logic is approximately:

```python
I = n_up - n_down

C = quote_centre(...)

up_price = round_tick(C)
down_price = round_tick(1 - C)

up_target = next_grid_target_above(I, L)
down_target = next_grid_target_below(I, L)

up_size = up_target - I
down_size = I - down_target

if phase == QUOTE:
    up_allowed = True
    down_allowed = True

elif phase == ENDGAME:
    target = +30 if C > 0.5 else -30

    d = I - target

    up_allowed = d < +5
    down_allowed = d > -5

if I >= band_hard:
    up_allowed = False

if I <= -band_hard:
    down_allowed = False
```

Then execution decides whether the desired orders should be kept, replaced, or suppressed.

---

# 48. Economic State Must Run Alongside Strategy State

At every iteration:

```python
total_cost = cost_up + cost_down

pnl_if_up = (
    n_up
    - total_cost
    - fees
    + estimated_rebate
)

pnl_if_down = (
    n_down
    - total_cost
    - fees
    + estimated_rebate
)
```

So the strategy always knows both possible settlement outcomes.

That should be a **first-class state**, not merely a post-trade report.

---

# 49. Confirmed vs Fitted vs Open

## Strongly Established

```text
binary Up-space identity
buy-only structure
net inventory definition
5-share lattice
fractional grid sizing
zero synthetic spread
one-cent tick
post-only maker behavior
no traditional exit
settlement redemption
explicit ENDGAME regime
favourite residual mechanism
importance of queue priority
```

## Fitted / Incomplete

```text
exact endgame_tilt = 30
exact endgame_band = 5
some hard-band magnitudes
```

## Still Open

```text
exact quote centre
exact sigma
exact rule selecting L
some grid-step selection behavior
exact rebate calibration
precise latency required to reproduce target queue performance
```

---

# 50. Most Important Implementation Philosophy

For the implementation phase, impose one rule above everything else:

> **First replicate the observed strategy exactly. Do not optimize it while implementing it.**

Do not independently decide that:

```text
a positive spread is safer
inventory skew is more rational
flattening reduces risk
fixed lots are simpler
a hard 0.11-0.89 band is cleaner
periodic requoting is easier
```

Those may be sensible ideas for another strategy, but they would create a different bot.

The correct order is:

```text
1. exact accounting
2. exact state machine
3. exact grid logic
4. exact maker constraints
5. exact endgame behavior
6. exact event handling
7. latency/queue validation
8. paper replication
9. minimum-size live validation
10. only then experiment with improvements
```

That gives the implementation a reliable foundation because every role can be reviewed independently and proven to match the strategy before moving to the next layer.
