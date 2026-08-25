# P8 measurement evidence

> ## SUPERSEDED FOR QUEUE-METRIC ACCEPTANCE
>
> Independent review found a correctness defect in the shadow queue model used for this run.
> Slots followed the **desired price** rather than the executable order lifecycle, so a quote
> the reconciler refused to submit still acquired a slot, aged it, and credited itself with
> every subsequent depth decrease at that level. This run recorded **119,116**
> `POST_ONLY_BLOCK` sides, so the effect is large, not marginal.
>
> Therefore, from this document:
>
> * `queue_ahead` (all quantiles), `AT_FRONT`, `PRICE_OK_BUT_DEEP`, and every shadow slot
>   count are **superseded** and must not be used for acceptance. They are retained because
>   deleting a wrong measurement is worse than labelling it.
> * The **latency** figures, the action counters, and `keep_ratio` do not depend on the queue
>   model and remain valid, with one correction noted below: `execution_queue_loss_actions`
>   (887) and shadow slot losses (1,049) were reported side by side as though they were the
>   same metric. They are different metrics and are now named apart.
> * The **instrumentation overhead** figure here (+21.3%) was measured with a method that has
>   since been replaced by a paired, interleaved, tier-split benchmark.
>
> Corrected evidence: [`P8B-MEASUREMENT.md`](P8B-MEASUREMENT.md) (correctness), then
> [`P8C-PERFORMANCE-CLOSURE.md`](P8C-PERFORMANCE-CLOSURE.md) (performance). Correction
> branches `fix/p8-measurement-hotpath-closure` and `fix/p8-telemetry-offload`.

Measurement only. **No strategy parameter was changed to improve any number in this
document.** Everything here describes what the current strategy *does*; nothing here has been
acted upon. The reductions these numbers argue for are recorded as open items, not applied.

## Provenance

| Field | Value |
| --- | --- |
| Kind | `INSTRUMENTED_SHADOW_MEASUREMENT` |
| Market slug | `btc-updown-5m-1787652900` |
| Market id | `0x302cba040fc6534c04ea3720e3be3510eb80184b99bb440f2b0d34f54329b70b` |
| `live_trading_enabled` | **false** |
| Orders sent to any venue | **0** |
| Credentials used | none — no key, no signing, no authenticated socket, no write endpoint |
| Cycles observed | 137,752 |
| Journal steps | 141,526 |
| Sampling | every 10th event; `OwnFill` / `OrderStateEvent` / `PhaseEvent` / `HealthEvent` always traced |
| Telemetry accepted / dropped | 14,920 / **0** |
| Raw data | `p8-measurement-btc-updown-5m-1787652900.json` |

### This is not target-wallet evidence

The order side is a **shadow**: a modelled venue acknowledgement, not a real resting order.
Queue figures are labelled `SHADOW_ESTIMATE` throughout. They describe how *our* strategy would
have behaved against real book data. They are not observations of the target wallet, and they
close nothing about it.

## Critical-path latency (ns)

| Stage | n | p50 | p90 | p95 | p99 | max |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `spot_receive_to_decide` | 5,225 | 74,215 | 248,606 | 341,138 | 467,759 | 933,030 |
| `clob_receive_to_decide` | 132,523 | 133,564 | 503,796 | 580,845 | 937,830 | 211,933,215 |
| `phase_receive_to_decide` | 4 | 50,616 | 163,423 | 163,423 | 163,423 | 163,423 |
| `fill_receive_to_decide` | **0** | — | — | — | — | — |
| `decide_duration` | 137,752 | 40,218 | 149,407 | 170,223 | 221,203 | 160,663,638 |
| `prepare_duration` | 137,752 | 12,350 | 51,528 | 58,284 | 83,966 | 148,799,575 |
| `reconcile_duration` | 137,752 | 171,659 | 519,893 | 833,462 | 1,563,535 | 3,068,209 |
| `receive_to_reconcile` | 137,752 | 323,138 | 1,031,208 | 1,423,458 | 2,243,373 | 212,132,016 |
| `keep_cycle` | 6,795 | 287,056 | 658,854 | 876,886 | 1,620,708 | 160,893,525 |
| `acting_cycle` | 1,269 | 303,389 | 728,355 | 970,432 | 2,044,884 | 3,829,865 |

`real_order_rtt`: **UNRUN — deferred to P14.** Submit-to-acknowledge cannot be measured without
sending an order, and P8 sends none. It is absent from this table rather than estimated.

All values come from `time.perf_counter_ns()` only. No exchange timestamp is subtracted from a
local timestamp anywhere in the measurement path; the two clock domains are never mixed.

## Queue position (`SHADOW_ESTIMATE`) — SUPERSEDED, see the banner above

| Metric | n | p50 | p90 | p95 | p99 | max |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `queue_ahead` (share units) | 134,797 | 0 | 82,000,000 | 128,420,000 | 248,760,000 | 529,430,000 |

Divide by 1,000,000 for shares: p50 **0 shares ahead**, p90 **82**, p99 **249**.

| Slot metric | Value |
| --- | ---: |
| Slots acquired | 1,049 |
| Slot-cycles kept | 252,864 |
| Slots lost | 887 |
| Lost — `PRICE_CHANGED` | 510 |
| Lost — `UNSAFE_REPLACEMENT` | 377 |

Confidence is `ESTIMATED`, `STALE`, or `UNKNOWN`. There is deliberately **no `EXACT`**: the venue
does not publish a queue index, so none is claimed.

**Known optimistic bias.** Depth ahead is decremented by observed decreases in displayed size at
our price. A decrease measured against the previous observation may include size that joined
*after* we did, so the estimate can read lower than reality. It is clamped to the displayed size
(`ahead = min(ahead, displayed)`), because we cannot be behind more size than is shown, but the
bias is not otherwise corrected. Increases are ignored, since new same-price orders join behind us.

## Action and quality counters

The `KEEP` / `NOTHING` / `BLOCKED` / `PLACE` / `REPLACE` / `CANCEL` counts and `keep_ratio`
below are unaffected by the queue defect. The `AT_FRONT` and `PRICE_OK_BUT_DEEP` quality counts
**are** affected and are superseded.

| Action | Count |
| --- | ---: |
| `KEEP` | 133,400 |
| `NOTHING` | 21,591 |
| `BLOCKED` | 118,739 |
| `PLACE` | 887 |
| `REPLACE` | 510 |
| `CANCEL` | 377 |

**`keep_ratio` = 0.99339** (133,400 keeps over 134,287 cycles that had a live order). The
queue-preservation property (I-KEEP-on-remaining) holds on real market data: a resting order
survives, unmodified, through better than 99.3% of the cycles in which it exists.

| Quality | Count |
| --- | ---: |
| `NOT_QUOTING` | 140,707 |
| `AT_FRONT` | 97,655 |
| `PRICE_OK_BUT_DEEP` | 37,142 |

| Reason | Count |
| --- | ---: |
| `QUOTING` | 134,797 |
| **`POST_ONLY_BLOCK`** | **119,116** |
| `CENTRE_UNAVAILABLE` | 10,390 |
| `PHASE_NOT_QUOTING` | 9,212 |
| `ENDGAME_GATE` | 1,989 |

`AT_FRONT` means the estimate places us at zero depth ahead. `PRICE_OK_BUT_DEEP` carries **no
invented depth threshold** — it means the desired price is quotable and the estimate is non-zero.

## Instrumentation overhead — method superseded

Deterministic benchmark, same corpus and same ordering both times, 40 repeats × 39 events =
1,560 cycles per configuration. Raw data: `p8-instrumentation-overhead.json`.

| Metric | p50 off | p50 on | Δ | p95 off | p95 on | Δ |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `decide_ns` | 27,581 | 29,342 | **+1,761 (+6.4%)** | 33,848 | 35,389 | +1,541 (+4.6%) |
| `cycle_ns` | 95,393 | 115,739 | **+20,346 (+21.3%)** | 147,834 | 172,186 | +24,352 (+16.5%) |

Measured, not inferred from source inspection.

### Superseded measurements, retained for the record

Three earlier runs were wrong, and the errors mattered:

1. **+133.3% (p50 `cycle_ns` +32,047 ns).** The `enabled=False` path returned before
   `prepare_both_sides` and `reconcile` — work production performs on every cycle regardless.
   Charging it to instrumentation overstated the cost roughly threefold.
2. **+49.3%, then +48.0% after profiling.** Rescoped so `enabled` gates only measurement.
   `cProfile` then showed `LatencyBook.by_kind` and `_strategy_reason` building dict literals on
   every call (28,160 enum hashes per 1,560 cycles); both were hoisted to constants.
3. **+216.7%.** `_apply_shadow` was still inside the measurement gate, so the OFF run reconciled
   against an *empty* order table while the ON run reconciled against 400 orders. That gap is
   venue-simulation cost, not telemetry cost. Moving shadow application outside the gate — it
   mutates only the executor's order table — produced the +21.3% figure above.

Each of these would have been invisible to source inspection and each produced a confidently
wrong number.

## Finding: `LiveOrderTable.current()` is linear in orders ever placed

Surfaced by profiling, not by reading the code. `current()` calls `occupying()`, which filters
and sorts **every order the table has ever held**, including terminal ones, on every call — twice
per cycle. Direct measurement, one live order plus N retained terminal orders:

| Retained terminal orders | ns per cycle (2 × `current()`) |
| ---: | ---: |
| 0 | 1,212 |
| 50 | 14,067 |
| 200 | 52,401 |
| 500 | 127,133 |
| 1,049 | 265,267 |
| 2,000 | 516,257 |

Linear, ~258 ns per retained order per cycle. This market placed **1,049** orders, so by the end
of a single 5-minute market roughly **265 µs per cycle** goes to re-scanning dead orders — more
than the entire measured `receive_to_reconcile` p50 of 323 µs, and the reason `reconcile_duration`
(p50 171 µs) is the dominant stage in the table above.

This is a production hot-path cost, not an instrumentation artefact. It is **not fixed here**:
P8 measures. Recorded as **O15**.

## Reproducing

```bash
.venv/bin/python tools/measure_market.py <output-directory>   # waits for the next T0, ~9 min
.venv/bin/python tools/instrumentation_overhead.py            # deterministic, seconds
```

Both are read-only. `LIVE_TRADING_ENABLED` is `False` and no code path in either tool can reach
a venue write endpoint.
