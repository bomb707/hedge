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
- §6 Numeric contract (FROZEN at P1)
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

### §3.4 Event ordering contract  *(established P2)*

`EventMeta.ingress_ordinal` is the **total order**, and it is the only thing that defines it.

```text
market_id         which market the event belongs to
event_id          stable identity, used to refuse a repeated fill
ingress_ordinal   the total order - strictly increasing, assigned at ingress
timestamp         TimestampNs - data, drives the phase; NOT the ordering key
```

Timestamps are deliberately not the ordering key: two feeds do not share a timestamp domain,
an exchange timestamp can tie, and a slow feed can deliver an older timestamp later.
Ordering on them — or on Python's arrival order — would let replay reproduce a different
sequence than production, which would silently invalidate every experiment run against a
journal.

The ingress adapter (P6) merges feeds into one stream, assigns the ordinal, and normalises
timestamps so they are non-decreasing. The reducer enforces both and **fails closed**
(`EventOrderError`) on a repeated or decreasing ordinal, or on a decreasing timestamp. A
recorded stream therefore has exactly one legal interpretation.

### §3.5 Fill idempotency  *(established P2)*

Applying a fill twice would silently corrupt every downstream figure (I01), so the reducer
tracks `applied_fill_ids` and **raises** `DuplicateEventError` on a repeat rather than
ignoring it. Rejecting rather than absorbing is deliberate: a re-delivered fill means the
ingress path is broken, and quietly swallowing it would hide that. De-duplicating a venue
that legitimately re-sends is P6/P7 work; `EventMeta.event_id` is the mechanism it needs.

Identity is the event id, never the payload — two genuinely separate fills with identical
size and price are real volume and must both apply.

### §3.6 Phase model  *(established P2)*

**One source of truth: the phase is a pure function of the event timestamp.** There is no
stored phase field on `MarketState`; `MarketState.phase` is a property over
`phase_at(t0, last_event_timestamp, config)`. A recorded phase that could drift out of
agreement with the stream is therefore not merely discouraged, it is unrepresentable.

`PhaseEvent` exists to journal the boundary explicitly — so replay and telemetry see it, and
so a feed layer can force the core to observe a boundary in a quiet market. The reducer
**validates** it against the derived phase and raises `InvalidPhaseTransitionError` on any
disagreement. A `PhaseEvent` can never move the market to a phase its own timestamp does not
imply, and no transition is ever triggered by a wall clock.

Boundaries are half-open and exact, on integer nanoseconds, with no epsilon:

```text
elapsed <   3 s   PREARM        (also every elapsed < 0: the market is pre-armed early)
elapsed <  240 s  QUOTE
elapsed <  280 s  ENDGAME
elapsed <  300 s  SETTLING
otherwise         DONE
```

### §3.7 Decision-layer contract  *(established P4)*

`StrategyEngine.decide(state) -> DecisionResult` produces **intent plus the record explaining
it**, and nothing else. It never emits a cancel: "no desired order on this side" is strategy
intent, and translating desired-none against a live order into a CANCEL is the reconciler's
job at P7 (I09).

```text
phase not QUOTE/ENDGAME  ->  no orders, PHASE_NOT_QUOTING      (fast path: no centre,
centre unavailable       ->  no orders, CENTRE_UNAVAILABLE      no base lot, no grid)
otherwise                ->  candidate quote, then eligibility
```

**Eligibility is an intersection with typed reasons.** A side is live only if
`phase_allows AND endgame_gate_allows (ENDGAME only) AND hard_band_allows`. Reasons are an
enum, never free text, because they feed Detailed §35's `NOT_QUOTING` classification — "the
bot did not quote" is only useful if the *why* is machine-readable.

**Favourite direction comes from the raw centre, before quantization.** Canonical §32
evaluates `centre > 0.5` on the unrounded value. With `tick = 0.01` a raw centre of `0.504`
quantizes to `0.50`; comparing the quantized price would call the favourite DOWN while the
strategy's own value says UP, letting a rounding artefact decide a 30-share terminal
residual. The comparison is exact rational integer arithmetic:
`2 * numerator > denominator * PRICE_SCALE`.

**The candidate quote is built identically in QUOTE and ENDGAME** — same centre, tick
rounding, zero-spread prices, base lot, and grid plan — and only then is the endgame gate
applied. A5 is therefore structural: there is no branch in which the endgame could resize or
reprice, because sizing happens before the regime is consulted. Candidates are recorded in
telemetry whether or not they were emitted, so the invariant is checkable from the record.

**Economics are mandatory on every decision, and are never an eligibility input** — see §10,
A10.

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

### MarketData — Plane 1  *(built P6)*
- Polymarket market WebSocket (`book`, `price_change`, `tick_size_change`, `best_bid_ask`),
  with the documented `PING`/`PONG` application heartbeat. REST is used for discovery and
  recovery only, never as the steady-state path (Canonical §22).
- Binance `@aggTrade` for external BTC spot, consumed asynchronously and on the decision
  path (I11).
- `IngressClock` — wall-anchored monotonic time; drift measured, never corrected.
- Must never: block on decode, allocate per-tick dataframes, or hand mutable buffers to
  Plane 2.
- Strictly **read-only**: no order endpoint, credential, wallet key, or signing exists in
  this plane or anywhere it imports. Execution begins at P7.

### StrategyEngine — Plane 2 (pure)
`decide(state) -> DesiredOrders`. The single place the strategy exists.
- Sub-parts: `QuoteCentre`, `TWAPFairValue`, `GridSizer`, `BaseLotSelector`,
  `EndgameController`, `EligibilityGate`.
- `QuoteCentre` is a **replaceable strategy component** behind one interface, because the
  centre source is OPEN (O01). Initial: `clob_mid`, computed from the UP top of book only.
- `BaseLotSelector` is likewise replaceable — `choose_base_lot(market_state)`, never a
  frozen `L = 15` (I18, O03).
- `GridSizer` carries **both** O04 target-selection readings as named policies; neither is
  called correct, and the fingerprint provably cannot arbitrate between them (§6.8).
- Every replaceable component exposes a `ParameterStatus` at runtime, so telemetry and the
  UI can show which numbers are not established (I18).
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

### ExecutionEngine — Plane 1  *(built P7)*
- Sub-parts: `prepare_order` (intent → venue submission), the post-only guard, `reconcile`
  (pure), `LiveOrderTable`, `ReplacementTracker`, `TokenBucket`, `VenueAdapter`.
- Enforces post-only locally *and* at the venue (I06); an intentional taker fill is
  surfaced as an invariant violation, never absorbed (I07).
- Preserves queue priority by keeping unchanged orders (I09); no fixed requote delay (I10).
- Maintains client order IDs, idempotency, and in-flight/unknown state so that "unknown
  order state" is a detectable condition rather than a silent divergence.
- Must never: cancel because a market-data event merely arrived.
- Live trading is hard-disabled: a real write adapter cannot be constructed while
  `LIVE_TRADING_ENABLED` is `False`, checked before any credential or socket is touched.

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

### §4.1 Market-data contracts  *(established P6)*

**`EventMeta.timestamp` is synchronized local ingress time**, not a venue timestamp. The
strategy reacts when data is *received*, and P2 requires a non-decreasing stamp. The clock is
wall-anchored once and advanced monotonically:

```text
ingress = wall_anchor + (monotonic_now - mono_anchor)
```

so it is wall-aligned (phase boundaries derive from a market's `T0`) and immune to a
backwards NTP step. Drift from true wall time is **measured, never corrected** — correcting
mid-run would reintroduce the backwards jump this design exists to prevent. Venue timestamps
are kept in feed diagnostics and never enter Plane 2 state.

**One merger assigns every ordinal.** No feed numbers its own events; two counters could not
be interleaved into one legal order, and P5 replay depends on there being exactly one.

**Pre-arm warms state; the market's stream begins at `T0`.** `MarketState.initial` parks the
state clock at `T0`, so an event stamped earlier would violate the non-decreasing contract.
Messages arriving before `T0` are therefore consumed and applied to the book trackers but
produce no Plane 2 event — which is exactly what pre-arm is for (Canonical §21): at `T0` the
strategy already has a warm book and no discovery work remains.

**Continuity is handled conservatively.** Polymarket publishes no documented monotonic
sequence — the payloads carry `timestamp` and `hash`, neither with defined continuity
semantics — so `BookUpdate.sequence` stays `None` and nothing is fabricated into it. Instead
any disconnect, heartbeat failure, malformed message, unknown token, or resubscription marks
the stream unhealthy, **drops the book**, and requires a fresh authoritative snapshot before
it is trusted again. Reconnect never reorders: recovered data takes the next ordinal, and
history is never spliced backwards into an already-consumed stream.

**Venue tick is not strategy tick.** The venue's announced `tick_size` / `tick_size_change` is
its currently legal order-price increment, recorded in `VenueMarketRules` for P7's
submission-legality checks. It never mutates `MarketDefinition.tick`: the replica quotes on
its documented `0.01` grid unless an empirical strategy revision says otherwise. They happen
to coincide today; that is not relied upon.

### §4.2 Execution contracts  *(established P7)*

**The venue adapter rejects an illegal intent; it never alters one.** Price is passed through
untouched. Off-grid, out-of-range, crossing, or below-minimum all **block**. Moving a price by
one tick changes which queue the order joins, and queue position is where the edge lives
(Canonical §10.1) — so a helpful adjustment in transport would silently be a different
strategy.

**Size is the one thing that must change**, because the venue accepts two decimals while the
strategy legitimately produces six after fractional fills. It is truncated toward zero, never
rounded up, and both quantities are preserved on `PreparedOrder`. P3's lattice is not bent to
fit transport: the ledger stays authoritative over what actually filled, and the next fill
produces a fresh desired size.

**Post-only is a type, not a setting.** `OrderSide` has one member, `VenueOrderType` has one
member, and `POST_ONLY` is a constant `True`. SELL, FOK, FAK, market orders, and a
`post_only=False` retry are not representable. This matters because the SDK's
`create_limit_order` defaults `post_only=False` and accepts `Literal["BUY","SELL"]` — the
narrow gate makes those permissive defaults unreachable.

**The post-only race is expected and never answered with a taker fallback.** Local validation
uses the observed same-outcome ask; the venue flag handles the gap between validation and
arrival. On a post-only rejection the order state is recorded and the next deterministic cycle
decides afresh — never a retry with `post_only=False`, never a price change inside an error
handler.

**KEEP is the default, and the comparison is on *remaining* size.** A partially-filled order
whose remainder equals the newly desired size is a KEEP. Comparing the original size would
cancel a good order after every partial fill and hand away its queue slot for nothing. There
is no age-based, timer-based, or event-count-based replacement anywhere.

**Replacement is `CANCEL_THEN_PLACE`, labelled `OPERATIONAL`.** The sources do not establish
network sequencing, so this is an engineering choice: it avoids duplicate exposure and stays
inside Canonical §23's two-order model. `PLACE_THEN_CANCEL` is declared for P8/P13 to measure
but raises if selected. Every pending replacement is bound to the decision generation that
created it; if a newer decision supersedes it, the cancel acknowledgement reconciles against
*current* desired state rather than placing an obsolete price.

**The SDK is a boundary, not the architecture.** `polymarket-client==0.6.0` owns EIP-712
signing, L1/L2 auth, wire serialization, and the authenticated transports. It owns nothing
above that line. Its metadata cache is prewarmed during pre-arm so the hot path is
`sign → POST`; `post_order` returns `AcceptedOrder | RejectedOrder` immediately and
`wait_for_order_fill_settlement` is deliberately never called.

### §4.3 Measurement contracts  *(established P8)*

**Two clock domains, never mixed.** ``EventMeta.timestamp`` is the synchronized wall-aligned
*ingress* clock and drives the market lifecycle. Latency uses ``time.perf_counter_ns()`` — a
monotonic high-resolution clock with **no epoch**, only ever subtracted from itself.
Subtracting a venue timestamp from a ``perf_counter`` reading would produce a number that
looks like a latency and means nothing; venue stamps stay in feed diagnostics.

**Instrumentation is never deterministic state.** No latency value, queue estimate, or
performance counter enters ``MarketState``, ``DecisionResult``, ``LedgerState``, or a P5
journal. A measurement describes *this run on this machine*; putting one into a replayed
decision would make replay depend on the machine that recorded it (I20). Enforced by a static
test that also forbids Plane 2 from importing ``maker5m.telemetry`` or reading a performance
counter.

**Instrumentation never blocks trading.** The sink is a bounded in-memory ring that drops
oldest and counts drops. A lost observation is an observability incident; a stalled hot loop is
a trading incident (I19). Authoritative market events and execution actions are never dropped —
they are not telemetry.

**Traces are mutable and slotted, deliberately.** P4 and P7 profiling measured frozen dataclass
construction at ~99 ns per field. A trace is filled in place and snapshotted only when
published, because measurement scaffolding must not cost more than the thing it measures.

**Queue position is always an estimate.** Polymarket publishes no per-order queue index. Every
value carries a :class:`QueueConfidence` whose ceiling is ``ESTIMATED``; there is no ``EXACT``
member. Displayed depth at our exact price is the initial estimate; decreases reduce it,
increases do not raise it (new same-price orders join behind), a fill sets it to zero, and any
continuity loss invalidates it rather than being reconstructed from a fresh snapshot.

The model has a **documented optimistic bias**: the decrease is measured against the last
observation, which may include size added after we arrived, so consumption behind us can be
credited as progress. It is bounded by ``ahead <= displayed`` and by zero, and it is recorded
in the evidence rather than hidden — an optimistic queue estimate inflates ``AT_FRONT``, which
is the direction that would flatter the strategy.

**No numeric "deep" threshold is invented.** Any positive estimated queue-ahead classifies as
``PRICE_OK_BUT_DEEP`` with the quantity visible alongside. Choosing a threshold is what O08
exists to answer.

**A queue slot belongs to an order, not to a price.** *(corrected after P8 review.)* Slot
identity is the client order id. A slot opens only when an order is actually dispatched, at the
depth displayed immediately before dispatch, and it inherits nothing from any earlier slot —
including one at the same price, because that was a different order. It closes when the order
stops resting. `BLOCKED`, `WAIT`, and `NOTHING` open no slot at all, and a `REPLACE` closes the
old slot without granting a new one, since P7's policy is CANCEL_THEN_PLACE and the
replacement's queue position begins only when a later cycle reaches PLACE.

The first P8 implementation keyed slots on desired price and advanced them on strategy intent.
Against real data that gave 119,116 post-only-blocked sides a queue estimate each. Enforced
structurally rather than by convention: `classify()` has no `resting_price` parameter, so the
resting price can only come from a live estimate, and `AT_FRONT` is unreachable without one.

**State maintenance and emission are sampled differently.** Deterministic sampling may thin
*emission* — stage timestamps written to a trace, distribution samples, classification, the
sink. It must never thin *state*: shadow slot transitions and depth observations run on every
cycle of a measuring run, because an estimate that skipped unsampled depth changes would depend
on the sampling rate. Sampling is `OPERATIONAL` configuration and must never be used to make a
latency distribution look better than it is.

**The trading path captures; analysis happens downstream.** *(established P8C.)* Observation
is split in two. The hot path records *facts* — the displayed depth at our own price, the
reconcile plan, stage timestamps for sampled cycles — into a bounded non-blocking buffer, and
returns. Queue estimation, classification, counting, and distributions are reconstructed by
:class:`~maker5m.telemetry.analyzer.TelemetryAnalyzer` from that stream, in ingress order,
producing identical results. The trading path must never wait on telemetry analysis.

The split is drawn at *analysis*, not at *simulation*. Preparation, reconciliation, and the
shadow order-table lifecycle model what production does every cycle and therefore stay hot;
relocating them would make an OFF/ON comparison meaningless. Depth is the one measurement that
cannot be deferred either — the book is mutable, and the size resting at our own price has to be
sampled at the moment the cycle sees it.

**Observation order is authoritative, and gaps are not bridged.** Observations carry a capture
sequence and are folded strictly in order. Out-of-order input **fails closed**: a stream whose
order is unknown has unknown provenance, and silently sorting it would manufacture confidence.
A missing sequence means a dropped observation, which means an unseen depth change at our own
price, which means the estimate cannot be continued — it becomes ``STALE``. Trading is
unaffected by a telemetry drop; the measurement must still say so.

**Sampling controls timing work, not just output.** The sampling decision is made before reduce
and decide, via ``IngressMerger.submit(..., measure_stages=)`` and
``MarketDataPipeline.stage_selector``, so an unsampled ordinary event takes no perf-counter
readings at all. An action discovered after reconciliation is still recorded, with one reading
for the action itself and its earlier stages left ``NOT_CAPTURED``. "Always observe actions"
does not entitle a report to timestamps that were deliberately not taken, and none are imputed.

**Execution queue losses and shadow slot losses are different metrics.** `execution_queue_loss_actions`
counts reconciler decisions that give up a slot (REPLACE, CANCEL). `shadow_slot_losses` counts
slot identities that ceased to exist, which includes closures the plan does not name, such as a
complete fill. Both reconcile exactly to their own typed reason counts. They must never be
reported under one name: doing so produced two different totals, 887 and 1,049, presented as
though they measured the same thing.

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

## §6 Numeric contract — **FROZEN at P1**

Binary floating point is not acceptable for inventory, cost, or price on the hot path:
`0.01` is not representable, accumulated `+=` over hundreds of partial fills drifts,
equality comparison for order reconciliation becomes approximate, and replay stops being
bit-exact. All four are direct invariant violations (I01, I03, I09, I20).

Implemented in `src/maker5m/numeric/`. The scales below are **frozen**: changing them
invalidates every recorded replay journal and every stored ledger.

### §6.1 Frozen scales

```text
SCALE_DECIMALS = 6

SHARE_SCALE = 1_000_000     1 share       = 1_000_000 ShareUnits
MONEY_SCALE = 1_000_000     1 USDC        = 1_000_000 MoneyUnits
PRICE_SCALE = 1_000_000     probability 1 = 1_000_000 PriceUnits
```

Chosen to match the venue's atomic units (`COLLATERAL_TOKEN_DECIMALS = 6`,
`CONDITIONAL_TOKEN_DECIMALS = 6`) and to represent every documented tick size exactly. Full
evidence and the residual P6 traffic check are in `OPEN_ITEMS.md` O10.

### §6.2 Order-input precision is not ledger precision

```text
ORDER INPUT QUANTIZATION  !=  AUTHORITATIVE LEDGER PRECISION
```

The official client rounds a *submitted* order size to two decimals. That is a transport
concern on the way out. Position and collateral movements settle in 6-decimal atomic units,
and the ledger is authoritative over what the venue actually moved — never over what was
asked for. Consequences, all enforced in code:

- `quantize_order_size` (2 decimals, truncating toward zero) lives in `numeric/ticks.py`
  and is **never** applied to a ledger input;
- a `Fill` carries the venue's **collateral amount** as authoritative cost. Cost is not
  reconstructed as `shares * price`, because order construction and atomic rounding at the
  venue mean the two can differ. `price` is carried for analysis only;
- neither of the above is the strategy's 5-share inventory lattice, which is P3 work and
  appears nowhere in the numeric kernel.

### §6.3 Domain types

| Type | Represents | Notes |
|---|---|---|
| `ShareUnits` | outcome tokens, in `1/SHARE_SCALE` of a share | Signed: net inventory `I` is a `ShareUnits` (I02). Carries true fractional fills (I03). |
| `MoneyUnits` | USDC, in `1/MONEY_SCALE` dollars | Signed: PnL is a `MoneyUnits`. Costs, fees, and rebates are separately required non-negative. |
| `PriceUnits` | probability / share price, `1.0` is `PRICE_SCALE` | Absolute, not a tick count. A tick-count view is derived via `price_to_ticks`. |

P0 provisionally named the price type `PriceTicks` (a per-market tick count). P1 replaced it
with `PriceUnits` (an absolute fixed-point probability) because a tick count is only
meaningful once a market's tick is known, whereas the ledger and the parser need a
representation that is valid before any market exists. The tick grid is a *view* over
`PriceUnits`, not the storage format.

**Representation: `typing.NewType` over `int`.** The three types are distinct to the type
checker, so a value of one domain cannot be assigned to another; because `int + int` widens
to plain `int`, mixed-domain arithmetic also fails to type-check the moment its result is
stored or passed anywhere annotated — which under `mypy --strict` is everywhere. At runtime
they are ordinary `int` objects: exact, immutable, hashable, and cheap to compare.

Measured on CPython 3.12: a raw `int` add is ~28 ns, re-wrapping the result through the
`NewType` costs ~60 ns, and a frozen-dataclass wrapper costs ~232 ns. At this strategy's
event rates — of the order of 100 fills and a few thousand book updates per 300 s market —
the difference is far below the noise floor, so the cheaper representation was taken and no
wrapper class is justified.

### §6.4 Scaling and rounding policy

```text
shares_at_par(shares)              -> MoneyUnits    one winning share pays exactly $1.00
notional_cost(shares, price, mode) -> MoneyUnits    explicit rounding mode, no default
```

- These two named functions are the **only** cross-domain conversions. Every other
  operation stays inside one domain.
- `notional_cost` is exact for every price on the documented tick grid. Where it is not,
  the caller must name `FLOOR`, `CEILING`, or `EXACT` (which raises) — rounding is always a
  documented decision at a named boundary, never implicit.
- The ledger does **not** call `notional_cost`; authoritative cost comes from the venue
  (§6.2).
- No intermediate ever becomes `float`. `Decimal` is used nowhere: decimal strings are
  parsed directly to integer units, so no binary floating-point error is ever introduced.
  `Fraction` appears only in the off-hot-path Term1/Term2 decomposition.

### §6.5 Exactness contract

1. Every venue-reported price and quantity must be **exactly representable**. A value that
   is not raises `NotRepresentableError` and must halt new quoting — never a silent round.
   Excess fractional digits that are all zero carry no information and are accepted
   (`"1.0000000"` is fine, `"1.0000001"` is not).
2. Parsing is strict: plain decimal strings only. No exponent, no underscores, no
   whitespace, no bare sign, no leading or trailing dot, ASCII digits only, and non-string
   input is rejected. Adapters hand over the venue's string, never a float.
3. The 5-share lattice is exact: `GRID = 5 * SHARE_SCALE`. The modular fingerprint (I04) is
   integer modular arithmetic and is therefore exactly testable — at P3.
4. Order reconciliation equality is integer equality — fast and unambiguous (I09).
5. `pnl_if_up`, `pnl_if_down`, and the settlement result are exact `MoneyUnits`.
   `Term1 + Term2` is an exact rational whose sum equals trading PnL exactly (§6.7).
6. `float` appears only at presentation boundaries, through the single explicitly-named
   `to_display_float`. It is never converted back into state.

### §6.8 Quote construction and tick quantization  *(established P3)*

Two quantization decisions sit on the strategy path, and both are named policies rather than
inherited language defaults. The built-in `round` decides nothing in this codebase.

**Tick quantization of the centre (O13).** The sources give one worked example
(`0.6274 -> 0.63`), which excludes FLOOR and says nothing about a tie. `TickRounding` names
`HALF_EVEN` / `HALF_UP` / `HALF_DOWN`.

**The zero-spread construction is deliberately not Canonical §32's.** §32 writes

```text
px_up   = round_to_tick(centre)
px_down = round_to_tick(1.0 - centre)
```

Rounding both sides independently is tie-policy dependent: at an exact half tick it breaks
the complement identity — and therefore I05's zero spread — for `HALF_UP` and `HALF_DOWN` at
every tie point on the grid. `maker5m.strategy.prices` instead complements the
**already-quantized** centre:

```text
up_buy_price   = quantize(C)
down_buy_price = 1 - up_buy_price          (exact integer complement)
```

which is exact under every policy. The open O13 default therefore cannot leak into the
CONFIRMED zero-spread property. `QuotePrices` asserts the invariant at construction, so a
future edit that reintroduces a spread fails immediately.

**Grid rounding (inside O04).** `GridRounding` names the same three tie rules for snapping an
offset inventory to the 5-share lattice. Canonical §12.1's `round(I/grid + L/grid)` would be
banker's rounding in Python; whether that is intended is unstated, so it is selected
explicitly rather than inherited.

### §6.6 Where floats remain legitimate

The quote-centre model (TWAP fair value, `normal_cdf`, `log`, `sqrt` — Canonical §7) is
inherently real-valued. Policy: the centre model may compute in float, but its **output is
immediately quantised to `PriceUnits` by one explicit, documented rounding rule**, and only
the quantised value enters state, decisions, and the replay journal. Determinism is
therefore preserved at the quantisation boundary rather than throughout the model. The
quantisation rule is itself part of the strategy contract and must be recorded. This is a
P3 concern; nothing in the P1 kernel produces a float.

### §6.7 Term1 / Term2 exactness

Average acquisition price is genuinely rational, so the two terms are computed with
`fractions.Fraction` in `accounting/decomposition.py` — no floating-point average, and no
premature rounding of an intermediate. Their sum is always an exact integer `MoneyUnits`
amount:

```text
term1 + term2 == gross_payout - total_cost                (exactly)
net_pnl       == term1 + term2 - fees + rebate            (exactly)
```

Fees and rebates sit outside the term identity because Canonical §4 defines the terms from
share counts and prices alone. `Fraction` is slower than `int`; that is acceptable because
this is Plane 3 analytics. The hot-path ledger stays integer-only.

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

### §7.1 Journal contract  *(established P5)*

Canonical UTF-8 NDJSON: one header record, then one record per step, `\n` after every line
including the last. `json.dumps` with `sort_keys=True`, compact separators, and
`ensure_ascii=True`, so the bytes cannot vary with insertion order, whitespace, locale, or
encoding. **No floats anywhere** — the encoder rejects one rather than writing it. Enums are
written as their explicit stable values; no `repr`, no `pickle`, no class paths, no object
identities. Optional values are always present and explicitly `null`, so "recorded as
nothing" is distinguishable from "this build forgot to write it".

```text
decode(encode(journal)) == journal
encode(decode(bytes))   == bytes        byte for byte
```

The decoder **fails closed** on an unknown schema version, unknown event tag, unknown enum
value, missing field, unexpected field, or an unsupported strategy component. A journal is
evidence; a decoder that guessed would turn it into fiction.

### §7.2 The config snapshot is complete  *(established P5)*

The header carries the **entire** strategy configuration — centre component, base-lot
selector, grid policy, grid rounding, tick rounding, and all three regime magnitudes — never
a reference to whatever this build's defaults happen to be. A journal recorded under
`OBSERVED_ADJACENT` must still replay under `OBSERVED_ADJACENT` after the default changes,
or every O01/O03/O04/O05/O06/O13 comparison would drift silently with the code.

Every header also declares its **provenance** (`SYNTHETIC` / `RECONSTRUCTED` / `LIVE_PAPER` /
`LIVE`). The difference matters enormously and is invisible in the data, so it is a required
field rather than an assumption. No journal in this repository is anything but `SYNTHETIC`.

### §7.3 Verified replay vs parameter sweep  *(established P5)*

Two operations that must never be conflated:

* **verified replay** re-derives every decision from the journal's *own* recorded config and
  compares the **complete** `DecisionResult` after each event, failing at the *first*
  divergence with the step index, event id, and ingress ordinal. Comparing only the emitted
  order would miss a decision that is wrong in its centre, its eligibility reasons, or its
  economics while the order happens to look identical;
* **parameter sweep** re-derives decisions under a *different* config and compares nothing,
  because a candidate config is expected to decide differently.

The sweep produces trajectories and **does not score or rank them**. Judging one requires an
empirical objective measured against real data, which does not exist yet; a ranking derived
from synthetic journals would look like evidence and be none.

---

## §8 Module map

Python 3.12+. `src/` layout, one distribution package `maker5m` so the subsystem names do
not collide in the global namespace.

```text
src/maker5m/
    numeric/        ShareUnits / MoneyUnits / PriceUnits, parsing, ticks     [P1 DONE]
    domain.py       Outcome - shared leaf primitive, imports nothing          [P2 DONE]
    market/         events, MarketState, reducer, phase machine, snapshot     [P2 DONE]
    strategy/       up-space, centre, quantization, prices, base lot, grid   [P3 DONE]
                    config, endgame, eligibility, decision, engine.decide()   [P4 DONE]
    accounting/     LedgerState, Fill, settlement, Term1/Term2 decomposition  [P1 DONE]
    replay/         schema, canonical codec, recorder, verifier, sweeps       [P5 DONE]
    feeds/          Polymarket book feed, Binance spot feed, clock            [P6]
    execution/      prepare, guard, live orders, reconciler, rate limit, SDK  [P7 DONE]
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
              ->  strategy
                     ->  market
                            ->  accounting
                                   ->  domain, numeric
```

`numeric` and `domain` import nothing from the project. This is what keeps Plane 2 pure and
replayable.

**Corrected in P2.** P0 placed `accounting` above `market`, and P1 put the `Outcome` enum in
`market` to satisfy that. P2 showed the direction is the other way round: `market` needs
`Fill` and `LedgerState` from `accounting` — the event stream carries fills and `MarketState`
embeds the ledger — while `accounting` needed only that one enum. Keeping it there produced a
genuine import cycle. `Outcome` now lives in the leaf module `maker5m/domain.py`, which
imports nothing, and both packages re-export it so call sites are unchanged. This is a
structural correction, not a change to any P1 arithmetic.

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

| A9 | Canonical §4's `Term2 = R * (1 - a_W)` with `R = n_W - n_L` | **Incorrect when `n_W < n_L`**, i.e. whenever the bot ends holding more of the *loser* — which is exactly what happens when the endgame favourite does not win. Worked from the document's own example with the outcome reversed (120 UP @ 0.60, 100 DOWN @ 0.50, DOWN wins), the literal formula gives `-10 + -10 = -20` against a true settlement result of `-22`. The residual is a loser residual there: it pays nothing and cost `a_L` per share. The general form implemented is `Term1 = M*(1 - a_W - a_L)`, `Term2 = (n_W - M)*(1 - a_W) - (n_L - M)*a_L`, which reduces to Canonical's expression for `n_W >= n_L`. This is not a strategy change: Canonical §35 makes "Term 1 + Term 2 reproduces settlement PnL" a mandatory acceptance criterion, and §4 asserts the decomposition "is algebraically equivalent to the exact settlement accounting" — the correction is what makes both statements true. Both branches are regression-tested. |

| A10 | Does Canonical §17 imply an economic eligibility gate? | **No.** §17 says the endgame engine "should monitor" the settlement edges and the incremental cost of acquiring favourite shares; Detailed §28 says the dual-token cost "must always be visible". Both are descriptive objectives. Canonical §32's decision function computes `settlement_edge_up` / `settlement_edge_down` and **returns them without using them in eligibility** — its gates are phase, the endgame band, and `band_hard`, nothing more. Neither source states a threshold such as "block the favourite side when `pnl_if_favourite < 0`". Converting a descriptive objective into an invented threshold would be a strategy change of exactly the kind Canonical §37 forbids. So economics are **mandatory decision telemetry** and eligibility remains `phase + endgame gate + band_hard`. Both rebate views are recorded because O07 is open (A6). |

Anything not listed here and not in `OPEN_ITEMS.md` must be resolved by reading the frozen
source, not by assumption.
