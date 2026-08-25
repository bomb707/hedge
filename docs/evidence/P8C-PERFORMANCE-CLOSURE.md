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

