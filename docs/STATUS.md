# Status

Compact project tracker. Update at **every accepted git boundary** — this file plus the
commit is the audit trail.

---

## LIVE TRADING: DISABLED

`maker5m.safety.LIVE_TRADING_ENABLED` is `False`. P7 adds a complete execution architecture
including a real authenticated write adapter, and **it cannot be armed**: `VenueAdapter.arm_live`
raises before any credential is read or any socket is opened.

There is deliberately no `--live` flag, no `LIVE=true` environment variable, and no config key
that bypasses the constant. Every one of those would be a way to enable real trading without
the P14 review that is supposed to gate it. Unlocking requires a source edit and code review.

Asserted structurally in `tests/execution/test_safety.py`: the gate refuses and the transport
is never even constructed; no execution module reads the environment or implements a bypass
switch; secrets never appear in a repr; `SELL`, `FOK`, `FAK`, and `post_only=False` are not
representable; no `float()` exists on the order path; and no test constructs a real SDK client.

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
| **Current phase** | **P7 — Execution state + post-only order reconciler** |
| P7 implementation gate | **PASSED** |
| Live execution | **NOT ARMED and not armable** — P14 owns that |
| Target-wallet empirical replay | **UNRUN / BLOCKED** |
| Current branch | `feature/p7-execution` |
| Venue-tick correction | `55977f4` — `fix: update current venue tick capabilities` |
| P7 boundary commit | recorded by the immediately following commit on this branch |
| Last accepted milestone | P6 — read-only market-data adapters (`6a50794`, tip `d6851a4`) |
| Next milestone | **P8 — Queue and latency instrumentation** |
| `main` | `d6851a4` — fast-forwarded to the accepted P6 HEAD, pushed |
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
| Suite | 1 065 passed (896 at the P6 boundary; +169 in P7) |
| `ruff check` / `ruff format --check` | clean |
| `mypy` (strict) | clean — `src/`, `tests/`, `tools/`; **zero `type: ignore` in `execution/`** |
| Runtime dependencies | `websockets`, `polymarket-client==0.6.0` — both pinned or bounded |

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

Full detail in [`OPEN_ITEMS.md`](OPEN_ITEMS.md). **P7 closed none and added none.**

```text
O01 quote-centre source            OPEN      O08 latency for queue dominance OPEN
O02 volatility sigma               OPEN      O09 spot-to-CLOB timing model   OPEN
O03 base-lot L selection rule      OPEN      O10 venue precision / scales    CLOSED
O04 grid-target selection          OPEN      O11 resolution source           OPEN
O05 endgame tilt magnitude         FITTED    O12 BTC spot scale              CLOSED
O06 endgame gate magnitude         FITTED    O13 tick tie-breaking           OPEN
O07 fee/rebate calibration         OPEN      O14 strike chaining unverified  OPEN
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
