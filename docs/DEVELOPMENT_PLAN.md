# Development Plan

Staged implementation with strict boundaries. Read **only the phase you are working on**,
plus `INVARIANTS.md` and `STATUS.md`.

## Rules that govern every phase

1. **Code existing is not completion.** A phase is complete only when its acceptance gate
   has been *executed* and recorded in `STATUS.md`. "The module is there" is not a gate.
2. **No forward implementation.** Do not build a later phase's work early because it seems
   convenient. Out-of-scope lists are binding.
3. **No strategy optimization before replication correctness** (Canonical §37). If a change
   cannot be labelled CONFIRMED / FITTED / OPEN / OPERATIONAL, it does not go in.
4. **No OPEN item gets silently resolved.** Closing one requires the recorded experiment
   from `OPEN_ITEMS.md` and an update to that file.
5. **Every phase ends at a clean git boundary** with a green check run, and updates
   `STATUS.md`.
6. Phases P1-P5 build the deterministic core with **no network access at all**. That is
   deliberate: the strategy must be provably correct offline before a socket is opened.

---

## P0 — Repository baseline + architecture freeze   ← CURRENT

- **Goal:** an auditable boundary before implementation. Freeze the strategy sources,
  record the architecture, invariants, and open items, and stand up a minimal skeleton.
- **Inputs:** the two supplied strategy documents and figures.
- **Outputs:** `docs/INDEX.md`, `ARCHITECTURE_SSOT.md`, `INVARIANTS.md`, `OPEN_ITEMS.md`,
  `DEVELOPMENT_PLAN.md`, `STATUS.md`; frozen sources under `docs/strategy/` with pinned
  checksums; `pyproject.toml`; empty-but-valid `src/maker5m/*` package tree; `tests/`.
- **Tests:** frozen-source checksum test; package import test; live-trading-disabled test.
- **Acceptance gate:** `ruff check`, `ruff format --check`, `pytest` all green; git tree
  clean; no trading functionality present.
- **Out of scope:** everything else. Explicitly: no order submission, keys, signing,
  websockets, quote-centre model, grid algorithm, endgame algorithm, database, frontend,
  redemption.

---

## P1 — Fixed-point numeric kernel + exact accounting

- **Goal:** exact, deterministic arithmetic and a ledger that reproduces settlement PnL to
  the last unit.
- **Inputs:** `ARCHITECTURE_SSOT` §6; invariants I01, I02, I03; **O10 must be closed first**
  — the scales cannot be chosen from a guess.
- **Outputs:** `numeric/` (`PriceTicks`, `ShareUnits`, `MoneyUnits`, scaling, one explicit
  rounding boundary, exactness guard) and `accounting/` (`PositionLedger`, `CostBasis`,
  `SettlementPnL`, `Term1Term2`, `RebateLedger` skeleton).
- **Modules:** `src/maker5m/numeric/`, `src/maker5m/accounting/`.
- **Tests:** Canonical §34-L0 in full — exact settlement PnL; `Term1 + Term2` identity;
  both hypothetical branches; partial-fill accounting; cost-basis across replacements;
  the `120 UP @ 0.60 / 100 DOWN @ 0.50 → −$2` worked example; a non-representable quantity
  raising rather than rounding; property tests that random fill sequences keep
  `Term1 + Term2 == settlement PnL` exactly.
- **Acceptance gate:** L0 suite green **and** the arithmetic reproduces a known target-wallet
  market ledger exactly. Canonical §34-L1 is explicit: if arithmetic does not match the
  known ledger, stop.
- **Out of scope:** strategy logic, market state, any I/O, rebate *calibration* (O07 stays
  open — only the ledger structure is built).

---

## P2 — MarketState + event contracts + phase machine

- **Goal:** the authoritative state object, the five hot-path event types, and the phase
  machine — all deterministic and clock-free inside Plane 2.
- **Inputs:** `ARCHITECTURE_SSOT` §3, §5; Canonical §6, §24, §31; I20.
- **Outputs:** immutable event value objects (`SpotTick`, `BookUpdate`, `OwnFill`,
  `OrderStateEvent`, `PhaseEvent`, health events); `MarketState` with single-owner mutation;
  frozen snapshot type for Plane 3; `PhaseMachine`
  (`PREARM → QUOTE → ENDGAME → SETTLING → DONE`).
- **Modules:** `src/maker5m/market/`, plus the leaf `src/maker5m/domain.py`.
- **Tests:** phase transitions at `T0+3 / +240 / +280 / +300` driven purely by event
  timestamps; no wall-clock read anywhere in `market/` or `strategy/` (enforced by a static
  check, not by convention); snapshot immutability; state carries every field required by
  Canonical §24.1 that P2 owns.
- **Contracts established here** (see `ARCHITECTURE_SSOT` §3.4-§3.6): ingress-ordinal
  ordering, fill idempotency by event id, and the derived-phase model. P3 onwards must not
  re-litigate them.
- **Acceptance gate:** phase machine is a pure function of the event stream, and a static
  check proves Plane 2 modules import no clock, no I/O, and no network module.
- **Out of scope:** feeds, real timing sources, strategy decisions.

---

## P3 — Strategy core: Up-space + grid sizing

- **Goal:** Up-space translation, zero-spread quoting, and grid sizing.
- **Inputs:** Canonical §5, §8, §12; Detailed §5, §9-13; I04, I05; **O04 (source conflict)
  and O01, O03 apply.**
- **Outputs:** Up-space translation (`BUY DOWN @ d ≡ SELL UP @ 1-d`); tick rounding;
  `GridSizer` with target selection as a **swappable policy** carrying both the Canonical
  §12.1 reading (default) and the Detailed §12 reading (named alternative), per O04;
  `BaseLotSelector` interface with a labelled default; `QuoteCentre` interface with
  `clob_mid` implemented.
- **Modules:** `src/maker5m/strategy/`.
- **Tests:** the modular fingerprint `bid_size ≡ (-I) mod 5`, `ask_size ≡ (+I) mod 5` as a
  property test over random fractional inventories; the `I = -28.63, L = 15` worked example
  under **both** O04 policies with the divergence asserted explicitly rather than hidden;
  zero synthetic spread; exact tick rounding; the soft band is not enforced as a cutoff.
- **Acceptance gate:** fingerprint property test green over ≥10⁵ random inventories; both
  O04 policies present, selectable, and logged when they disagree; no `gamma`/skew term
  exists anywhere in the module.
- **Contracts established here** (see `ARCHITECTURE_SSOT` §6.8): named tick and grid tie
  policies, and the zero-spread construction by complementing the quantized centre rather
  than rounding both sides independently. P4 onwards must not re-litigate them.
- **Passing this gate is an implementation result, not an empirical one.** Both O04 policies
  reproducing their documented examples says nothing about which the target wallet used;
  that stays OPEN until replay evidence decides.
- **Out of scope:** endgame, execution, feeds, closing O01/O03/O04.

---

## P4 — ENDGAME controller + strategy decision engine

- **Goal:** the complete `StrategyEngine.decide(state) -> DesiredOrders`.
- **Inputs:** Canonical §15, §17, §32; Detailed §24-29, §47; I12, I13, I14, I17; O05, O06.
- **Outputs:** `EndgameController` (favourite direction, target, binding gate);
  `EligibilityGate` combining endgame gate and one-sided `band_hard`; the assembled
  `decide()` returning desired orders **and** the decision telemetry record.
- **Modules:** `src/maker5m/strategy/`.
- **Tests:** the Canonical §32 decision function reproduced case-by-case; gate binds in
  both directions; `centre == 0.50` resolves to DOWN per A1; `band_hard` blocks only the
  outward side per A4; ENDGAME changes eligibility only — prices and sizes identical to
  QUOTE for the same state (A5); no flattening path exists.
- **Acceptance gate:** `decide()` is pure — same input, same output, no clock, no I/O —
  proven by test, and reproduces every worked example in Canonical §32 and Detailed §47.
- **Contracts established here** (see `ARCHITECTURE_SSOT` §3.7 and §10 A10): eligibility as
  an intersection with typed reasons, favourite direction from the **raw** centre, and the
  absence of any economic eligibility gate. P5 onwards must not re-litigate them.
- **Passing this gate is an implementation result, not an empirical one.** `decide()`
  composes O01, O03, O04, O05, O06, and O13 at their reference or fitted settings. Green
  tests say the composition is correct; they say nothing about whether those settings match
  the target wallet.
- **Out of scope:** execution, order placement, closing O05/O06.

---

## P5 — Deterministic replay engine

- **Goal:** run recorded event journals through the **same** `decide()` used in production.
- **Inputs:** `ARCHITECTURE_SSOT` §7; I20; Canonical §34-L2.
- **Outputs:** journal format (versioned, with the config captured in the header); recorder;
  replay harness; parameter-sweep runner for OPEN items.
- **Modules:** `src/maker5m/replay/`.
- **Tests:** replaying a journal reproduces decisions bit-for-bit; recording a replay
  produces a byte-identical journal (round-trip); a config change produces a *different*
  and reproducible result; no strategy branch keyed on "am I replaying".
- **Acceptance gate:** byte-identical round-trip on a non-trivial journal, and the replayed
  decision stream matches the recorded one exactly.
- **Out of scope:** live feeds, real journals from the venue (synthetic journals only at
  this stage).

---

## P6 — Polymarket / BTC market-data adapters

- **Goal:** real streaming market data feeding the P2 event contracts. **First network
  access in the project.**
- **Inputs:** Canonical §21, §22; Detailed §6; I11.
- **Outputs:** Polymarket CLOB websocket book feed with sequence tracking and gap
  detection; Binance spot websocket; clock synchroniser; market discovery and strike
  chaining (`coinPriceStart[N] = coinPriceEnd[N-1]`); pre-arm of market `N+1`; REST used
  only for recovery.
- **Modules:** `src/maker5m/feeds/`, `src/maker5m/market/` (discovery).
- **Tests:** decode conformance against captured real messages; gap detection; reconnect
  and resubscribe; staleness detection; **an external spot tick alone wakes `decide()`**
  (I11) — asserted, not assumed; recorded live journals replay identically under P5.
- **Acceptance gate:** a full 5-minute market recorded end-to-end and replayed
  deterministically; pre-arm completes before `T0` with no discovery work in the opening
  seconds.
- **Out of scope:** any order submission, keys, signing. Read-only network only.

---

## P7 — Execution state + post-only order reconciler

- **Goal:** live order state and the minimal-action reconciler. First write path to the
  venue — gated off by default.
- **Inputs:** Canonical §11, §20, §23, §33; Detailed §16, §17; I06, I07, I09, I10.
- **Outputs:** `LiveOrderTable` (client IDs, in-flight, ack/reject, partials, idempotency);
  `PostOnlyGuard` (local **and** venue-level); `OrderReconciler.diff()` as a pure Plane 2
  function; `CancelReplace`; token-bucket `RateLimiter`; signing/credentials behind an
  interface that is **disabled by default**.
- **Modules:** `src/maker5m/execution/`.
- **Tests:** unchanged order ⇒ `KEEP` (never `CANCEL`) across a large synthetic event
  stream; a crossing order is never submitted; no fixed requote delay exists in any code
  path; rate limiter is free under normal load and only bounds excess; unknown order state
  is *detected*, not assumed; a fill triggers recomputation of **both** sides (I08).
- **Acceptance gate:** over a long synthetic stream, zero would-be taker submissions and a
  `KEEP` rate consistent with the reconciler spec; `LIVE_TRADING_ENABLED` still `False`.
- **Out of scope:** enabling live trading; queue instrumentation; risk halts.

---

## P8 — Queue and latency instrumentation

- **Goal:** measure the two properties the edge depends on.
- **Inputs:** Canonical §10, §22, §25, §27; Detailed §33, §35; O08, O09.
- **Outputs:** per-stage high-resolution timestamps across the whole critical path;
  `queue_ahead` estimation; the `AT_FRONT` / `PRICE_OK_BUT_DEEP` / `OFF_PRICE` /
  `NOT_QUOTING` / `STALE` classifier; latency and queue metrics.
- **Modules:** `src/maker5m/telemetry/`, `src/maker5m/execution/` (queue tracker).
- **Tests:** classifier correctness on constructed scenarios; instrumentation adds no
  blocking work to the hot path — asserted by a benchmark, not by inspection.
- **Acceptance gate:** a latency budget attributable stage-by-stage, and evidence that
  enabling instrumentation does not measurably slow `decide()`.
- **Out of scope:** closing O08/O09 (that is P15, and needs live-paper data).

---

## P9 — Risk / health / recovery

- **Goal:** the bot refuses to create new risk when it cannot trust its own state.
- **Inputs:** Canonical §28, §28.1; Detailed §38; I07, I17.
- **Outputs:** one-sided `band_hard` enforcement (pure, Plane 2); staleness, sequence-gap,
  clock-drift, API-error-rate, and rate-limit-uncertainty monitors; position and cost-ledger
  consistency reconciliation against the venue; kill switch; recovery/resync path.
- **Modules:** `src/maker5m/risk/`.
- **Tests:** each kill-switch condition trips and halts new orders while leaving existing
  balances untouched; a taker fill raises a risk event (I07); recovery from a simulated
  disconnect restores consistent state; **no halt path ever flattens inventory** (I15).
- **Acceptance gate:** every condition in Canonical §28.1 has a test that trips it, and no
  halt path sells, hedges, or flattens.
- **Out of scope:** settlement, redemption.

---

## P10 — Settlement / resolution / redeem

- **Goal:** correct winner determination and redemption. **O11 must be closed first.**
- **Inputs:** Canonical §18, §18.1; Detailed §32, §33; I15, I16; O11.
- **Outputs:** `ResolutionVerifier` with explicit source precedence and an explicit
  ambiguous branch; `Redeemer`; realised-PnL reconciliation against the ledger.
- **Modules:** `src/maker5m/settlement/`.
- **Tests:** cancel-at-`T0+280` then hold; no sell/hedge/merge/split/convert path exists in
  the codebase; ambiguous resolution halts rather than guesses; realised redemption
  reconciles to the P1 ledger exactly.
- **Acceptance gate:** end-to-end market lifecycle in paper mode ending in a correctly
  reconciled settlement, with realised PnL matching the ledger to the last unit.
- **Out of scope:** live capital.

---

## P11 — Telemetry persistence

- **Goal:** durable decision/fill records and post-market analytics — entirely in Plane 3.
- **Inputs:** Canonical §25, §26; Detailed §34; I19.
- **Outputs:** bounded non-blocking queue with drop accounting; durable sink; the full
  Canonical §25 decision and fill record schemas; the Canonical §26 per-market metric set;
  Term1/Term2 post-market decomposition.
- **Modules:** `src/maker5m/telemetry/`.
- **Tests:** hot path never blocks on the sink — proven by stalling the sink entirely and
  asserting `decide()` latency is unchanged; drops are counted, never silent; every field
  in Canonical §25 and §26 is present.
- **Acceptance gate:** with the sink artificially stalled, hot-path latency is statistically
  unchanged and drop counters increment.
- **Out of scope:** UI rendering.

---

## P12 — UI / control plane

- **Goal:** operator visibility and a narrow control channel.
- **Inputs:** `ARCHITECTURE_SSOT` §2, §4; I19.
- **Outputs:** read-only views over published snapshots (live accounting with both PnL
  branches, inventory, phase, orders, queue, latency, OPEN-parameter labels); a control
  channel that **enqueues control events** rather than mutating state.
- **Modules:** `src/maker5m/ui/`.
- **Tests:** the UI holds no lock reachable from Plane 1; killing the UI does not stop
  trading; control commands arrive as ordered events and are therefore replayable.
- **Acceptance gate:** UI process killed mid-market, trading continues uninterrupted, and
  the event journal shows no gap.
- **Out of scope:** anything that lets the UI write trading state directly.

---

## P13 — Live shadow / paper mode

- **Goal:** Canonical §34-L3. Run against the real live market with no real orders.
- **Inputs:** all prior phases; Canonical §34-L3; Detailed §35.
- **Outputs:** composition root wiring the full system in paper mode; every would-be quote
  and fill classified; ≥200 markets of recorded journals for the OPEN-item experiments.
- **Modules:** `src/maker5m/bot/`.
- **Tests:** long-run stability; state machine never wedges; low stale rate; recorded
  journals replay deterministically.
- **Acceptance gate:** sustained paper operation across many consecutive markets with
  correct accounting, a stable state machine, a low stale rate, and reasonable modelled
  queue position — the explicit L3 checklist in Canonical §34.
- **Out of scope:** real capital.

---

## P14 — Minimum-size live validation

- **Goal:** Canonical §34-L4. One market at a time, minimum capital.
- **Inputs:** the L4 preconditions in Canonical §34 and the full acceptance checklist in
  Canonical §35.
- **Outputs:** live trading enabled behind an explicit, deliberate switch with the risk
  limits of P9 active.
- **Tests:** realised maker fraction is 100%; zero taker fills; realised PnL reconciles to
  the ledger; realised prices and sizes match the strategy's intent.
- **Acceptance gate:** **every** box in Canonical §35 ticked with evidence, plus explicit
  human authorisation. This gate is never passed by an agent acting alone.
- **Out of scope:** scaling size; strategy changes.

---

## P15 — Research / optimization of OPEN items

- **Goal:** close the open items using the recorded evidence — the first phase in which
  changing the strategy is permitted at all.
- **Inputs:** `OPEN_ITEMS.md`; recorded journals from P13/P14; the P5 sweep runner.
- **Outputs:** closed items with recorded evidence, updated labels, and updated docs.
- **Order:** O10 and O04 are already required earlier (P1, P3). Then O01 → O09 → O08
  (they interact), O03, O02, O07, and O05/O06 **jointly** (they interact — a
  one-dimensional sweep of either is invalid).
- **Acceptance gate:** each closure carries its experiment, its data, and its label change.
  A closure without recorded evidence is rejected.
- **Out of scope:** changing a CONFIRMED strategy rule. Optimization beyond replication is
  a separate, explicitly-labelled experiment — never a silent edit to the replica
  (Canonical §37).

---

## Dependency notes

```text
O10 closes -> P1 can freeze scales
O04 decided -> P3 can claim correctness (P3 ships both policies meanwhile)
P5 must exist before P6, so live journals are replayable from the first recording
P8 must exist before P13, so paper mode produces the data P15 needs
O11 closes -> P10
```
