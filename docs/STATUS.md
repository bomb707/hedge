# Status

Compact project tracker. Update at **every accepted git boundary** — this file plus the
commit is the audit trail.

---

## LIVE TRADING: DISABLED

`maker5m.safety.LIVE_TRADING_ENABLED` is `False`. P7 adds a complete execution architecture
including a real authenticated write adapter, and **it cannot be armed**: `VenueAdapter.arm_live`
raises before any credential is read or any socket is opened.

P8 adds measurement only. The instrumented market run placed **zero** orders: no credential was
requested, no authenticated socket was opened, and the order side is a shadow simulation whose
figures are labelled `SHADOW_ESTIMATE`.

There is deliberately no `--live` flag, no `LIVE=true` environment variable, and no config key
that bypasses the constant. Every one of those would be a way to enable real trading without
the P14 review that is supposed to gate it. Unlocking requires a source edit and code review.

Asserted structurally in `tests/execution/test_safety.py`: the gate refuses and the transport
is never even constructed; no execution module reads the environment or implements a bypass
switch; secrets never appear in a repr; `SELL`, `FOK`, `FAK`, and `post_only=False` are not
representable; no `float()` exists on the order path; and no test constructs a real SDK client.

---

## P7 correction: concurrent dispatch

Independent review found that the P7 report claimed "independent UP/DOWN actions may dispatch
concurrently" while the implementation dispatched them **sequentially**. The claim was wrong,
and the test that appeared to support it —
`test_up_and_down_are_independent_so_they_may_dispatch_concurrently` — only proved the *plan*
was independent of evaluation order. It said nothing about the network calls, and would have
passed against a strictly sequential executor.

The gap was real: `records = [self._dispatch(side, now_ns) for side in plan.sides]` over
synchronous transport methods, with no async path in the codebase at all.

**Corrected.** `Executor.run_cycle_async` dispatches independent outcome requests with
`asyncio.gather` over `AsyncVenueAdapter`. The official SDK provides a genuine async client —
`AsyncSecureClient` with coroutine `create_limit_order` / `post_order` / `cancel_order` over
`httpx[http2]` — so this is real concurrency, not a blocking call wrapped in an executor.

Reservation is deliberately separated from dispatch. `Executor.reserve` allocates client order
ids, registers `PENDING_PLACE` / `PENDING_CANCEL`, and takes rate-limiter capacity
**synchronously, before any await**. A structural test asserts `reserve()` contains no
`Await` node: a suspension point in there would let a concurrent cycle observe no in-flight
request and create a duplicate.

Concurrency is across independent outcomes only. Within one side replacement remains
`CANCEL_THEN_PLACE`, and completion order cannot influence replay — authenticated order and
fill events re-enter through the P6 ingress merger and receive their ordinal there.

**The proof is discriminating.** Temporarily replacing `asyncio.gather` with a sequential loop
makes three barrier tests fail with `TimeoutError`; restoring it makes them pass. The barrier
holds each request inside the transport until the other side enters, so a sequential
implementation deadlocks rather than merely running slowly — no wall-clock timing is involved.

The synchronous `run_cycle` is retained for unit tests and is documented as test support, not
production.

---

## P8: what measurement found

Instrumentation only. **No strategy parameter was changed to improve any number below.**
Full evidence: [`evidence/P8-MEASUREMENT.md`](evidence/P8-MEASUREMENT.md).

Measured against one real market, `btc-updown-5m-1787652900`, 137,752 cycles,
`live_trading_enabled: false`, **0 orders sent**, 0 telemetry drops.

### The queue-preservation property holds

**`keep_ratio` = 0.99339** — 133,400 KEEPs across 134,287 cycles that had a live order. A
resting order survives unmodified through better than 99.3% of the cycles in which it exists.
That is the single most important behaviour in the system, and it is now measured on real data
rather than asserted. 1,049 slots acquired, 887 lost (510 `PRICE_CHANGED`, 377
`UNSAFE_REPLACEMENT`).

### Critical path

`receive_to_reconcile` p50 **323 µs**, p99 2.24 ms. The dominant stage is not the strategy:
`decide_duration` is p50 40 µs and `prepare_duration` p50 12 µs, while `reconcile_duration` is
p50 **172 µs**. See O15 — that is mostly an execution-layer data-structure cost, now quantified.

`real_order_rtt` is **UNRUN and deferred to P14**, not estimated. Every latency figure comes from
`time.perf_counter_ns()` alone; no exchange timestamp is ever subtracted from a local one.

### Finding: `POST_ONLY_BLOCK` on 119,116 sides

Of ~140,707 `NOT_QUOTING` sides, **119,116** were suppressed because the desired price would have
crossed or equalled the same-outcome ask. With zero synthetic spread this happens constantly.

**Reported, not acted on.** It may mean the zero-spread reading is wrong, or that the strategy
genuinely quotes only in the minority of moments when the book leaves room. Deciding between
those is a strategy question with an unresolved source conflict behind it (O01, O04); inventing a
spread to raise the quoting rate would be exactly the kind of optimization P8 forbids.

### Queue estimates are `SHADOW_ESTIMATE`, and biased

The order side is modelled, not real. `queue_ahead` p50 is 0 shares, p90 82, p99 249. Confidence
is `ESTIMATED`, `STALE`, or `UNKNOWN` — there is **no `EXACT`**, because the venue publishes no
queue index. The estimate is knowingly **optimistic**: a decrease in displayed size may include
size that joined after we did. Clamped to displayed size, otherwise uncorrected and documented.

None of this is evidence about the target wallet. It describes our strategy against real books.

### Instrumentation overhead

Deterministic benchmark, 1,560 cycles per configuration: `decide_ns` p50 **+1,761 ns (+6.4%)**,
`cycle_ns` p50 **+20,346 ns (+21.3%)**. Sampling is every 10th event, with fills, order states,
phase changes, and health events always traced — sampling reduces telemetry volume and is never
used to hide a latency figure.

Three earlier overhead numbers were wrong (+133%, +49%, +217%) and all three are retained in the
evidence manifest with the reason each was wrong. Two came from charging production work to
instrumentation; one from profiling that found per-call dict literals. None was visible by
reading the source.

### O08 / O09 remain OPEN

The latency distribution is now *measurable*, which is what P8 owed. It does not by itself
establish whether latency or queue position dominates fill probability — that needs real resting
orders, which P8 does not place. Both stay OPEN.

---

## Two different kinds of "done"

| | |
|---|---|
| **Implementation gate** | The code does what this phase specified, proven by tests. |
| **Empirical replication correctness** | The code does what the *target wallet* did. Only replay against the wallet's own history can establish that. |

---

## Current position

| | |
|---|---|
| **Current phase** | **P8 — Queue and latency instrumentation** |
| P8 implementation gate | **PASSED** |
| P7 implementation gate | **PASSED** (with the concurrency correction below) |
| Live execution | **NOT ARMED and not armable** — P14 owns that |
| Target-wallet empirical replay | **UNRUN / BLOCKED** |
| Real order round-trip latency | **UNRUN — P14** (measuring it requires sending an order) |
| Current branch | `feature/p8-queue-latency` |
| Venue-tick correction | `55977f4` — `fix: update current venue tick capabilities` |
| P7 boundary commit | `de96681` — `feat: add post-only execution reconciler` |
| P7 concurrency correction | `d333aeb` — `fix: dispatch independent outcome orders concurrently` |
| P8 boundary commit | `16bd4d4` — `feat: add queue and latency instrumentation` |
| Last accepted milestone | P7 — execution state + reconciler, corrected (`0f17bd2`) |
| Next milestone | P9 — awaiting acceptance of P8; not started |
| `main` | `0f17bd2` — fast-forwarded through P7 and its correction, pushed |
| Remote | `origin` → `https://github.com/bomb707/hedge.git` |

Nothing merged by merge commit, rebased, squashed, or force-pushed. `main` advances by
fast-forward only; all milestone branches are retained.

---

## The official SDK, reverified

| | |
|---|---|
| Distribution | `polymarket-client`, pinned **`==0.6.0`** |
| Import name | `polymarket` |
| Repository | `Polymarket/py-sdk` (`https://github.com/Polymarket/py-sdk`) |
| Requires | Python `>=3.11` |
| Legacy client | `py-clob-client` (0.34.6) — archived, **not used** |

Verified by introspecting the installed package, not from documentation alone:

```text
OrderType = Literal["GTC", "GTD", "FAK", "FOK"]
OrderSide = Literal["BUY", "SELL"]
TickSize  = Literal["0.1", "0.01", "0.005", "0.0025", "0.001", "0.0001"]

SecureClient.create_limit_order(*, token_id, price, size, side,
                                post_only: bool = False, expiration=None, ...) -> SignedOrder
SecureClient.post_order(signed_order) -> AcceptedOrder | RejectedOrder
SecureClient.cancel_order(*, order_id) / cancel_orders(*, order_ids)
```

Three findings that shaped the design:

- **`post_only` defaults to `False`.** The SDK's default is the unsafe one for this strategy,
  so it is passed explicitly on every call and no code path can omit it.
- **`order_type = "GTC" if expiration is None else "GTD"`.** We never pass an expiration, so
  every order is GTC. The strategy cancels explicitly at SETTLING, so GTD is unnecessary.
- **Signing resolves market metadata through an internal cache**, and on a tick mismatch it
  re-fetches over REST *before* signing. That is exactly the latency hazard Canonical §22
  warns about, so the cache is prewarmed during pre-arm and preparation validates the tick
  itself, which keeps the refetch branch unreachable. The allowance lookup and on-chain
  approval are a rejection-recovery fallback, not on the happy path.

---

## Venue tick correction

`SUPPORTED_TICK_SIZES` was missing `0.005` and `0.0025`, which the venue added after the set
was written. Corrected against the SDK's own `TickSize` literal. Both are exactly
representable at `PRICE_SCALE = 1_000_000` (`5_000` and `2_500` price units, each dividing the
scale), so this is a venue-capability correction and **O10 is not reopened**.

`MarketDefinition.tick` remains `0.01`. The replica's quote grid comes from the frozen
strategy evidence; the venue's legal increment is a separate concept.

---

## What P7 delivered

- **`PreparedOrder`** — `outcome`, `token_id`, `strategy_price`, `submission_price`,
  `strategy_size`, `submission_size`, `venue_tick`, `min_order_size`, `outcome_status`,
  `observed_ask`, plus derived `size_quantization_delta` and `price_unchanged`. The
  `DesiredOrder` is never mutated.
- **Post-only guard** — typed outcomes: `SAFE`, `NO_BOOK`, `WOULD_CROSS`, `OFF_VENUE_TICK`,
  `OUT_OF_VENUE_RANGE`, `BELOW_MIN_SIZE`, `ZERO_AFTER_QUANTIZATION`, `UNKNOWN_VENUE_RULES`.
  Judged against the **same outcome's** observed ask; equality is blocked; the DOWN ask is
  never inferred from UP.
- **`LiveOrderTable`** — many orders, not one per side, because a cancel racing an
  acknowledgement cannot otherwise be represented. `PENDING_PLACE`, `LIVE`,
  `PARTIALLY_FILLED`, `PENDING_CANCEL`, `FILLED`, `CANCELLED`, `REJECTED`, `UNKNOWN`, with a
  deterministic per-outcome view for the reconciler and idempotent updates.
- **Pure `reconcile`** — `NOTHING` / `KEEP` / `PLACE` / `CANCEL` / `REPLACE` / `WAIT` /
  `BLOCKED`, each with a typed `SideReason`. No clock, no network, no logging.
- **Token bucket** — free under normal load, bounds excess, with reserved cancel capacity so a
  cancel can never be starved by placements. Time is an argument, never read internally.
- **`ReplacementTracker`** — `CANCEL_THEN_PLACE` with generation-bound staleness.
- **`VenueAdapter`** — the single SDK boundary, with `RecordingTransport` for tests.
- **User-stream normalization** — venue order updates and trades become P2 `OrderStateEvent`
  and `OwnFill`. P2 semantics were not bent to match SDK shapes.

### Measured

Long unchanged stream, 5 000 decision cycles × 2 sides = 10 000 side-decisions:

```text
PLACE     2        CANCEL    0        WAIT      0
KEEP   9998        REPLACE   0        BLOCKED   0

network requests issued: 2 placements, 0 cancels
```

Microbenchmarks (median): `prepare_order` 3.07 µs · `reconcile` KEEP 3.26 µs · `reconcile`
REPLACE 3.30 µs. KEEP is marginally cheaper, but both are dominated by frozen-dataclass
construction rather than by logic — the same effect measured in P4. P8 owns end-to-end latency.

---

## Tests

| | |
|---|---|
| Status | **green** |
| Suite | 1 176 passed (1 065 at the P7 boundary; +111 in P8) |
| `ruff check` / `ruff format --check` | clean |
| `mypy` (strict) | clean — `src/`, `tests/`, `tools/`; **zero `type: ignore` in `execution/`** |
| Runtime dependencies | `websockets`, `polymarket-client==0.6.0` — both pinned or bounded |
| Dev dependencies | `pytest`, `mypy`, `ruff`, `pytest-asyncio` |

Two P6 guards were **rescoped, not relaxed**: they were written when the repository contained
no write path at all. The market-data plane must still contain no credential material, and
`execution/credentials.py` is now asserted to be the *single* module where such material is
even named.

Several structural guards read the **code** rather than the source text, because these modules
deliberately *describe* what they refuse to implement — a `min_requote_ms` delay, a `--live`
flag, `post_only=False`, `wait_for_order_fill_settlement`. A plain text scan would trip over
its own documentation.

---

## Open strategy items

Full detail in [`OPEN_ITEMS.md`](OPEN_ITEMS.md). **P8 closed none and added one: O15**, a
measured execution-layer latency defect. O08 and O09 are now *measurable* but remain OPEN.

```text
O01 quote-centre source            OPEN      O08 latency for queue dominance OPEN
O02 volatility sigma               OPEN      O09 spot-to-CLOB timing model   OPEN
O03 base-lot L selection rule      OPEN      O10 venue precision / scales    CLOSED
O04 grid-target selection          OPEN      O11 resolution source           OPEN
O05 endgame tilt magnitude         FITTED    O12 BTC spot scale              CLOSED
O06 endgame gate magnitude         FITTED    O13 tick tie-breaking           OPEN
O07 fee/rebate calibration         OPEN      O14 strike chaining unverified  OPEN
                                             O15 current() linear in orders  OPEN
```

Replacement sequencing and the rate budget are labelled `OPERATIONAL`, not strategy OPEN
items: they are ordinary execution-policy choices that do not change the reconstructed
strategy.

---

## P6 live capture evidence (retained)

Two consecutive full `btc-updown-5m-*` markets captured read-only and verified end to end by
the P5 replay engine: every decision reproduced, final state matched, canonical bytes
round-tripped identically. Manifests are committed under [`docs/evidence/`](evidence/).

The journals are 150-200 MB each — every step records the complete `DecisionResult` — so they
are stored outside Git and identified by digest:

```text
path        /home/hr/p6-captures/btc-updown-5m-1787647500.journal.ndjson
bytes       159,464,961
sha256      dbd436b4d2bb46c23182390256e07ff8712246d311c83b52ecb08048e717d3aa
steps       108,617

path        /home/hr/p6-captures/btc-updown-5m-1787647200.journal.ndjson
bytes       193,408,732
steps       130,374
sha256      see docs/evidence/p6-capture-btc-updown-5m-1787647200.manifest.json

reproduce   .venv/bin/python tools/capture_market.py <output-directory>
```

These are one machine's local disk and are **not** durable artefacts; the committed manifests
are the record and the command above regenerates them. Observed precision stayed within the
frozen scales throughout (price 0-3 decimals, size 0-2, BTC 8), and the venue announced four
`tick_size_change` events during one market while the replica stayed on its `0.01` grid.

---

## Verification ladder

Canonical §34.

```text
L0  arithmetic                 PASSED
L1  historical reconstruction  BLOCKED  (needs target-wallet ledger data, not in repo)
L2  offline replay             ENGINE PASSED (on real data) / TARGET-WALLET EMPIRICAL UNRUN
L3  live paper                 not started  (P13 - sustained validation programme)
L4  minimum-size live          not started  (P14)
```

**L1 remains UNRUN and is not relabelled.** **L2's empirical half remains UNRUN**: the P6
journals record our own bot's trajectory, not the target wallet's history.

P7 performed **no live order test**: no credential was requested, no authenticated socket was
opened, and no order of any size was placed. P14 owns minimum-size live execution.

---

## Update ritual

At each accepted boundary, update: current phase, branch, boundary commit, last accepted
milestone, next milestone, blockers, open-item labels, and test status. Record the
implementation gate and the empirical status **separately**. If a gate was not actually
executed, say so — code existing is not completion
([`DEVELOPMENT_PLAN.md`](DEVELOPMENT_PLAN.md), rule 1).
