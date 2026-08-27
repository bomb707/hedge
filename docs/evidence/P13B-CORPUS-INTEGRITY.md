# P13B — corpus integrity corrections

**Provenance: `REAL_PUBLIC_MARKET_DATA`** for every market referenced. **No order. No credential.
No chain write.** `LIVE_TRADING_ENABLED` and `REDEMPTION_ENABLED` are both `False` throughout.

**Date:** 2026-08-27 (UTC). Corrects four collection defects found in the independently reviewed
P13 implementation/pilot boundary. The ≥200-market acceptance corpus is a separate gate.

## The first collector was stopped, and nothing was deleted

`p13-corpus-1` was stopped by `SIGTERM` to PID 3397985, after verifying that the PID was still
this root's runner rather than a reused number. It had attempted 14 markets and written **12
durable rows, all COMPLETE and all replaying EXACT**; two markets were in flight and have no row.

Everything is preserved at `/home/hr/p13-corpus/` — index, journals, stores, archives and log —
and `EPOCH_STATUS.md` beside it records the epoch as **`SUPERSEDED_FOR_FINAL_P13_ACCEPTANCE`**
with its reasons. The inventory, with per-market hashes, is
`docs/evidence/p13-corpus-1-superseded.json`.

It is real data and remains useful for exploratory engineering, replay work and store-durability
evidence. It cannot count toward the acceptance gate, for three reasons.

## Defect A — L3 was not classifying every decision

`TelemetryAnalyzer.process` returns early for a cycle that neither acted nor was latency-sampled.
P13 therefore classified **every acting cycle plus one in ten of the rest** and reported the
result as a market fraction.

```text
btc-updown-5m-1787826000   143,740 decisions   287,480 side opportunities   30,734 classified
btc-updown-5m-1787826300   154,465 decisions   308,930 side opportunities   ~38,000 classified
```

The bias is not random. Acting cycles are exactly the ones where an order was being placed or
replaced, which is when the queue position is worst — so the sampled denominator over-weights the
worst moments and any `AT_FRONT` rate taken over it means nothing in particular.

**One classifier still.** `maker5m.telemetry.classifier.classify` is untouched and remains the
only implementation. The analyzer gained an explicit mode:

| mode | what it classifies |
|---|---|
| `SAMPLED_OR_ACTING` | P8's accepted behaviour. **Still the default.** |
| `EVERY_DECISION` | both sides of every decision observation. P13's. |

A cycle is classified exactly once in either mode. The shadow queue state advances once per
observation regardless, before either branch, because it models where our order sits and cannot
be allowed to depend on who is counting.

**Latency sampling is untouched.** Reading a clock is the expensive part and P8's policy is
accepted; nothing was added to Plane 1. The same deterministic stream, at three sampler settings:

```text
SAMPLED_OR_ACTING    400 / 40 / 4 classifications
EVERY_DECISION       400 / 400 / 400
stage timings        200 / 20 / 2      (unchanged in both modes)
```

**The denominator is exact.** Every decision observation carries two side opportunities, UP and
DOWN, so with no own fills — and there are none, because no order is ever sent —
`classified == 2 × decisions_written`. Each entry records `expected_classifications`,
`actual_classifications` and `classification_complete`, and a market where they disagree is
retained and not counted. If own fills ever enter this stream the formula changes and must be
restated rather than assumed.

**Actions get the same treatment.** `sum(action_counts) == 2 × decisions` too. The corpus report
had been reading actions from `worker.summary()["actions"]`, which does not exist — `WorkerStats`
counts rows, not reconciler decisions — so every action total it printed was silently zero. They
now come from the analyzer's own per-side counters, which P8 already increments exhaustively.

## Defect B — "prearm ready" was discovery ready

`prearm.ready_ns` recorded when `discover_market` returned. The CLOB and Binance producers do not
start until T0-30, so the reported **74.9 s lead proved that metadata had been resolved** and said
nothing about whether a book or a BTC price existed before the market's first event.

P6 now records three warm milestones — first CLOB message, book ready, first valid spot — purely
observationally, first-write-wins, with no effect on event ordering, health semantics,
warm-message application, the T0 boundary, strategy, risk or execution. Every P6 regression is
green.

```text
discovery_started_ns / discovery_ready_ns     metadata
clob_first_ns / clob_book_ready_ns            a usable book
spot_first_valid_ns                           a real BTC price
feed_ready_ns = max(book_ready, spot_first)   only when both exist
```

A market is warm when it has **both**. A book with no spot cannot price a centre and a spot with
no book has nothing to quote against, so `feed_ready_ns` is `None` when either is missing — never
a number standing in for a guess — and `feed_ready_before_t0` false makes the market ineligible.

The old pilot rows are **not edited**. Under that schema `prearm.ready_ns` is
**`DISCOVERY_READY_NS`**, and the actual warm-feed timestamps are unrecoverable from them. That is
precisely why they cannot prove the prearm gate.

## Defect C — the cold backlog was not bounded

`MAX_COLD_BACKLOG = 3` was defined, `_bound_cold` was written, and the launch loop called neither.
It logged `cold backlog is N markets` and launched anyway. A settlement watch can run four hundred
seconds before verification, replay and compression even begin, so a slow chain accumulates tasks
and 650 MB raw stores with nothing to stop it.

The loop now waits for capacity **before a market exists** — never during one, so a running
session is never made to wait on a closed market's settlement — and if capacity has not appeared
by the last moment the market could still be launched, the slot is skipped and a
`COLD_BACKLOG_CAP` row is appended. A missing five-minute window that says why beats a queue
nobody is watching. `cold_backlog`, `cold_backlog_high_water` and the cap are on every entry.

## Defect D — a market could count before its row was durable

`_finalize` logged an append failure and then incremented the completion total anyway. A full disk
would have produced a "two-hundred-market corpus" with fewer than two hundred rows in it.

Completion now requires the append to have succeeded, and the target is a property of the corpus
rather than of this process's uptime: at startup the collector counts the durable qualifying rows
for its **epoch, config hash and source revision**, and collects the remainder. Restarting after
150 collects 50 more. All three identities are required — another epoch is another collection,
another config hash another experiment, another revision another build.

Two more integrity rules came with it:

* **A torn tail no longer eats the next entry.** A kill mid-append leaves a fragment with no
  newline; appending onto it would weld the next row to the wreck of the last and lose both. The
  fragment is closed off with a newline and fsynced first, and left exactly where it is — it is
  the evidence that a process died there.
* **The corpus knows which software made it.** `git rev-parse HEAD` names a commit and says
  nothing about whether the imported files match it, so the identity records the tree hash and
  whether any *tracked* file is modified. The runner refuses to start an epoch with a dirty tree
  unless `--allow-dirty` is passed for an exploratory run. Untracked files are ignored: evidence
  directories are not the software.

The feed floors `PaperConfig` had defined and nobody applied are now enforced, and the collection
knobs that change behaviour — settle timeout, poll interval, raw-store retention, classification
mode — are part of the config hash. Paths are not: where evidence is written is not what the
experiment is.

## What the first corrected pilot found

Three real markets under `p13b-pilot-1`, all COMPLETE, all replaying EXACT, all exhaustively
classified, all warm before T0. And one thing nobody had asked it to check: a single `observe`
cycle of **480 ms** against a 25 µs median, on the markets that had a predecessor being released
underneath them, with a 2,535-observation buffer high-water on the market that was live at the
time.

Freeing 150,000 recorded steps is one C-level traversal with no bytecode boundary in it, so the
event loop could not service the *currently trading* market until it finished. A thread would not
have helped: deallocation holds the GIL throughout. It is now done in chunks of 4,096 with a yield
between them, which bounds the pause rather than the work.

Nothing may stall the ingress owner. P12B and P12C each spent a round on that rule, and it applies
to the cleanup path exactly as it applies to a `listdir` or a `print`. `p13b-pilot-1` is retained
as the evidence that found it, and the acceptance pilot is the run after the fix.

## Two more defects the corrected pilots found

Neither was on the review's list. Both were found by the instrumentation added for it, which is
what instrumentation is for.

**The launch loop held every market it had launched.** After four markets the process held four
live sessions. `release()` had cleared each one's insides faithfully; the loop's own bookkeeping
was keeping the objects alive in a `dict[str, MarketSession]` that only ever needed the slugs.
"Released" was false however thoroughly the insides had been emptied.

**Full garbage collections were stalling the ingress owner.** A full collection is proportional to
the number of *tracked* objects, and this process holds a market's recorded event stream — about
750,000 tracked objects per market, several million with closed markets still resident. Measured
over twenty minutes of real markets:

```text
gen 0    24,000 collections   max 12 ms      total 3.8 s
gen 1     2,200 collections   max 17.7 ms    total 2.4 s
gen 2        37 collections   max 1,460 ms   total 17.1 s
```

That traversal runs inside whichever allocation triggers it, and on this process that includes the
ingress owner's own cycle: single `observe` calls of 277, 541, 588, 594, 674 and 762 ms appeared
against a 25 µs median.

Full collections over that graph find almost nothing — `ReplayStep` holds an event and a decision,
which hold tuples, integers and strings; it is acyclic and reference counting frees all of it. So
full collections were made **forty times rarer, not disabled**: an asyncio process does produce
cycles elsewhere and something has to collect them. That in turn exposed a session-level cycle —
the session held its control ingress, the ingress held a lambda, the lambda captured the session —
which is now broken so a closed market is freed by reference counting alone.

After both: `observe` maxima of 15.6, 25.8 and 26.0 ms across the last three markets of the
acceptance pilot, and 426 ms on the first, where one full collection still landed on an ingress
cycle. Zero drops throughout, and the residual is recorded per market rather than described as
solved.

## The acceptance pilot — `p13b-pilot-5`

Four consecutive real markets, one process, no restart, on a clean tree at `66da7ca`.

| | 1787839200 | 1787839500 | 1787839800 | 1787840100 |
|---|---|---|---|---|
| verification | COMPLETE | COMPLETE | COMPLETE | COMPLETE |
| replay | EXACT | EXACT | EXACT | EXACT |
| decisions | 235,924 | 219,310 | 199,752 | 159,786 |
| classified / expected | 471,848 / 471,848 | 438,620 / 438,620 | 399,504 / 399,504 | 319,572 / 319,572 |
| side actions | 471,848 | 438,620 | 399,504 | 319,572 |
| CLOB / BTC messages | 226,849 / 12,551 | 210,181 / 13,311 | 191,751 / 11,350 | 153,854 / 8,938 |
| book ready before T0 | 29.83 s | 29.81 s | 29.70 s | 29.75 s |
| spot ready before T0 | 29.13 s | 29.12 s | 28.90 s | 29.02 s |
| **feed ready before T0** | **29.13 s** | **29.12 s** | **28.90 s** | **29.02 s** |
| drops / gaps / sink errors | 0 / 0 / 0 | 0 / 0 / 0 | 0 / 0 / 0 | 0 / 0 / 0 |
| PLACE by risk state | SAFE 886 | SAFE 810 | SAFE 597 | SAFE 440 |
| settlement | RESOLVED UP | RESOLVED DOWN | RESOLVED DOWN | RESOLVED UP |

Discovery readiness was 74.9 s on every handoff and is reported separately, because it is a
different fact: metadata resolved, feeds not yet open.

**PLACE while HALTED: 0. PLACE while RECOVERING: 0**, across 221 HALTED and 5 RECOVERING risk
records. Cold backlog high-water **1** against a cap of 3. Corpus append failures **0**.

### The exhaustive L3 distribution

Over 1,629,544 side opportunities — every side of every decision:

```text
AT_FRONT            563,602   34.59 %
PRICE_OK_BUT_DEEP   224,192   13.76 %
NOT_QUOTING         841,724   51.65 %
OFF_PRICE                 0    0.00 %
STALE                    26    0.00 %
```

**Do not compare these against the superseded fractions.** They do not share a denominator: the
old numbers were taken over acting cycles plus a tenth of the rest, and this one is taken over the
market.

Reconciler actions over the same denominator: KEEP 784,015, BLOCKED 723,964, NOTHING 114,007,
PLACE 3,779, REPLACE 2,007, CANCEL 1,772 — 1,629,544 in total, matching exactly.

### Resources, measured after release

```text
market   start RSS   trading-end RSS   post-release RSS   live sessions   threads   fds
1           35 MB          602 MB           1,477 MB            3            8      24
2          533 MB        1,497 MB           1,503 MB            3            8      24
3        1,468 MB        1,519 MB           2,091 MB            2            7      21
4        1,503 MB        2,091 MB           2,056 MB            1            6      16
```

`live_sessions` falls to one as the run drains, which is the number that answers the question:
sessions are being released. Three is the architectural steady state — the market being finalized,
the one trading, and the one warming — not a leak. RSS plateaus around 1.5-2.1 GB and does not
grow by a market's graph per market; it is the operating system's view, and glibc does not hand
freed arenas back promptly, which is precisely why the object count is reported beside it.

### Cost to the trading path

The accepted process-isolated benchmark, after the pilot (`p13b-overhead.json`):

| P8C limit | P13B | |
|---|---|---|
| decide p50 overhead ≤ 1,000 ns | −376 ns | MET |
| decide p50 overhead ≤ 3 % | −1.48 % | MET |
| full-cycle p50 overhead ≤ 5,000 ns | +736 ns | MET |
| full-cycle p50 overhead ≤ 5 % | +1.32 % | MET |

No limit was moved. Exhaustive classification runs on the persistence worker's thread; observation
buffer high-water across the pilot was 247 to 11,759 of 320,000, and **no market dropped an
observation**.

## Status of the earlier artifacts

* [`P13-PILOT.md`](P13-PILOT.md) and its artifacts are **retained unedited**. The composition,
  market-overlap, settlement-overlap, store-durability and replay evidence in it remain valid.
  Its L3 distribution is superseded — non-exhaustive — and its 74.9 s figure is discovery
  readiness. **P13 pilot v1 is SUPERSEDED FOR THE FINAL GATE.**
* `p13-corpus-1` is preserved, marked superseded, and excluded from the acceptance count.
* The corrected acceptance corpus runs under a new epoch in its own root, and the two are never
  pooled.
