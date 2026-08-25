# Architecture SSOT

Single source of truth for **structure**. The strategy SSOT is
`strategy/Polymarket_5m_Maker_Bot_Canonical_Strategy_Spec.md`; this document never
overrides it. Rules live in `INVARIANTS.md`; schedule lives in `DEVELOPMENT_PLAN.md`.

Sections are independently readable. Read only the one you are working in.

- §1 Design forces
- §2 The three planes
- §3 Event flow
- §4 Component catalogue
- §5 Concurrency and ownership
- §6 Numeric contract (target for P1)
- §7 Determinism and replay
- §8 Module map
- §9 Configuration and parameter labelling
- §10 Ambiguities resolved by precedence

---

## §1 Design forces

Priority order for every design decision in this repository:

```text
1. exact strategy correctness
2. deterministic accounting and state correctness
3. maker-only / post-only safety
4. queue position preservation
5. minimum event-to-order latency
6. reliable recovery and state reconciliation
7. maintainability and observability
8. UI functionality
```

When two of these conflict, the lower number wins. The ordering is unusual — most systems
put maintainability above latency — and it is deliberate: the reconstructed total edge is
~`0.255` cents/share against a `1.000` cent tick (Canonical §27), and fill rate collapses
as queue depth ahead grows (Canonical §10.1). Latency and queue position are therefore
**strategy properties**, not optimizations, and the architecture must not trade them away
for convenience.

---

## §2 The three planes

The system is split into three planes by *latency class and mutability*, not by feature.

```text
┌─────────────────────────────────────────────────────────────────────┐
│ PLANE 1 - HOT EXECUTION                          budget: microseconds│
│                                                                      │
│  feed decode -> state ingestion -> action dispatch -> transport write │
│                                                                      │
│  Single owner thread. No locks. No allocation beyond the necessary.   │
│  Owns the ONLY mutable copy of MarketState.                          │
│  May call into Plane 2 synchronously.                                │
│  Never calls into Plane 3 synchronously.                             │
└──────────────────────────┬───────────────────────────────────────────┘
                           │ calls (synchronous, pure, in-thread)
                           v
┌─────────────────────────────────────────────────────────────────────┐
│ PLANE 2 - STRATEGY / ACCOUNTING                  budget: microseconds│
│                                                                      │
│  AccountingEngine.apply(fill)      -> new ledger state               │
│  StrategyEngine.decide(state)      -> DesiredOrders                  │
│  OrderReconciler.diff(desired,live)-> minimal ExecutionActions       │
│                                                                      │
│  PURE and DETERMINISTIC. No I/O, no clock reads, no randomness,      │
│  no logging, no exceptions used for control flow.                    │
│  Identical code runs in production and in replay.                    │
└──────────────────────────┬───────────────────────────────────────────┘
                           │ publishes immutable snapshots / events
                           │ (non-blocking, bounded queue)
                           v
┌─────────────────────────────────────────────────────────────────────┐
│ PLANE 3 - CONTROL / UI / PERSISTENCE          budget: milliseconds+  │
│                                                                      │
│  telemetry sinks, database, dashboards, UI, research, analytics,     │
│  post-market decomposition, operator commands                        │
│                                                                      │
│  Reads only immutable snapshots. Holds NO lock that Plane 1 or 2     │
│  can ever wait on. May be slow, may fall behind, may be killed.      │
└─────────────────────────────────────────────────────────────────────┘
```

### §2.1 The hard rules between planes

1. **Plane 3 never blocks Planes 1-2.** No shared mutex, no synchronous callback, no
   unbounded queue that causes back-pressure into the loop. The queue between Plane 1 and
   Plane 3 is bounded; on overflow it **drops and counts drops** rather than blocking.
   A dropped telemetry record is an observability incident; a blocked hot loop is a
   trading incident. (I19)
2. **Plane 2 never performs I/O.** Not even logging. It returns decisions *and* the
   telemetry record describing them; Plane 1 hands that record to Plane 3.
3. **Plane 2 never reads a clock.** Time enters as a field on the event. This is what makes
   replay exact. (I20)
4. **Only Plane 1 mutates MarketState.** Everything outside sees frozen snapshots.
5. **Killing Plane 3 must not stop trading.** If the dashboard, database, or UI process
   dies, the bot keeps quoting. The converse does not hold: if Plane 1 halts, Plane 3 must
   surface it loudly.

### §2.2 Explicitly forbidden on the hot path

```text
database persistence      dashboard rendering      historical analytics
heavy logging             blocking HTTP            pandas/dataframe work
slow serialization        synchronous disk I/O     research calculations
```

---

## §3 Event flow

The target flow, which the production hot path must be structurally equivalent to:

```text
External BTC event ─┐
Polymarket book event ─┤
Own fill ──────────────┤
Order state event ─────┤
Phase event ───────────┘
         ↓
   authoritative MarketState          (Plane 1 mutates; single owner)
         ↓
   StrategyEngine.decide()            (Plane 2, pure)
         ↓
   DesiredOrders                      (value object; immutable)
         ↓
   OrderReconciler.diff()             (Plane 2, pure)
         ↓
   ExecutionEngine                    (Plane 1; minimum required action only)
```

### §3.1 The five hot-path event kinds

| Event | Source | Primary state effect |
|---|---|---|
| `SpotTick` | external BTC feed | spot, spot_ts, spot_seq → may move centre; **must be able to wake `decide()` alone** (I11) |
| `BookUpdate` | Polymarket CLOB websocket | best bid/ask, depth, queue estimate → may move centre and post-only validity |
| `OwnFill` | Polymarket user channel | `n_up`/`n_down`, cost basis, `I`, both PnL branches, **both** desired sizes (I08) |
| `OrderStateEvent` | Polymarket user channel | ack / reject / cancel-ack / partial → live order table |
| `PhaseEvent` | clock source in Plane 1 | PREARM → QUOTE → ENDGAME → SETTLING → DONE |

Health events (feed staleness, sequence gap, clock drift, rate-limit state) enter the same
ordered stream so that a halt is replayable like any other decision.

### §3.2 Dirty-flag coalescing

Per Canonical §19.1, an event marks strategy state dirty and a coalesced `decide()` runs.
Coalescing must **not** be implemented as a timer (I10). It exists only to avoid running
`decide()` twice for two events already in the same batch drained from the socket. If the
queue is empty, `decide()` runs immediately — coalescing must never delay a lone event.

### §3.3 Minimum required execution action

`OrderReconciler.diff()` returns the *smallest* action set that makes live match desired:

```text
desired is None,  live exists          -> CANCEL
desired is None,  live is None         -> nothing
desired == live and live is healthy    -> KEEP        (I09 - queue priority preserved)
otherwise                              -> REPLACE
```

`KEEP` is the default and the common case. Any design in which a market-data event
routinely produces `CANCEL` is wrong.

---

## §4 Component catalogue

Each component states its plane, what it owns, and what it must never do.

### MarketCoordinator — Plane 1
Owns the market lifecycle and is the top-level event pump.
- Sub-parts: `MarketDiscovery`, `StrikeChain`, `PhaseMachine`.
- Discovers market `N+1` **during** market `N` and pre-arms it: token IDs, subscriptions,
  strike (`coinPriceStart[N] = coinPriceEnd[N-1]`), warm connections, prepared order
  templates and signatures where the architecture allows (Canonical §21, Detailed §3).
- Drives the phase machine: `PREARM → QUOTE (T0+~3s) → ENDGAME (T0+240s) →
  SETTLING (T0+280s) → DONE`.
- Must never: do discovery work inside the opening seconds of a market; make strategy
  decisions.

### MarketData — Plane 1
- `PolymarketBookFeed` — streaming book maintenance, sequence tracking, gap detection.
  REST is for recovery and reconciliation only, never the main live path (Canonical §22).
- `BinanceSpotFeed` — external BTC spot, consumed asynchronously and immediately; on the
  decision path (I11).
- `ClockSynchronizer` — monotonic clock for intervals, drift monitoring against exchange
  timestamps; drift beyond threshold is a kill-switch input.
- Must never: block on decode, allocate per-tick dataframes, or hand mutable buffers to
  Plane 2.

### StrategyEngine — Plane 2 (pure)
`decide(state) -> DesiredOrders`. The single place the strategy exists.
- Sub-parts: `QuoteCentre`, `TWAPFairValue`, `GridSizer`, `BaseLotSelector`,
  `EndgameController`, `EligibilityGate`.
- `QuoteCentre` is a **replaceable strategy component** behind one interface, because the
  centre source is OPEN (O01). Initial: `clob_mid`.
- `BaseLotSelector` is likewise replaceable — `choose_base_lot(market_state)`, never a
  frozen `L = 15` (I18, O03).
- Produces zero synthetic spread (I05), grid-fingerprint sizes (I04), endgame eligibility
  (I14), and hard-band suppression (I17).
- Must never: read a clock, perform I/O, log, or know that a venue exists.

### AccountingEngine — Plane 2 (pure)
- Sub-parts: `PositionLedger`, `CostBasis`, `SettlementPnL`, `Term1Term2`, `RebateLedger`.
- Applies fills at full fractional precision (I03) and maintains `n_up`, `n_down`,
  `cost_up`, `cost_down`, `fees`, `estimated_rebate`, `total_cost`, `pnl_if_up`,
  `pnl_if_down` as live first-class state (I01).
- `Term1Term2` is an analytic view that must reconcile exactly with the settlement
  accounting for every market.
- Must never: approximate, round inventory to integers, or treat residual as profit.

### ExecutionEngine — Plane 1
- Sub-parts: `PostOnlyGuard`, `DesiredOrderBuilder`, `OrderReconciler` (the diff itself is
  Plane 2 and pure), `LiveOrderTable`, `CancelReplace`, `RateLimiter` (token bucket),
  `QueueTracker`.
- Enforces post-only locally *and* at the venue API where supported (I06); an intentional
  taker fill is a defect and a risk event (I07).
- Preserves queue priority by keeping unchanged orders (I09); no fixed requote delay (I10).
- Maintains client order IDs, idempotency, retry, and in-flight/ack reconciliation so that
  "unknown order state" is a detectable condition rather than a silent divergence.
- Must never: cancel because a market-data event merely arrived.

### RiskEngine — Plane 1 (checks) + Plane 2 (hard band)
- The `band_hard` inventory limit is a **pure eligibility input** and belongs in Plane 2 so
  replay reproduces it.
- Everything else is Plane 1 environmental health: feed staleness, CLOB sequence gaps,
  clock drift, order-state uncertainty, position/ledger inconsistency, API error rate,
  rate-limit uncertainty, ambiguous resolution near settlement.
- Any trip **stops new orders** and reconciles; it does not change the economic strategy
  (I17, Canonical §28.1).

### SettlementEngine — Plane 1 / Plane 3 boundary
- `ResolutionVerifier` — authoritative winner determination, preferring on-chain
  resolution/redemption evidence over cached metadata (I16). Source selection is OPEN (O11).
- `Redeemer` — redeem the winner. Never sell, hedge, merge, split, convert, or flatten
  (I15). Redemption is not latency-critical and must not sit in Plane 1's loop.

### Telemetry — Plane 3
- `DecisionLog`, `FillLog`, `LatencyMetrics`, `QueueMetrics`, `PnLMetrics`,
  `ReplayRecorder`.
- Receives **records constructed in Plane 2**, over a bounded non-blocking queue.
- `ReplayRecorder` writes the canonical ordered event journal that P5 replays. Its format
  is a first-class contract, not a debug artifact.
- Must never: be on a code path Plane 1 awaits.

### Replay — offline
- Reads the recorded event journal and drives the **same** Plane 2 code (I20).
- Provides parameter sweeps for OPEN items by re-running the identical journal under
  different configs.
- Must never: contain a second implementation of any strategy rule. A replay-only branch
  inside strategy code is a defect.

### UI — Plane 3
- Read-only view of published snapshots plus a narrow, explicit command channel
  (e.g. halt, resume, kill-switch) that enqueues a control event into the ordered stream —
  never a direct mutation of trading state.
- Must never: hold a lock, or be required for trading to run.

---

## §5 Concurrency and ownership

```text
MarketState              mutated by Plane 1 only, single owner, never shared mutably
DesiredOrders            immutable value object produced by Plane 2
LiveOrderTable           mutated by Plane 1 only (ExecutionEngine)
Ledger                   mutated by Plane 1 applying Plane 2's pure result
Snapshots                immutable, published to Plane 3
```

- One asyncio event loop owns Planes 1 and 2 for the trading path. Strategy code is
  synchronous inside it — `decide()` must complete far inside a single loop tick and must
  never `await`.
- Plane 3 runs in separate tasks, threads, or processes fed by bounded queues.
- Because state is single-owner, there are **no locks on the trading path**. If a design
  needs a lock there, the ownership model has been broken.
- Concurrent markets: one `MarketCoordinator` instance per market, sharing feeds and the
  execution transport. Cross-market state is limited to risk aggregation and rate-limit
  budget.

---

## §6 Numeric contract (target for P1 — not implemented in P0)

Binary floating point is not acceptable for inventory, cost, or price on the hot path:
`0.01` is not representable, accumulated `+=` over hundreds of partial fills drifts,
equality comparison for order reconciliation becomes approximate, and replay stops being
bit-exact. All four are direct invariant violations (I01, I03, I09, I20).

### §6.1 Domain types

Three distinct integer newtypes. They are not interchangeable and must not be implicitly
mixed; the type checker is expected to enforce this.

| Type | Represents | Scale | Notes |
|---|---|---|---|
| `PriceTicks` | a quotable price level | integer count of the market's tick | `tick = 0.01` CONFIRMED (Canonical §8.2). `0.63 -> 63`. Grid rounding, level identity, and reconciliation equality all operate here. |
| `ShareUnits` | a quantity of outcome tokens | integer sub-shares, `SHARE_SCALE` per share | Carries true fractional fills (I03). Signed; net inventory `I` is a `ShareUnits`. |
| `MoneyUnits` | USD cost / PnL / fees / rebates | integer sub-dollars, `MONEY_SCALE` per dollar | Ledger arithmetic only. |

`SHARE_SCALE` and `MONEY_SCALE` are fixed at P1 and then frozen; changing them later
invalidates recorded journals. Their concrete values depend on venue precision, which is
**OPEN (O10)** — P1 must pick them from measured venue behaviour, not from a guess.

### §6.2 Scaling policy

```text
price_money_per_share = PriceTicks * tick_size_money      (exact, integer)
cost_delta            = fill_size_shares * price_money_per_share / SHARE_SCALE
```

- Every multiplication that reduces scale uses an **explicit, documented rounding mode**,
  applied once, at a named boundary. Implicit rounding anywhere is a defect.
- Cost accrual rounding direction must be chosen so the ledger is conservative and
  *reproducible*; the same inputs must always give the same last unit.
- No intermediate ever becomes `float`. `Decimal` is acceptable in Plane 3 analytics but
  forbidden on the hot path (allocation and speed).

### §6.3 Exactness contract

1. Every venue-reported price and quantity must be **exactly representable** in these
   types. A value that is not representable is a **hard error** that halts new quoting —
   never a silent round. This is how a wrong `SHARE_SCALE` gets detected instead of
   quietly corrupting the ledger.
2. The 5-share lattice is exact: `GRID = 5 * SHARE_SCALE`. The modular fingerprint (I04)
   is integer modular arithmetic and is therefore exactly testable.
3. Order reconciliation equality is integer equality on `(PriceTicks, ShareUnits)` — fast
   and unambiguous (I09).
4. `pnl_if_up`, `pnl_if_down`, `Term1`, `Term2` are computed in `MoneyUnits` and must
   satisfy `Term1 + Term2 == settlement PnL` exactly (I01).
5. Float appears only at presentation boundaries: UI, logs, research. Never back-converted
   into state.

### §6.4 Where floats remain legitimate

The quote-centre model (TWAP fair value, `normal_cdf`, `log`, `sqrt` — Canonical §7) is
inherently real-valued. Policy: the centre model may compute in float, but its **output is
immediately quantised to `PriceTicks` by one explicit, documented rounding rule**, and only
the quantised value enters state, decisions, and the replay journal. Determinism is
therefore preserved at the quantisation boundary rather than throughout the model. The
quantisation rule is itself part of the strategy contract and must be recorded.

---

## §7 Determinism and replay

Replay is not a testing convenience; it is the mechanism by which every OPEN item is
eventually closed (Canonical §36, §34-L2). It therefore constrains the architecture:

```text
production:  live feeds  -> event journal -> Plane 1 -> Plane 2 -> venue
replay:      recorded journal            -> harness -> Plane 2 -> assertions
                                                       ^^^^^^^
                                              the identical code object
```

Requirements this places on the code:

1. Strategy and accounting are pure functions of `(state, event)`. (§2.1)
2. Time, sequence numbers, and feed identity are **event fields**, never ambient reads.
3. No dependence on set/dict iteration order in decision logic.
4. Every decision emits a record sufficient to reconstruct the inputs that produced it —
   the full decision log of Canonical §25.
5. Config used for a run is captured in the journal header, so a replay is
   `(journal, config)` reproducible and a parameter sweep is an explicit config diff.
6. Journal ordering is the single source of truth for event order. If two feeds interleave
   differently on a rerun, that is a recording defect, not acceptable nondeterminism.

`ReplayRecorder` and the replay harness must round-trip: recording a replay must produce a
byte-identical journal. That property is the P5 acceptance gate.

---

## §8 Module map

Python 3.12+. `src/` layout, one distribution package `maker5m` so the subsystem names do
not collide in the global namespace.

```text
src/maker5m/
    numeric/        PriceTicks / ShareUnits / MoneyUnits, scaling, rounding   [P1]
    market/         MarketState, event contracts, PhaseMachine, snapshots     [P2]
    strategy/       QuoteCentre, GridSizer, BaseLotSelector, Endgame, decide  [P3,P4]
    accounting/     PositionLedger, CostBasis, SettlementPnL, Term1Term2      [P1,P2]
    replay/         journal format, replay harness, parameter sweeps          [P5]
    feeds/          Polymarket book feed, Binance spot feed, clock            [P6]
    execution/      PostOnlyGuard, LiveOrderTable, reconciler, rate limiter   [P7]
    risk/           hard band, staleness, consistency, kill switch            [P9]
    settlement/     resolution verification, redemption                       [P10]
    telemetry/      decision/fill logs, latency, queue, PnL metrics           [P8,P11]
    ui/             read-only views, control channel                          [P12]
    bot/            composition root, wiring, runtime entry points            [P13]

tests/
    unit/           per-module; arithmetic and invariant conformance
    integration/    cross-plane behaviour, replay determinism
```

`bot/` is a **composition root only** — wiring, configuration, and process entry. It holds
no strategy logic and no god object. There is no "manager everything" class anywhere.

Dependency direction (a module may only import downward):

```text
bot  ->  ui, telemetry, settlement, risk, execution, feeds, replay
              ->  strategy, accounting, market
                          ->  numeric
```

`numeric` imports nothing from the project. `strategy` and `accounting` import only
`numeric` and `market` — this is what keeps Plane 2 pure and replayable.

---

## §9 Configuration and parameter labelling

Every strategy parameter is a named config field carrying its status label (I18):

```text
grid                5 shares            CONFIRMED
tick                0.01                CONFIRMED
delta_ticks         0                   CONFIRMED
post_only           true                CONFIRMED
gamma               0                   canonical newer spec
band_skew           0                   canonical newer spec
band_hard           ~100 shares         CONFIRMED (safety wall)
quote_start_offset  ~3 s                CONFIRMED
endgame_start       240 s               CONFIRMED
stop_offset         280 s               CONFIRMED
twap_window         60 s                CONFIRMED
queue_improve_depth 0 ticks             target behavior
max_strategy_orders 2                   CONFIRMED
endgame_tilt        30 shares           FITTED       -> O05
endgame_band        5 shares            FITTED       -> O06
base_lot L          15 / 20 / 25        values CONFIRMED, selection rule OPEN -> O03
centre_source       clob_mid initially  OPEN         -> O01
sigma               unknown             OPEN         -> O02
fee/rebate model    incomplete          OPEN         -> O07
max_requotes_per_sec ~8                 OPERATIONAL
```

The label must be available at runtime so telemetry and the UI can mark which numbers are
not established. A config value whose label is `OPEN` and which has no corresponding entry
in `OPEN_ITEMS.md` is a defect.

---

## §10 Ambiguities resolved by precedence

Recorded here so they are not re-litigated. Genuinely unresolved items are **not** here —
they are in `OPEN_ITEMS.md`.

| # | Question | Resolution |
|---|---|---|
| A1 | Favourite when `centre == 0.50` exactly | Canonical §32 is explicit: `favourite_up = centre > 0.5`. So `0.50 → DOWN`. Detailed §25 leaves the tie undefined; Canonical wins. Implement as `> 0.5`, and log the tie case as it is economically arbitrary. |
| A2 | Up-space `bid`/`ask` vs venue `BUY UP`/`BUY DOWN` | Same objects. `bid_*` ≡ UP side ≡ `BUY UP @ C`; `ask_*` ≡ DOWN side ≡ `BUY DOWN @ (1-C)`. Canonical §15.2 uses `bid_allowed/ask_allowed`, §32 uses `up_allowed/down_allowed`. Code uses one vocabulary throughout: **UP side / DOWN side**. |
| A3 | Is `band ~ 40` a control? | No. Canonical §14.1 and Detailed §15 call it an *observational soft region*. Only `band_hard` is enforced. (I17) |
| A4 | Is `band_hard` two-sided? | One-sided per side, per Canonical §32: `I >= band_hard` blocks the UP side only; `I <= -band_hard` blocks the DOWN side only. The inward side stays live. |
| A5 | Does ENDGAME replace grid sizing? | No. Detailed §29: ENDGAME modifies **order eligibility only**. Pricing, sizing, post-only, and event-driven behaviour are unchanged. (I14) |
| A6 | `rebates` in the live PnL formulas | Live state uses `estimated_rebate`; realised rebate is reconciled post-market. Canonical §3.2 vs §24.3. The two must be tracked as distinct fields, never conflated. |
| A7 | Is the `0.11-0.89` band enforced? | No. Soft only. Canonical §8.3 and §29.9 forbid a hard cutoff. (I05) |
| A8 | Where does the hard band live, Plane 1 or 2? | Plane 2 — it is a pure eligibility input in Canonical §32's decision function, so replay must reproduce it. Environmental risk checks stay in Plane 1. |

Anything not listed here and not in `OPEN_ITEMS.md` must be resolved by reading the frozen
source, not by assumption.
