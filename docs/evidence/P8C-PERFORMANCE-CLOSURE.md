# P8C performance closure

The final P8 boundary. [`P8-MEASUREMENT.md`](P8-MEASUREMENT.md) is superseded on queue metrics;
[`P8B-MEASUREMENT.md`](P8B-MEASUREMENT.md) remains the authoritative **correctness** evidence and
is not superseded. This document closes the **performance** gate that P8B reported as failed.

Branch `fix/p8-telemetry-offload`, from `c5cec7f`.

No strategy parameter was changed. No spread was introduced. `strategy/`, `accounting/`,
`market/`, `numeric/`, the reconciler, and the corrected `LiveOrderTable` occupancy index are
untouched by this round.

## What moved, and what deliberately did not

P8B's measurement was correct and synchronous. Every cycle mutated shadow queue slots, counted
actions, classified both sides, and updated distributions before the trading loop could
continue — +4,902 ns on an ordinary unsampled book update, for work no trading decision depends
on.

| Stays on the trading path | Moved downstream |
| --- | --- |
| `prepare_both_sides`, `reconcile` | shadow queue slot transitions |
| shadow `LiveOrderTable` lifecycle | execution-quality classification |
| displayed depth at our own price | action, KEEP, and queue-loss counters |
| stage timestamps, **sampled cycles only** | every latency distribution |
| one tuple built, one `deque` append | `queue_ahead` accumulation |

The split is drawn at *analysis*, not at *simulation*. Preparation, reconciliation, and the
shadow order table model what production does every cycle; moving them to the analyzer would
have lowered the telemetry number by charging production work to the wrong side, which is the
error that produced P8's earlier +133% and +217% figures. They run in **both** benchmark arms.

Depth is the other thing that cannot move. The book is mutable and moves continuously, so the
size resting at our own price has to be sampled at the moment the cycle sees it. There is no
later time at which the analyzer could recover it.

## Observation representation — measured, not assumed

300,000 iterations, best of 7 rounds:

| Representation | ns to build |
| --- | ---: |
| **tuple of references (16 fields)** | **76** |
| tuple of fully extracted primitives (26) | 176 |
| slots dataclass | 305 |
| NamedTuple | 362 |
| frozen slots dataclass | 1,791 |

| Buffer insertion | ns |
| --- | ---: |
| **`deque(maxlen).append`** | **46** |
| hand-rolled preallocated ring | 95 |

The frozen dataclass — the obvious "clean" choice — costs 24× a tuple. That is the same lesson
P8 already learned when a discarded `QueueEstimate` was found costing ~1 µs per side, and it is
why this was benchmarked rather than reasoned about.

The observation carries *references* to objects the cycle already built: the reconcile plan and
the eligibility record. It copies nothing that would retain the world — no `MarketState`, no
`DecisionResult`, no book, no `LiveOrderTable`.

## Buffer, drops, and memory

`deque(maxlen=160_000)`. Capture never blocks: on overflow the oldest observation is dropped and
the trading path continues immediately. The drop count is *derived* (`accepted - drained -
len`), so learning it costs the hot path nothing — a length check per append would put work back
where it was just removed.

Capacity is set from evidence. The busiest market measured produced 117,772 cycles, and one
observation retains **638 bytes** including the plan it references, so the bound is ≈97 MiB with
36% headroom.

| Retention measured over 120,000 observations | bytes/observation | total |
| --- | ---: | ---: |
| tuple holding the plan by reference | 638 | 73.0 MiB |
| tuple with the plan's fields extracted | 315 | 36.0 MiB |

Extracting the fields halves memory for about +100 ns of capture. That is a real trade and it is
recorded rather than taken: P8 analyses one bounded five-minute market, and P11 owns continuous
draining, where the trade will matter. Retained graphs are also what the collector walks —
disabling GC in the steady-state benchmark cut the instrumented p99 tail roughly in half — so
the option is left explicit for whoever needs it.

### Drop semantics

A drop means an unseen depth change at our own price, so the estimate cannot be continued across
it. The analyzer detects the sequence gap independently of the buffer's own counter, marks queue
confidence `STALE`, and **does not bridge**. Trading is unaffected — the loss happened in
observation, not execution — but the measurement says so. A P8 acceptance market must show zero
drops, and this one does.

Out-of-order observations **fail closed** with `TelemetryOrderError` rather than being sorted
into shape: a stream whose order is unknown has unknown provenance, and repairing it silently
would manufacture confidence.

## Semantic equivalence — proven, not asserted

`tools/queue_semantics_snapshot.py` was run against the **previous synchronous implementation**
in a git worktree at `c5cec7f`, and against the offloaded pipeline, over 468 cycles of the same
deterministic corpus at `sample_every = 10`.

```text
shadow slots acquired / kept / lost      120 / 132 / 120         IDENTICAL
loss reasons  PRICE_CHANGE 60, SIZE_CHANGE 36, DESIRED_WITHDRAWN 24   IDENTICAL
actions  BLOCKED 288, NOTHING 276, KEEP 132, PLACE 120, REPLACE 96, CANCEL 24  IDENTICAL
quality  AT_FRONT 220, NOT_QUOTING 544                           IDENTICAL
reasons  POST_ONLY_BLOCK 246, QUOTING 220, PHASE_NOT_QUOTING 104,
         ENDGAME_GATE 98, NO_LIVE_ORDER 96                       IDENTICAL
queue_ahead sequence, all 220 elements in order                  IDENTICAL
```

That snapshot is frozen at `tests/telemetry/golden/synchronous_queue_semantics.json` and
asserted element by element, so a future change that alters what P8 measures cannot pass
unnoticed.

## Hot-path capture cost

`tools/untraced_path_cost.py`, best-of-rounds nanoseconds, for what an **unsampled** cycle
performs:

| Component | ns |
| --- | ---: |
| Observation build + buffer insert | 178.6 |
| Same-price depth reads ×2 | 117.2 |
| Continuity health check | 51.6 |
| **Unsampled total** | **347.4** |
| Sampling decision (now before reduce/decide) | 98.4 |
| Three perf-counter reads — **sampled cycles only** | 251.1 |

P8B's equivalent figure was ~1,970 ns, of which 1,570 ns was the two-side analytical state loop
that no longer exists on this path.

## Full-cycle overhead — paired, interleaved, tier-split

Identical simulated work both sides; identical starting state; off and on alternating within
each repeat with the order flipped between repeats; warmed up first; every repeat reported; two
streams. Raw data: `p8c-overhead.json`.

### Steady-state stream — the production-shaped one

One order resting per side, desired price unchanged, depth churning underneath. 3,599 unsampled
/ 400 sampled / 1 acting cycle per pass, against a measured production KEEP ratio of 0.996.

| Tier | n | off p50 | on p50 | Δ p50 | Δ p95 | Δ p99 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| **unsampled ordinary** | 14,396 | 32,874 | 33,829 | **+955 (+2.9%)** | +1,748 (+5.0%) | +14,045 (+23.0%) |
| sampled ordinary | 1,600 | 32,887 | 34,247 | +1,360 (+4.1%) | +2,783 (+8.1%) | +19,600 (+35.6%) |
| always-traced action | 4 | 70,947 | 65,029 | −5,918 (−8.3%) | — | — |

The always-traced row has **n = 4** per configuration. Its quantiles are noise and the negative
delta is meaningless; it is shown for completeness, not as a result.

The p99 rows are dominated by garbage collection, and that is stated rather than smoothed away.
Re-running the steady-state comparison with GC disabled moved the unsampled p99 delta from
+11,711 ns to +5,236 ns, so roughly half of the instrumented tail is collector work over the
retained observation graphs. The p50 delta was unaffected.

### Replay corpus — retained, but unrepresentative

51% of its cycles act, against 0.9% in the real market.

| Tier | n | off p50 | on p50 | Δ |
| --- | ---: | ---: | ---: | ---: |
| unsampled ordinary | 2,304 | 34,590 | 35,574 | +984 (+2.8%) |
| sampled ordinary | 3,776 | 39,881 | 40,722 | +841 (+2.1%) |
| always-traced action | 6,400 | 45,746 | 47,024 | +1,278 (+2.8%) |
| overall `cycle_ns` | 12,480 | 41,513 | 42,568 | +1,055 (+2.5%) |
| overall `decide_ns` | 12,480 | 26,098 | 26,267 | +169 (+0.6%) |

Per-repeat full-cycle p50 (off / on): 41483/42263, 40606/42164, 40704/41892, 43863/41903,
42388/44353, 41489/42729, 41547/42627, 41563/42459.

## Process-isolated decide overhead — the authoritative measurement

`observe()` runs after `decide()`, so instrumentation cannot slow a decision that has already
finished. P8B nonetheless reported decide p50 +7.7% in-process, because allocator state, GC
scheduling, and cache residency carry from measured cycles into the next cycle's decision. No
amount of interleaving inside one interpreter removes that, so each configuration now gets a
fresh interpreter that does nothing else.

Twelve pairs, alternating launch order, 60 passes each, 3 warmup passes per process, Python
3.12.3, same corpus and config. Raw data: `p8c-decide-isolated.json`.

| Pair | Order | off p50 | on p50 | Δ |
| ---: | --- | ---: | ---: | ---: |
| 0 | off first | 25,936 | 29,135 | +3,199 (+12.33%) |
| 1 | on first | 26,733 | 27,175 | +442 (+1.65%) |
| 2 | off first | 26,634 | 26,255 | −379 (−1.42%) |
| 3 | on first | 26,964 | 26,723 | −241 (−0.89%) |
| 4 | off first | 26,624 | 26,456 | −168 (−0.63%) |
| 5 | on first | 26,326 | 27,148 | +822 (+3.12%) |
| 6 | off first | 26,040 | 25,996 | −44 (−0.17%) |
| 7 | on first | 26,121 | 25,890 | −231 (−0.88%) |
| 8 | off first | 26,178 | 27,292 | +1,114 (+4.26%) |
| 9 | on first | 26,458 | 26,689 | +231 (+0.87%) |
| 10 | off first | 26,045 | 26,535 | +490 (+1.88%) |
| 11 | on first | 25,905 | 27,042 | +1,137 (+4.39%) |

| Estimator | Δ | Δ % |
| --- | ---: | ---: |
| Median of per-configuration p50s | **+454 ns** | **+1.73%** |
| Median of per-pair deltas | +336 ns | +1.26% |

Both are reported rather than whichever flatters. Five of twelve pairs are negative and the
spread runs −1.42% to +12.33%, so the effect sits close to this machine's noise floor; the
outlying first pair is the coldest process of the run.

## P8 PERFORMANCE GATE: PASSED

| Limit | Target | P8B | P8C | Verdict |
| --- | --- | ---: | ---: | --- |
| Unsampled full-cycle p50 overhead | ≤ 5,000 ns | 4,902 | **955** | **MET** |
| Unsampled full-cycle p50 overhead | ≤ 5 % | 15.1 % | **2.9 %** | **MET** |
| Decide p50 overhead (process-isolated) | ≤ 1,000 ns | 1,968 † | **454** | **MET** |
| Decide p50 overhead (process-isolated) | ≤ 3 % | 7.7 % † | **1.73 %** | **MET** |

† P8B's decide figures were same-process and therefore contaminated; they are not directly
comparable to P8C's isolated measurement and are shown only for continuity.

Sampled cycles (+1,360 ns, +4.1%) and always-traced cycles legitimately cost more, and are
reported separately rather than averaged in. No limit was moved.

## Full real market — `btc-updown-5m-1787663400`

| Field | Value |
| --- | --- |
| Market id | `0x1f7c9d8819f6695e6ab3dbdfffe017c75df9da38e600041d1613fcba12f0d630` |
| Run (UTC) | 2026-08-25T13:15:14Z |
| `live_trading_enabled` | **false** |
| Orders sent to any venue | **0** |
| Credentials used | none — no key, no signing, no authenticated socket, no write endpoint |
| Cycles | 204,440 |
| Journal steps | 210,570 |
| Feed | 201,200 CLOB + 7,428 spot messages; 0 malformed, 0 unhandled, 0 reconnects |
| Raw data | `p8c-measurement-btc-updown-5m-1787663400.json` |

### Observation stream

| Metric | Value |
| --- | ---: |
| Captured | 204,440 |
| Buffer capacity | 320,000 |
| **Dropped** | **0** |
| Gaps seen downstream | **0** |
| Observations lost downstream | **0** |
| Cycles with stage timing | 20,440 (**10.0%**, sampling is 1-in-10) |
| Retained memory | ≈124 MiB at 638 bytes per observation |

Zero drops and zero gaps, which is the acceptance requirement: the analysis covers the entire
market with no interval whose queue continuity had to be abandoned.

The 10.0% is worth stating plainly, because the *first* P8C market run got this wrong. See
"A defective run, retained" below.

### Execution actions (exact, every cycle)

| Action | Count |
| --- | ---: |
| `KEEP` | 201,049 |
| `BLOCKED` | 173,215 |
| `NOTHING` | 31,616 |
| `PLACE` | 1,500 |
| `REPLACE` | 902 |
| `CANCEL` | 598 |
| `WAIT` | 0 |

**`keep_ratio` = 0.99259** — 201,049 keeps over 202,549 cycles that had a live order.

### Queue (`SHADOW_ESTIMATE`)

| Metric | Value |
| --- | ---: |
| Shadow slots acquired | **1,500** |
| Shadow slot-cycles kept | 201,049 |
| Shadow slot losses | **1,500** |
| — `PRICE_CHANGE` | 902 |
| — `UNSAFE_REPLACEMENT` | 591 |
| — `DESIRED_WITHDRAWN` | 7 |
| Slots still open at end | 0 |
| `execution_queue_loss_actions` | 1,500 (`PRICE_CHANGED` 902, `UNSAFE_REPLACEMENT` 591, `DESIRED_WITHDRAWN` 7) |

Slots acquired equals `PLACE` actions exactly (1,500), which is the defining property of the
corrected lifecycle: a slot exists only where an order was dispatched. Both loss totals
reconcile exactly to their typed reasons, and the two metrics remain reported apart.

| Queue metric | n | p50 | p90 | p95 | p99 | max |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `queue_ahead` (share units) | 22,108 | 0 | 87,710,000 | 166,250,000 | 433,580,000 | 1,189,590,000 |

In shares: p50 **0**, p90 **88**, p95 **166**, p99 **434**, max **1,190**.

### Execution quality (sampled cycles)

| Quality | Count | Rate |
| --- | ---: | ---: |
| `NOT_QUOTING` | 22,850 | 50.9% |
| `AT_FRONT` | 16,540 | 36.8% |
| `PRICE_OK_BUT_DEEP` | 5,568 | 12.4% |
| `OFF_PRICE` | **0** | 0% |
| `STALE` | **0** | 0% |

| Reason | Count |
| --- | ---: |
| `QUOTING` | 22,108 |
| **`POST_ONLY_BLOCK`** | **18,291** |
| `NO_LIVE_ORDER` | 902 |
| `PHASE_NOT_QUOTING` | 926 |
| `ENDGAME_GATE` | 2,535 |
| `CENTRE_UNAVAILABLE` | 196 |

`OFF_PRICE` is zero for the structural reason recorded in P8B: shadow acknowledgement is
instantaneous, so no order can rest at a stale price while a replacement is in flight. It is
reachable and unit-tested at the classifier, and producing it in a run needs real dispatch
latency (P13/P14). `STALE` is zero because continuity held for the whole market — 0 reconnects,
0 malformed messages, 0 telemetry gaps.

**The `POST_ONLY_BLOCK` finding stands and is still not acted on.** `BLOCKED` covers
**173,215 of 408,880 sides (42.4%)**, consistent with P8B's 41.4%. No spread was introduced.

### Critical-path latency (ns, sampled cycles)

| Stage | n | p50 | p90 | p95 | p99 | max |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `spot_receive_to_decide` | 749 | 85,991 | 270,015 | 329,812 | 463,995 | 846,859 |
| `clob_receive_to_decide` | 19,687 | 153,319 | 501,558 | 614,399 | 1,182,158 | 193,168,078 |
| `phase_receive_to_decide` | 4 | 98,708 | 315,296 | 315,296 | 315,296 | 315,296 |
| `fill_receive_to_decide` | **0** | — | — | — | — | — |
| `decide_duration` | 20,440 | 47,827 | 151,531 | 173,795 | 241,600 | 193,105,703 |
| `prepare_duration` | 20,440 | 15,585 | 50,649 | 59,944 | 89,756 | 2,069,076 |
| `reconcile_duration` | 20,440 | **11,154** | 34,859 | 38,195 | 50,298 | 167,910 |
| `receive_to_reconcile` | 20,440 | 178,857 | 584,808 | 698,643 | 1,218,561 | 193,205,335 |
| `keep_cycle` | 1,227 | 150,327 | 526,234 | 740,071 | 1,760,416 | 3,793,918 |
| `acting_cycle` | 241 | 172,714 | 578,872 | 732,683 | 1,077,926 | 2,139,321 |

`real_order_rtt`: **UNRUN — deferred to P14.** Absent, not estimated. All values come from
`time.perf_counter_ns()` alone; no exchange timestamp is subtracted from a local one.

### O15 holds across three markets

| `reconcile_duration` p50 | P8 (pre-fix) | P8B | P8C |
| --- | ---: | ---: | ---: |
| ns | 171,659 | 14,882 | **11,154** |

Reconciliation remains the smallest stage on the critical path, below `decide` (47,827) and
`prepare` (15,585). The occupancy index was not touched in this round and its regression suite
runs unchanged; the standalone benchmark still reports a slope of **0.0 ns per retained order**
against 260.9 for the original scan.

## A defective run, retained

The first P8C market run — `btc-updown-5m-1787662800`, 153,762 cycles, retained as
`p8c-measurement-SUPERSEDED-btc-updown-5m-1787662800.json` — produced stage timings for **126
cycles instead of ~15,400**. Nothing raised and nothing failed; the latency evidence was simply
almost empty, and the number was small enough to be easy to skim past.

The cause was making the same decision twice. The hot path asked the sampler with
`meta.ingress_ordinal`; the analyzer asked again with the observation's ordinal. `next_meta`
assigns an ordinal and *then* increments, so those differ by one, and at `sample_every = 10`
the two answers could essentially never agree. Only cycles that were also acting — and
therefore traced regardless — survived. That is exactly the 126.

Whether a cycle was sampled is now a captured fact, read from the observation rather than
re-derived, so there is one source of truth and no second opinion. The regression test asserts
the arithmetic that would have caught it: on a KEEP-dominated corpus, cycles with stage timing
must land within 50% of `cycles / sample_every`. Restoring the off-by-one fails it and four
others.

That run's queue, action, and quality results were unaffected — none of them depend on stage
timing — and they agree with the corrected run. It is retained anyway.

## Buffer sizing, resized twice

| Market | Cycles | Capacity at the time | Fill |
| --- | ---: | ---: | ---: |
| `…1787652900` | 137,752 | — | — |
| `…1787658900` | 117,772 | — | — |
| `…1787662800` | 153,762 | 160,000 | 96% |
| `…1787663400` | 204,440 | 220,000 | 93% |

Each bound set from the busiest market *so far* was nearly filled by the next. The capacity is
now **320,000**, set from the busiest observed plus a little over half again (≈195 MiB at the
bound, ≈124 MiB for the market above). Markets vary by nearly 2× in cycle count, and a bound
sized to the last one is a bound about to be discovered the hard way.

Overflow is not silent: the oldest observations are dropped, the count is visible, and the
analyzer independently sees the sequence gap and marks queue confidence `STALE`.

## Reproducing

```bash
.venv/bin/python tools/measure_market.py <output-directory>   # waits for the next T0, ~10 min
.venv/bin/python tools/instrumentation_overhead.py            # paired, interleaved, tiered
.venv/bin/python tools/decide_overhead_isolated.py            # process-isolated decide
.venv/bin/python tools/untraced_path_cost.py                  # unsampled capture breakdown
.venv/bin/python tools/live_order_lookup_bench.py             # O15 before/after
.venv/bin/python tools/queue_semantics_snapshot.py            # the equivalence snapshot
```

All read-only. `LIVE_TRADING_ENABLED` is `False` and no code path in any of these tools can
reach a venue write endpoint.

## Open items

**O08 remains OPEN. O09 remains OPEN.** The latency distribution is measurable and the queue
model is correct, but neither question can be settled without real resting orders, which P8
does not place. **O15 remains CLOSED.** No strategy open item was created for the telemetry
architecture: it is an engineering concern, not a question about the reconstructed strategy.
