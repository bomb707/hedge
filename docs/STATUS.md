# Status

Compact project tracker. Update at **every accepted git boundary** — this file plus the
commit is the audit trail.

---

## LIVE TRADING: DISABLED

`maker5m.safety.LIVE_TRADING_ENABLED` is `False`. P6 added the first network access, and it is
**strictly public and read-only**: no order endpoint, no credential, no API key, no wallet
key, no signing, no write path of any kind. `tests/feeds/test_read_only_guarantees.py` asserts
this structurally — it scans every module for credential and signing markers, forbids HTTP
POST, restricts the WebSocket endpoints to the two public market-data streams, checks the
Polymarket subscription carries no authentication, and forbids reading the environment.

Unlocking is a **P14** decision, gated on the full Canonical §35 checklist plus explicit human
authorisation. It is not a configuration knob.

---

## Two different kinds of "done"

| | |
|---|---|
| **Implementation gate** | The code does what this phase specified, proven by tests. |
| **Empirical replication correctness** | The code does what the *target wallet* did. Only replay against the wallet's own history can establish that. |

P6 adds a third distinction worth keeping separate: capturing our **own** bot's trajectory on
real market data is not evidence about the target wallet's trajectory.

---

## Current position

| | |
|---|---|
| **Current phase** | **P6 — Polymarket / BTC market-data adapters** |
| P6 implementation gate | **PASSED** |
| P6 live acceptance gate | **PASSED** — two full markets captured read-only and verified by P5 |
| Target-wallet empirical replay | **UNRUN / BLOCKED** — no reconstructed wallet journal exists |
| Current branch | `feature/p6-market-data` |
| P6 boundary commit | `6a50794` — `feat: add read-only live market-data adapters` |
| Last accepted milestone | P5 — deterministic replay engine (`ab22f3b`, tip `c8463e3`) |
| Next milestone | **P7 — Execution state + post-only order reconciler** |
| `main` | `c8463e3` — fast-forwarded to the accepted P5 HEAD, pushed |
| Remote | `origin` → `https://github.com/bomb707/hedge.git` |

### Branch policy

```text
main                                latest ACCEPTED milestone
feature/<phase>                     active phase development
fix/<subject>                       accepted corrections, merged forward by fast-forward
bootstrap/phase-0 … feature/p5-replay   retained milestone boundaries
```

Nothing merged by merge commit, rebased, squashed, or force-pushed.

---

## Blockers

| Blocker | Blocks | Detail |
|---|---|---|
| **No reconstructed target-wallet journal** | **L1, L2-empirical, O04's closure** | The single missing artefact. P5 built the machinery, P6 proved it works on real data — but only on *our* bot's trajectory. |
| **O14** — strike chaining unverified, no strike published | any strike-dependent model (O01 option 2, O02) | New in P6, see below. |
| **O11** — authoritative resolution source unnamed | P10 | Both sources say "prefer on-chain" without naming a source, depth, or timeout. |

`O12` and the residual half of `O10` are now **closed**. The P5 STATUS said both "O12 blocks
P6" and "nothing blocks P6"; the accurate statement was **P6 could start but could not pass
its gate until O12 was closed from real feed evidence**. It has been.

Nothing blocks P7.

---

## Open strategy items

Full detail in [`OPEN_ITEMS.md`](OPEN_ITEMS.md).

```text
O01 quote-centre source            OPEN      O08 latency for queue dominance OPEN
O02 volatility sigma               OPEN      O09 spot-to-CLOB timing model   OPEN
O03 base-lot L selection rule      OPEN      O10 venue precision / scales    CLOSED (kernel + live traffic)
O04 grid-target selection          OPEN      O11 resolution source           OPEN
O05 endgame tilt magnitude         FITTED    O12 BTC spot scale              CLOSED (self-describing kept)
O06 endgame gate magnitude         FITTED    O13 tick tie-breaking           OPEN
O07 fee/rebate calibration         OPEN      O14 strike chaining unverified  OPEN  (new)
```

**P6 closed O10 and O12 from real evidence, and closed nothing else.** A live journal of our
own bot's decisions says nothing about which strategy parameter the target wallet used, so
O04 in particular is untouched.

### O12 — closed: the self-describing representation is kept

Not "a fixed BTC scale was found". Binance publishes `PRICE_FILTER.tickSize = "0.01000000"`
and `quotePrecision = 8` **per symbol**, and sends prices as decimal strings; 4 357 live
messages all carried 8 decimals and every one round-trips exactly through `BtcPrice`. Because
precision is symbol metadata rather than a global constant, deriving the scale from the string
is exact by construction and a frozen global scale would just be the guess this item existed
to avoid.

### O14 — new: the strike is not published, and chaining is unverified

Canonical §6.2 states `coinPriceStart[N] == coinPriceEnd[N-1]`. **Neither field exists** in the
Gamma event payload or the CLOB market payload. Resolution is a Chainlink BTC/USD TWAP over
the window against "the price at the beginning of that range" — a reference price that is
real but not exposed as a queryable field.

So `MarketDefinition.strike` is left `None` and `strike_available` is `False`. Nothing was
fabricated; a wrong strike would feed straight into any future fair-value model. Pre-arm does
not need it.

Separately, and in the other direction: the venue's own `cryptoMarketConfig` reads
`{"twapEnabled": true, "twapLookbackSeconds": 60, "duration": "5m"}`, which **corroborates the
60-second TWAP settlement of Canonical §7** from live metadata.

---

## What P6 delivered

- **Polymarket market-data adapter** — public WebSocket, `book` / `price_change` /
  `tick_size_change` / `best_bid_ask` / `last_trade_price`, documented `PING`/`PONG`
  heartbeat, bounded exponential backoff with jitter.
- **External BTC adapter** — Binance `btcusdt@aggTrade`. Chosen because its `p` field is a
  single exact traded price; `bookTicker` was rejected because deriving one `SpotTick` from a
  bid and an ask needs a midpoint rule, and inventing one silently would be an unrecorded
  normalisation decision.
- **Ingress clock** — wall-anchored monotonic, so it is wall-aligned *and* immune to a
  backwards NTP step. `EventMeta.timestamp` is **synchronized local ingress time**, never a
  venue timestamp; venue stamps stay in feed diagnostics.
- **Single ingress merger** — the only assigner of `ingress_ordinal`, so there is exactly one
  legal production event order for P5 to replay.
- **Discovery and pre-arm** — the `btc-updown-5m-<T0>` slug is addressable directly and the
  next market exists well before it starts. Fails closed on zero or ambiguous matches.
- **Phase scheduler** — boundary events at exactly `T0+3 / +240 / +280 / +300`, through the
  same merger, so a quiet market still crosses phases on time.
- **Conservative continuity** — no venue sequence is invented; any disconnect, malformed
  message, unknown token, or resubscription drops the book and requires a fresh snapshot.
- **Venue rules kept separate from strategy** — `tick_size_change` is recorded, never applied
  to `MarketDefinition.tick`.
- **O10 guard on every message** — a non-representable value raises rather than rounds.

### One real bug the live run found

`MarketState.initial` parks the state clock at `T0`, so the first pre-arm event — arriving
*before* `T0` — was rejected as a decreasing timestamp. Fixed without touching P2: messages
before `T0` are consumed and applied to the book trackers but produce no Plane 2 event. That
is what pre-arm is for (Canonical §21) — at `T0` the book is already warm and no discovery
work remains — and it keeps P5 able to rebuild the identical initial state from the header
alone. Integration surfacing this is exactly why the phase exists.

---

## Live capture evidence

Two consecutive full markets, captured read-only and verified end to end by the P5 engine.

| | primary | second |
|---|---|---|
| slug | `btc-updown-5m-1787647500` | `btc-updown-5m-1787647200` |
| decision steps | 108 617 | 130 374 |
| phases covered | all five | all five |
| `BookUpdate` / `SpotTick` / `HealthEvent` / `PhaseEvent` | 102 296 / 4 111 / 2 206 / 4 | 122 426 / 4 330 / 3 614 / 4 |
| CLOB messages · price_changes · books | 104 298 · 100 092 · 2 204 | — |
| PONGs · reconnects · malformed · unhandled | 33 · 0 · 0 · 0 | — |
| venue `tick_size_change` observed | **4** | 0 |
| **P5 verified every decision** | **yes** | **yes** |
| **final state matches** | **yes** | **yes** |
| **canonical bytes round-trip identical** | **yes** | **yes** |
| pre-arm slack before next `T0` | 500.5 s | 462.6 s |

Observed precision, all within the frozen six-decimal scales:

```text
polymarket price   0-3 decimals   829,146 samples   finest example "0.001"
polymarket size    0-2 decimals   425,320 samples   finest example "95259.64"
binance  price     8 decimals       4,357 samples   e.g. "80099.79000000"
```

Clock health: 107 458 samples, max absolute offset 2.745 s against venue source timestamps
(network latency plus venue clock; measured, never corrected).

### The journals are stored outside Git

Every step records the **complete** `DecisionResult`, which is deliberate — a decision can be
wrong in its centre or eligibility while the emitted order looks identical — so the canonical
journal runs about 1.5 kB per step. A single market is 150–200 MB, which does not belong in
Git history.

```text
path        /home/hr/p6-captures/btc-updown-5m-1787647500.journal.ndjson
bytes       159,464,961
sha256      dbd436b4d2bb46c23182390256e07ff8712246d311c83b52ecb08048e717d3aa
steps       108,617

path        /home/hr/p6-captures/btc-updown-5m-1787647200.journal.ndjson
bytes       193,408,732
sha256      see docs/evidence/p6-capture-btc-updown-5m-1787647200.manifest.json
steps       130,374

reproduce   .venv/bin/python tools/capture_market.py <output-directory>
```

These are on a single machine's local disk and are **not** durable artefacts. The committed
manifests in [`docs/evidence/`](evidence/) are the record; the journals themselves are
regenerable by the command above. P11 owns durable telemetry persistence, and the per-step
size is a real input to that design.

---

## Tests

| | |
|---|---|
| Status | **green** |
| Suite | 896 passed (690 at the P5 boundary; +206 in P6) |
| `ruff check` / `ruff format --check` | clean |
| `mypy` (strict) | clean — `src/`, `tests/`, `tools/` |
| Runtime dependencies | one: `websockets`. HTTP discovery uses the standard library. |

Real-message conformance fixtures under `tests/feeds/fixtures/`, all labelled
`REAL_PUBLIC_FIXTURE` with capture date, source, endpoint, and market/symbol identity:
Polymarket `book`, `price_change`, `last_trade_price`, Gamma and CLOB discovery excerpts,
Binance `aggTrade` and `exchangeInfo`.

**Two documented event types have no raw fixture.** `tick_size_change` and `best_bid_ask` are
sporadic — four tick changes occurred in one captured market and none in the other, and a
further three-minute targeted listen caught neither. Their handling is unit-tested against the
documented shape, and the live capture proves the `tick_size_change` path runs on real traffic
(the manifest records four of them). That is weaker than a raw fixture and is recorded as such
rather than glossed over; a fixture should be added opportunistically.

---

## Verification ladder

Canonical §34.

```text
L0  arithmetic                 PASSED
L1  historical reconstruction  BLOCKED  (needs target-wallet ledger data, not in repo)
L2  offline replay             ENGINE PASSED (now on real data) / TARGET-WALLET EMPIRICAL UNRUN
L3  live paper                 not started  (P13 - sustained validation programme)
L4  minimum-size live          not started  (P14)
```

**L1 remains UNRUN and is not relabelled.**

**L2 stays split.** The replay engine is now proven against 239 k real decision steps rather
than only synthetic ones — a genuine strengthening. The *empirical* half of Canonical §34-L2,
reproducing the **target wallet's** behaviour, is still **UNRUN**: these journals record our
own bot's decisions, not the wallet's history.

P6's capture is labelled `LIVE_PAPER` — real market data, no real orders. That is the accurate
provenance, and it is not the same as P13, which is the sustained live-paper *validation
programme* over ≥200 markets.

---

## Update ritual

At each accepted boundary, update: current phase, branch, boundary commit, last accepted
milestone, next milestone, blockers, open-item labels, and test status. Record the
implementation gate and the empirical status **separately**. If a gate was not actually
executed, say so — code existing is not completion
([`DEVELOPMENT_PLAN.md`](DEVELOPMENT_PLAN.md), rule 1).
