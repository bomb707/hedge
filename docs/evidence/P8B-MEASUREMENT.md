# P8 corrected measurement evidence

Supersedes the queue metrics in [`P8-MEASUREMENT.md`](P8-MEASUREMENT.md), which are retained
and labelled rather than deleted. Produced on `fix/p8-measurement-hotpath-closure` after
independent review found three closure issues in the accepted P8 work.

Measurement only. **No strategy parameter was changed to improve any number here.** No
positive spread was introduced. `strategy/`, `accounting/`, `market/`, `numeric/`, and the
reconciler are untouched by this correction; the only execution change is the `LiveOrderTable`
occupancy index.

## Provenance

| Field | Value |
| --- | --- |
| Kind | `INSTRUMENTED_SHADOW_MEASUREMENT` |
| Market slug | `btc-updown-5m-1787658900` |
| Market id | `0xb133a93c539e9359d5248ce877340abb9beab5b30052ff798fffeffcd0186e5e` |
| Run (UTC) | 2026-08-25T12:00:13Z |
| `live_trading_enabled` | **false** |
| Orders sent to any venue | **0** |
| Credentials used | none — no key, no signing, no authenticated socket, no write endpoint |
| Cycles observed | 117,772 |
| Journal steps | 120,490 |
| Sampling | every 10th event; `OwnFill` / `OrderStateEvent` / `PhaseEvent` / `HealthEvent` and every acting cycle always traced |
| Telemetry accepted / dropped | 12,384 / **0** |
| Feed | 116,288 CLOB messages, 3,583 spot messages, 0 malformed, 0 unhandled, 1 reconnect |
| Raw data | `p8b-measurement-btc-updown-5m-1787658900.json` |

The order side remains a **shadow**: a modelled venue acknowledgement, not a real resting
order. Queue figures are labelled `SHADOW_ESTIMATE`. They describe how *our* strategy would
have behaved against real book data. They are not observations of the target wallet and they
close nothing about it. **O08 and O09 remain OPEN.**

## Which counters are per-cycle and which are sampled

This changed in the correction and matters for reading every table below.

* **Per cycle, always** — reconcile actions, `cycles_with_live_order`, `keep_ratio`, execution
  queue-loss counts, and all shadow slot state. Skipping these for unsampled events would make
  the queue estimate depend on the sampling rate.
* **Traced cycles only (1-in-10 plus every acting cycle)** — stage latencies, `queue_ahead`
  samples, and the execution-quality classification. Classification is emission, and emission
  is what sampling is for.

So `BLOCKED` (97,534) is an exact count over all sides, while `POST_ONLY_BLOCK` (10,117) is its
deterministic 1-in-10 sample. The two are consistent: 97,534 / 10 ≈ 9,753.

## Corrected queue results (`SHADOW_ESTIMATE`)

| Metric | Corrected | First run (defective) |
| --- | ---: | ---: |
| Shadow slots acquired | **462** | 1,049 |
| Shadow slot-cycles kept | **106,380** | 252,864 |
| Shadow slot losses | **462** | 1,049 |
| — `PRICE_CHANGE` | 259 | 510 |
| — `UNSAFE_REPLACEMENT` | 203 | 377 |
| Shadow slots still open at end | 0 | — |

Slots acquired now equals `PLACE` actions exactly (462), which is the defining property of the
corrected model: **a slot exists only where an order was dispatched.** Losses reconcile exactly
to their typed reasons (259 + 203 = 462), asserted by test.

| Queue metric | n | p50 | p90 | p95 | p99 | max |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `queue_ahead` (share units) | 11,322 | 0 | 77,360,000 | 138,610,000 | 366,880,000 | 972,060,000 |

In shares: p50 **0**, p90 **77**, p95 **139**, p99 **367**.

Confidence remains `ESTIMATED` / `STALE` / `UNKNOWN` with **no `EXACT`** — the venue publishes
no queue index. The documented optimistic bias is unchanged: a decrease in displayed size may
include size that joined after us; the estimate is clamped to displayed size and otherwise
uncorrected.

## Old versus new queue results

The differences below are **mechanical consequences of the corrected measurement model**. No
strategy behaviour changed between the runs, and nothing here was optimised in response.

| | First run | Corrected | Why it moved |
| --- | ---: | ---: | --- |
| Slots acquired | 1,049 | 462 | Blocked and withdrawn quotes no longer acquire slots; acquisition now equals `PLACE` |
| `AT_FRONT` rate | 97,655 of 275,504 sides (35.4%) | 8,570 of 24,768 sides (**34.6%**) | Classification is now sampled, and requires a real slot |
| `PRICE_OK_BUT_DEEP` rate | 37,142 (13.5%) | 2,752 (**11.1%**) | Same |
| `NOT_QUOTING` rate | 140,707 (51.1%) | 13,446 (**54.3%**) | Sides that used to be classified from a phantom slot now correctly read NOT_QUOTING |
| `queue_ahead` p90 | 82 shares | **77 shares** | Estimates no longer aged across periods when no order existed |
| `queue_ahead` p99 | 249 shares | **367 shares** | The optimistic drift of phantom slots is gone; the tail is worse and more honest |
| `keep_ratio` | 0.99339 | **0.99568** | Different market; unaffected by the queue defect |

The p99 moving *against* us is the useful signal: the defective model let blocked quotes bank
depth decreases they never earned, which flattered the deep tail. Two different markets are
being compared, so these are indicative rather than controlled — but the direction is what the
correction predicts.

## Execution actions (exact, every cycle)

| Action | Count |
| --- | ---: |
| `KEEP` | 106,380 |
| `BLOCKED` | 97,534 |
| `NOTHING` | 30,706 |
| `PLACE` | 462 |
| `REPLACE` | 259 |
| `CANCEL` | 203 |
| `WAIT` | 0 |

**`keep_ratio` = 0.99568** — 106,380 keeps over 106,842 cycles that had a live order. The
queue-preservation property holds on real data: a resting order survives unmodified through
better than 99.5% of the cycles in which it exists.

`execution_queue_loss_actions` = **462** (`PRICE_CHANGED` 259, `UNSAFE_REPLACEMENT` 203),
reconciling exactly. This is now reported apart from `shadow_slot_losses`; the two happen to be
equal here because in shadow mode every REPLACE and CANCEL closes exactly one slot and there
were no fills. They are still separate metrics and are counted separately.

## Execution quality (sampled cycles)

| Quality | Count | Rate |
| --- | ---: | ---: |
| `NOT_QUOTING` | 13,446 | 54.3% |
| `AT_FRONT` | 8,570 | 34.6% |
| `PRICE_OK_BUT_DEEP` | 2,752 | 11.1% |
| `OFF_PRICE` | **0** | 0% |
| `STALE` | **0** | 0% |

| Reason | Count |
| --- | ---: |
| `QUOTING` | 11,322 |
| **`POST_ONLY_BLOCK`** | **10,117** |
| `CENTRE_UNAVAILABLE` | 2,106 |
| `PHASE_NOT_QUOTING` | 964 |
| `NO_LIVE_ORDER` | 259 |

**`OFF_PRICE` is zero for a structural reason, not because nothing went wrong.** In shadow mode
a venue acknowledgement is instantaneous, so an order can never rest at a stale price while a
replacement is in flight: the reconciler's REPLACE closes the slot in the same cycle it decides
to move. `OFF_PRICE` is reachable at the classifier level and is unit-tested there; producing
it in a run requires real dispatch latency, which is P13/P14. **`STALE` is zero** because feed
continuity held for the whole market (1 reconnect, handled, 0 malformed messages).

### The `POST_ONLY_BLOCK` finding stands, and is still not acted on

`BLOCKED` accounts for **97,534 of 235,544 sides (41.4%)**, and the sampled reasons say
post-only is overwhelmingly why. With zero synthetic spread the desired price frequently equals
or crosses the same-outcome ask.

Reported, not acted upon. It may mean the zero-spread reading is wrong, or that the strategy
genuinely quotes only when the book leaves room. Deciding between those is a strategy question
behind O01 and O04. **No spread was introduced.**

## Critical-path latency (ns, traced cycles)

| Stage | n | p50 | p90 | p95 | p99 | max |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `spot_receive_to_decide` | 366 | 138,795 | 354,309 | 430,388 | 488,687 | 1,032,359 |
| `clob_receive_to_decide` | 12,014 | 207,153 | 614,373 | 653,134 | 1,549,805 | 63,827,154 |
| `phase_receive_to_decide` | 4 | 166,978 | 385,640 | 385,640 | 385,640 | 385,640 |
| `fill_receive_to_decide` | **0** | — | — | — | — | — |
| `decide_duration` | 12,384 | 66,105 | 175,551 | 211,875 | 235,380 | 63,720,386 |
| `prepare_duration` | 12,384 | 17,764 | 60,311 | 66,783 | 96,387 | 235,710,283 |
| `reconcile_duration` | 12,384 | **14,882** | 38,460 | 46,216 | 52,205 | 426,039 |
| `receive_to_reconcile` | 12,384 | 240,367 | 702,349 | 767,614 | 1,722,798 | 235,764,628 |
| `keep_cycle` | 440 | 158,512 | 546,308 | 964,632 | 2,100,933 | 3,006,006 |
| `acting_cycle` | 667 | 196,037 | 638,199 | 901,475 | 2,851,627 | 5,236,884 |

`real_order_rtt`: **UNRUN — deferred to P14.** Submit-to-acknowledge cannot be measured without
sending an order. Absent, not estimated. Every value comes from `time.perf_counter_ns()` alone;
no exchange timestamp is subtracted from a local one anywhere.

## O15: before and after, on real market data

| Stage p50 | First run | Corrected | Change |
| --- | ---: | ---: | ---: |
| `reconcile_duration` | 171,659 | **14,882** | **−91.3%** |
| `receive_to_reconcile` | 323,138 | **240,367** | −25.6% |
| `prepare_duration` | 12,350 | 17,764 | +43.8% |
| `decide_duration` | 40,218 | 66,105 | +64.4% |

Reconciliation has gone from the **largest** stage on the critical path to the **smallest**.
`prepare` and `decide` rose because these are different markets on a shared machine and because
the traced sample is now 1-in-10 rather than every cycle; neither was changed by this work.
Reconcile falling by an order of magnitude is not attributable to sampling — it is the
occupancy index.

Synthetic confirmation (`tools/live_order_lookup_bench.py`), two `current()` calls per cycle:

| Retained terminal orders | before | after |
| ---: | ---: | ---: |
| 0 | 1,311 | 477 |
| 200 | 52,097 | 467 |
| 1,049 | 251,406 | 498 |
| 10,000 | 2,512,039 | 461 |

Least-squares slope: **253.5 ns per retained order before, −0.0011 after.** Flat.

## Instrumentation overhead

Method — deliberately explicit, because the previous method produced four wrong answers:
identical simulated work on both sides; identical starting state; off and on interleaved within
each repeat with the order flipped between repeats; warmed up first; 8 independent repeats each
reported; split by sampling tier; and measured on two streams. Raw data:
`p8b-instrumentation-overhead.json`.

### Steady-state stream — the production-shaped one

One order resting per side, desired price unchanged, depth churning underneath. 3,599 unsampled
/ 400 sampled / 1 acting cycle per pass, against a measured production KEEP ratio of 0.996.
Full cycle = decide + prepare + reconcile + shadow + telemetry.

| Tier | n | off p50 | on p50 | Δ p50 | Δ p95 |
| --- | ---: | ---: | ---: | ---: | ---: |
| **unsampled ordinary** | 14,396 | 32,366 | 37,268 | **+4,902 (+15.1%)** | +4,841 (+13.8%) |
| sampled ordinary | 1,600 | 32,370 | 48,789 | +16,419 (+50.7%) | +16,337 (+46.3%) |
| always-traced action | 4 | 66,630 | 84,304 | +17,674 (+26.5%) | — (n too small) |

### Replay corpus — retained, but unrepresentative

51% of its cycles act, against 0.9% in the real market, so its tier mix is not production's.

| Metric | off p50 | on p50 | Δ |
| --- | ---: | ---: | ---: |
| `decide_ns` | 25,403 | 27,371 | +1,968 (+7.7%) |
| `cycle_ns` | 40,339 | 56,172 | +15,833 (+39.2%) |
| `cycle_ns`, unsampled tier | 33,674 | 39,798 | +6,124 (+18.2%) |

Per-repeat cycle p50 (off / on), showing the two configurations were measured under comparable
conditions: 40338/56390, 39670/55478, 39770/55440, 39813/55436, 39773/55290, 41776/60772,
42132/55708, 40232/56153.

## P8 PERFORMANCE GATE: NOT PASSED

| Limit | Target | Measured | Verdict |
| --- | --- | ---: | --- |
| Unsampled full-cycle p50 overhead | ≤ 5 µs | 4,902 ns | **MET** |
| Unsampled full-cycle p50 overhead | ≤ 5 % | **15.1 %** | **NOT MET** |
| `decide` p50 overhead | ≤ 1 µs | 1,968 ns | **NOT MET** |
| `decide` p50 overhead | ≤ 3 % | 7.7 % | **NOT MET** |

Recorded as failed rather than restated as a pass. Nothing was weakened to improve the numbers.

The `decide` figures need one honest caveat: `observe()` runs *after* `decide()` in the loop, so
instrumentation cannot slow the decision it has already finished. The +1,968 ns is second-order
— allocator and cache pressure from the traced cycles that surround it — not work added inside
`decide()`. It is still a real cost and is reported as one.

### The irreducible part, measured

`tools/untraced_path_cost.py`, best-of-rounds nanoseconds, for work an **unsampled** cycle still
performs:

| Component | ns |
| --- | ---: |
| Two-side state loop (slot depth + action counters) | 1,570 |
| Three `perf_counter_ns` reads | 249 |
| Sampling decision | 99 |
| Continuity health check | 50 |
| — of which `bid_size_at` ×2 | 423 |
| — of which `count_action` ×2 | 209 |

The state loop is the residue and **cannot be sampled away**. A depth decrease skipped because
its event was not sampled is a decrease the estimate never learns about, which would make
`queue_ahead` depend on the sampling rate — the property `test_the_estimate_does_not_depend_on_the_sampling_rate`
exists to prevent. Meeting a 5% relative limit would require either abandoning that guarantee
or leaving CPython, and neither is a trade this phase is entitled to make silently.

What *was* removed: a six-field frozen `QueueEstimate` built on every `on_place` and `on_keep`
and discarded by untraced callers (~1 µs per side, now guarded by an allocation test); a
side-string validation in the depth lookup; two property calls for `acting`; and the trace
reset, stage writes, and snapshot on every unsampled cycle.

## Reproducing

```bash
.venv/bin/python tools/measure_market.py <output-directory>   # waits for the next T0, ~9 min
.venv/bin/python tools/instrumentation_overhead.py            # paired, interleaved, tiered
.venv/bin/python tools/live_order_lookup_bench.py             # O15 before/after
.venv/bin/python tools/untraced_path_cost.py                  # unsampled-path breakdown
```

All read-only. `LIVE_TRADING_ENABLED` is `False` and no code path in any of these tools can
reach a venue write endpoint.
