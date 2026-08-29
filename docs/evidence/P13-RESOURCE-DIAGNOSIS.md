# P13 — resident memory and collector pauses: what the measurements say

**Branch** `fix/p13-runtime-resource-stability`, from the accepted acceptance-evidence commit
`2eb7d9afd7d36b798eb67111b007739c6bae391d`.

**This document changes nothing about `p13-corpus-6`.** Its 202 markets, its journals, its stores
and its empirical gate stand exactly as accepted. What is under investigation is the *process*
that collected them, which ended at 4.26 GB of resident memory and could not say why.

---

## 0. The two questions, kept apart

| | question | what it blocks |
|---|---|---|
| **A** | why does parent RSS keep rising after market sessions are released? | **P13's resource gate** |
| **B** | why do generation-2 collections cost hundreds of ms, and can they be attributed? | **P14 readiness risk** |

They are not the same question and are not answered together. A fix that stabilises memory does
not thereby fix the collector's pauses, and this document does not claim it did unless the
measurements say so.

## 1. The failure baseline (from the accepted corpus, unchanged)

Post-release RSS 36.2 MB → 4,261.7 MB across 202 markets in one process, 16 h 58 m.

* all-run slope **+10.26 MB/market** (122.1 MB/h)
* first 50 **+32.23**, middle 100 **+4.66**, last 50 **+31.46 MB/market** (374.6 MB/h)
* quartile medians 2,350.9 → 2,463.9 → 2,780.3 → 3,620.9 MB — monotone, ending at the maximum
* **no plateau at any point**; growth resumed rather than decelerating

And, decisively for what it rules *out*: gc-tracked objects did **not** trend (+2,071/market
against a 39 K–4.8 M range; −37,816 over the first fifty, −6,643 over the last fifty), live
sessions stayed at 2–3, file descriptors 16–26, cold backlog 1 of 6, lifecycle high-water 3 of 6.
Released market graphs really were released. Whatever is resident is not a retained session.

Collector pauses over the same run: 215 generation-2 collections costing 134.0 s, mean 623 ms,
max 1,738 ms; hot-path `observe` maximum 1,055.8 ms against a 23.6 µs median.

## 2. What was measured, not assumed

Nothing below started from "it must be fragmentation" or "it must be SQLite". The instruments
were added first (commit `cc7b9a6`) and the hypotheses were tested against them:

* `/proc/self/status` — `VmRSS`, `RssAnon`, `RssFile`, `RssShmem`, `VmSize`, `VmData`, `VmSwap`,
  `Threads`
* `/proc/self/smaps_rollup` — `Rss`, `Pss`, `Private_Clean`, `Private_Dirty`, `Shared_Clean`,
  `Shared_Dirty`, `Anonymous`, `AnonHugePages`, `Swap`
* glibc `mallinfo2` — `arena`, `hblkhd`, `uordblks` (in use), `fordblks` (free but retained)
* CPython — `gc.get_count`, `gc.get_stats`, `sys.getallocatedblocks`, tracked-object count
* thread names and counts; direct child processes read **separately** from the parent

Host: Linux 6.8, glibc 2.39, CPython 3.12.3. Every reader returns `None` where a platform will
not answer, and a machine with neither `/proc` nor glibc still produces a snapshot.

## 3. Experiment A — three real journals, three encoder paths

**Real evidence.** Three journals from `p13-corpus-6`, opened read-only and never modified: one
small, one at the corpus median, one of the largest. One fresh process per measurement, RSS
sampled every 2 ms during output.

| market | journal | steps | legacy encoder | current `encode_journal` | streaming writer |
|---|---:|---:|---:|---:|---:|
| `btc-updown-5m-1787890800` | 35.4 MB | 25,541 | **+48.2 MB** | +35.5 MB | **+0.0 MB** |
| `btc-updown-5m-1787907000` | 167.8 MB | 119,175 | **+254.9 MB** | +167.9 MB | **+0.0 MB** |
| `btc-updown-5m-1787925900` | 423.1 MB | 298,663 | **+652.3 MB** | +423.3 MB | **+0.0 MB** |

Peak resident above the pre-encode reading. `legacy` is `encode_journal` reproduced exactly as
`p13-corpus-6` ran it — a list of every encoded line, then a join over a second sequence of
`line + b"\n"`. The middle column is today's implementation. Measuring today's code and calling
it the baseline would have understated what the accepted run actually paid; the first pass of
this experiment did exactly that and was re-run.

**Byte identity: all nine runs produced a file whose SHA-256 and size equal the original
journal's.** The originals were re-hashed afterwards and are unchanged.

### What the same experiment says about *retention* (largest journal)

| reading | RSS | arena | uordblks | fordblks |
|---|---:|---:|---:|---:|
| after decode (graph resident) | 3,575.3 MB | 678.8 | 8.4 | 670.4 |
| after deleting the graph | 732.7 MB | 678.9 | 6.2 | 672.8 |
| after `gc.collect(2)` | 722.0 MB | 678.9 | 6.2 | 672.8 |
| after `malloc_trim(0)` | **50.9 MB** | 674.9 | 6.2 | 668.8 |

A full collection released **10 MB**. `malloc_trim` released **671 MB**. At that point 99 % of
the C heap was free and still resident.

By the interpretation written down before the numbers were taken, that is
`NATIVE_FREE_HEAP_RETAINED`: **not** live cyclic Python objects, but a C heap that glibc had not
returned to the kernel.

One more figure worth stating plainly: the decoded `ReplayStep` graph for a 423 MB journal is
**3,575 MB resident — 8.4× the journal bytes.** The encode transient is real, and it is smaller
than the graph it is built from.

## 4. Experiment B — the live collector

**`p13-diag-1`** — ten consecutive real paper markets, one process, 57.6 minutes, source
`cc7b9a66b01189b59e9cdd5c7ca02b1fb72bcfc2`, tree clean, `ACCEPTANCE_CLEAN`, `LIVE_TRADING_ENABLED`
and `REDEMPTION_ENABLED` false, 0 orders, 0 redemptions. The journal is written by
`encode_journal` **exactly as `p13-corpus-6` ran it**: this is the failing behaviour with
instruments attached, not a partly-fixed build.

Ten of ten COMPLETE, ten of ten replay EXACT, ten eligible, 0 drops, 0 gaps, 0 sink errors. The
target was eight; markets 9 and 10 were already in flight and were finished rather than abandoned.

| # | journal MB | post-release RSS | arena | fordblks | **uordblks** | encode ΔRSS | encode Δarena | gen2 | gen2 ms |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 198.9 | 595.1 | 246.1 | 228.7 | 17.4 | +416.9 | +206.7 | 1 | 421.0 |
| 2 | 100.6 | 623.6 | 289.5 | 274.0 | 15.5 | +109.8 | +9.2 | 0 | — |
| 3 | 178.6 | 667.0 | 331.6 | 313.9 | 17.7 | +218.6 | +39.9 | 1 | 270.0 |
| 4 | 158.6 | 678.7 | 342.4 | 324.1 | 18.3 | +158.6 | 0.0 | 1 | 343.3 |
| 5 | 120.7 | 688.6 | 348.9 | 329.3 | 19.6 | +127.1 | +6.4 | 1 | 338.6 |
| 6 | 143.1 | 682.7 | 343.3 | 323.3 | 20.0 | +136.7 | −6.4 | 1 | 284.7 |
| 7 | 148.0 | 682.7 | 343.4 | 323.0 | 20.3 | +148.0 | 0.0 | 1 | 361.7 |
| 8 | 150.4 | 927.6 | 581.1 | 561.7 | 19.4 | +393.3 | +237.7 | 1 | 465.1 |
| 9 | 100.9 | 927.9 | 581.2 | 564.1 | 17.1 | +100.9 | 0.0 | 1 | 465.1 |
| 10 | 87.9 | 927.9 | 588.7 | 575.3 | 13.3 | +87.9 | 0.0 | 1 | 301.7 |

All-run slope **+38.99 MB/market**, 95 % CI **[22.65, 55.34]**, r² 0.79. Bounded throughout: live
sessions ≤ 3, file descriptors ≤ 24, threads ≤ 9, pending tasks ≤ 11, cold backlog ≤ 1,
lifecycles ≤ 3.

**`uordblks` — the C heap actually in use — never left 13.3–20.3 MB.**

### Where the step is

Median and total change in RSS across each checkpoint transition, over the ten markets:

| transition | median | total |
|---|---:|---:|
| `market_start → capture_end` | +12.3 MB | +953.3 MB |
| `capture_end → before_journal_encode` | 0.0 | 0.0 |
| **`before_journal_encode → after_journal_write`** | **+142.4 MB** | **+1,897.8 MB** |
| `after_journal_write → after_step_release` | 0.0 | −2.1 |
| `after_step_release → after_latency_write` | −145.6 MB | −1,386.7 MB |
| `after_latency_write → after_settlement` | 0.0 | +1.2 |
| `after_settlement → after_store_close` | 0.0 | 0.0 |
| `after_store_close → after_cold_result` | 0.0 | +2.2 |
| `after_cold_result → after_release` | 0.0 | +7.1 |
| `after_release → post_release_settled` | 0.0 | 0.0 |

One transition accounts for the growth. Latency artifacts, settlement, store close, the cold
child's result and release itself contribute **nothing measurable** — which is the answer to
"should those be rewritten too": no, and the measurement is why.

The +1,897.8 MB added by the encode and the −1,386.7 MB released afterwards do not cancel. The
difference is the split visible in market 1: the joined journal `bytes` object is large enough
for glibc to serve from `mmap` (`hblkhd` 6.5 → 205.3 MB) and **is** returned to the kernel when
freed (`hblkhd` back to 4.3 MB). The per-line `bytes` objects are ~1.6 KB each — above CPython's
512-byte small-object threshold, below glibc's mmap threshold — so they come from the main
`arena`, and freeing them moves them to `fordblks` and no further.

### The quiescent probe

Taken at the one moment in a run when nothing is trading: every market closed, every cold task
drained. Interpretation fixed before the reading.

> **`NATIVE_FREE_HEAP_RETAINED`** — `gc.collect(2)` released **3.1 MB in 0.01 s**;
> `malloc_trim(0)` released **576.3 MB**.

## 5. What the evidence establishes, and what remains inference

### Established by measurement

1. **The resident bytes are free C heap, not live Python objects.** `uordblks` never left
   13.3–20.3 MB across ten markets while `arena` went 246 → 589 MB. At the end of the run a full
   collection released 3.1 MB and `malloc_trim` released 576.3 MB. Three independent instruments
   agree, and the gc-tracked count in the accepted corpus already said the same thing from the
   other direction.
2. **The allocation that grows the heap is the journal encode.** Of ten checkpoint transitions,
   one carries the growth (+1,897.8 MB total) and the rest carry effectively nothing. It is
   visible per market and per allocator: `arena` +206.7 MB on the first market with `uordblks`
   unchanged.
3. **Why that allocation and not the recorded graph.** At `capture_end` the arena held 22.8 MB
   while RSS was 368.6 MB — the `ReplayStep` graph is small objects, which CPython serves from
   `pymalloc` arenas that *are* returned. The journal's per-line `bytes` are ~1.6 KB: above
   CPython's 512-byte threshold, below glibc's mmap threshold, so they come from the main arena
   and freeing them reaches `fordblks` and stops there.
4. **Why the joined object hid it.** The final `bytes` object is large enough for glibc to serve
   by `mmap` (`hblkhd` 6.5 → 205.3 MB) and *is* returned on free (back to 4.3 MB). RSS therefore
   falls part-way back after every market, which is exactly what makes the residue look like
   ordinary churn in a `statm`-only view.
5. **Why it never plateaued over 202 markets.** The transient scales with the market: journals
   ran 37 to 443 MB, and two encodes can overlap — market 8 of the pilot took +237.7 MB of arena
   for a 150 MB journal because market 9 had already launched. The high-water keeps finding new
   maxima, and a heap high-water does not come down.

### Not established — stated as inference or as unknown

* That fragmentation *per se* contributes beyond the high-water effect. The measurements are
  consistent with it and do not isolate it.
* Why market 11 of the post-fix pilot advanced the arena by 35.9 MB when markets 7–10 had not.
  It is located (`after_step_release → after_latency_write`) and its size is bounded, but the
  trigger for that particular step is not established.
* Thread growth 8 → 12 in the accepted corpus is **not** an explanation of gigabytes and is not
  offered as one. In both pilots threads stayed ≤ 9 and file descriptors ≤ 24.
* Nothing here says the collector's generation-2 pauses are caused by the same thing. See §7.

## 6. The fix

`src/maker5m/replay/codec.py` — `iter_encoded_journal(journal)` yields exactly
`encode_line(record) + b"\n"` per record, in the same order. `encode_journal` becomes its
concatenation, so it keeps its name, signature and output.

`src/maker5m/replay/writer.py` — `write_journal_stream(path, journal)` consumes the iterator into
a `.partial` file one line at a time, hashing and counting as it writes, then renames. A failed
write leaves nothing at the real path: a short journal is a different market and must not be
somewhere the verifier would succeed on it.

`src/maker5m/bot/session.py` — `MarketSession.write_journal` calls the writer instead of building
`bytes`. It takes the size and digest from the writer rather than measuring the file afterwards,
and the corpus row records that digest **beside** the one the cold child computes from the
finished file, so a disagreement is visible rather than silently resolved.

### Why this is the minimal change

Of the ten checkpoint transitions, nine contribute nothing measurable. Latency artifacts,
settlement, SQLite close, the cold child's result and release itself were **measured** and left
alone — including the latency sidecars, which §13 of the brief nominates as the next suspect and
which the numbers do not support rewriting. Nothing was changed on suspicion.

### P5 byte identity

* streamed bytes `==` `encode_journal(journal)` on every codec fixture;
* the streamed file decodes to the same `Journal`;
* `encode_journal(decode_journal(streamed))` `==` streamed;
* and on the three **real** corpus journals above, the streamed file's SHA-256 and size equal the
  original's, with the originals re-hashed afterwards and unchanged.

No schema change, no record reordering, no JSON option changed, no newline contract changed, no
strategy change, no replay semantics change.

### Post-fix pilot — `p13-fix-1`

Fourteen consecutive real markets, one process, 76.8 minutes, source
`376cfcdd4a72f8909bd44598f1d8e43c988c3da6`, tree clean, `ACCEPTANCE_CLEAN`, 0 orders, 0
redemptions. Fourteen of fourteen COMPLETE, replay EXACT, eligible; 0 drops, 0 gaps, 0 sink
errors, 0 append failures.

| # | journal MB | post-release RSS | arena | fordblks | uordblks | encode ΔRSS | encode Δarena |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 131.6 | 308.4 | 41.4 | 25.2 | 16.2 | +5.1 | +0.4 |
| 2 | 154.4 | 369.3 | 112.5 | 95.6 | 17.0 | +3.6 | +0.1 |
| 3 | 126.7 | 387.4 | 123.7 | 103.1 | 20.6 | 0.0 | 0.0 |
| 4 | 159.7 | 406.8 | 125.7 | 105.8 | 19.9 | +2.6 | 0.0 |
| 5 | 95.4 | 411.0 | 132.5 | 113.8 | 18.7 | 0.0 | 0.0 |
| 6 | 140.1 | 416.6 | 143.6 | 126.8 | 16.8 | +1.1 | +1.0 |
| 7 | 141.9 | 425.4 | 145.9 | 128.0 | 17.9 | 0.0 | 0.0 |
| 8 | 150.4 | 427.1 | 145.9 | 125.0 | 20.9 | 0.0 | 0.0 |
| 9 | 78.9 | 428.6 | 145.9 | 127.4 | 18.5 | 0.0 | 0.0 |
| 10 | 158.1 | 428.8 | 145.9 | 125.3 | 20.6 | 0.0 | 0.0 |
| 11 | 113.7 | 451.9 | 181.8 | 163.9 | 18.0 | 0.0 | 0.0 |
| 12 | 135.3 | 454.1 | 181.8 | 160.5 | 21.3 | 0.0 | 0.0 |
| 13 | 133.8 | 454.8 | 182.4 | 160.8 | 21.6 | 0.0 | 0.0 |
| 14 | 186.2 | 440.9 | 153.5 | 139.7 | 13.8 | 0.0 | 0.0 |

Step attribution, medians and totals over the fourteen markets:

| transition | median | total | (pre-fix total) |
|---|---:|---:|---:|
| `market_start → capture_end` | +11.1 MB | +477.0 | +953.3 |
| **`before_journal_encode → after_journal_write`** | **0.0** | **+12.4** | **+1,897.8** |
| `after_step_release → after_latency_write` | +0.5 | +31.5 | −1,386.7 |
| `after_store_close → after_cold_result` | 0.0 | +34.3 | +2.2 |
| `after_cold_result → after_release` | 0.0 | +13.8 | +7.1 |

End-of-run quiescent probe: `NATIVE_FREE_HEAP_RETAINED`, full collection **1.0 MB**,
`malloc_trim` **55.5 MB** — the same verdict as before the fix, over an order of magnitude less
of it (576.3 MB).

Market 14 is the useful one: the largest journal of the run, 186.2 MB, cost 0.0 MB at the encode
and ended *below* market 13.

**This pilot is not the resource gate.** It shows the fix behaving as the mechanism predicts.
The gate is decided by the ≥50-market validation under the test declared before it ran.

## 7. Generation-2 attribution

`GcObserver` kept a running maximum, and the corpus report read it as each market's own. Across
`p13-corpus-6`, forty markets over 100 ms were credited with a generation-2 pause and only one of
them had actually raised the cumulative figure. That is an attribution defect, not a summary
choice, and no amount of care in the report could have recovered from it.

`GcEventLog` keeps each collection's generation, start and end, and a market's live window —
recorded on `perf_counter_ns`, the same clock — is intersected with them. Two things follow that
the old instrument could not express:

* a market with **no** full collection says so. `p13-diag-1` market 2 recorded
  `{'1': 318}` and no generation-2 pause at all, where the running maximum would have handed it
  market 1's 421 ms;
* a collection that **spans two markets** is attributed to both, with the whole pause and the
  part inside each window kept separately. Markets 8 and 9 of `p13-diag-1` both carry the same
  465.1 ms collection, which is the truth about what happened rather than a coincidence.

### What the memory fix did and did not do to the pauses

It did not fix them, and this document does not claim it did. Generation-2 collections continued
throughout the post-fix pilot — 284.9, 377.9, 295.1, 352.4, 352.4, 288.4, 374.0, 293.4, two
totalling 860.3, 566.9, 392.3, 335.3, 475.1 ms — on a process holding a much smaller heap.

That is consistent with the cost being driven by the number of *tracked objects* in the live
recorded graph rather than by resident bytes, and streaming the journal does not shrink the graph
that accumulates during a market. Whether it is worth changing anything about that is a separate
question with its own evidence, and no threshold is proposed here: **no latency requirement for
full-collection pauses has been established**, so `GC TAIL` remains an open P14 readiness risk
rather than a gate anyone can pass or fail.

`gc.collect` is not called on any ingress path — not in the observer, the tick, the strategy, the
risk overlay, the reconciler or a feed callback — and the thresholds remain `(700, 10, 400)`,
unchanged.

---

## 8. Resource validation — `p13-resource-1`

**57 consecutive real paper markets, one process, no restart, 4 h 48 m.** Frozen candidate:
source `376cfcdd4a72f8909bd44598f1d8e43c988c3da6`, tree
`a9a19a07c8b640a225620230fda9c21ee8a88cb9`, config
`b0d94e5927bbd574a2e55d8a491e79054998b68f295962d0632a19d08f9a135c`, tree clean,
`ACCEPTANCE_CLEAN`, `LIVE_TRADING_ENABLED` and `REDEMPTION_ENABLED` false, 0 orders sent, 0
redemptions sent, 0 chain writes.

The target was 55; markets 56 and 57 were already in flight and were finished rather than
abandoned. This is a **resource** validation corpus. It does not replace `p13-corpus-6`, which
remains the accepted 202-market empirical dataset.

57 of 57 COMPLETE · 57 of 57 replay EXACT · 57 of 57 evidence-eligible · 0 incomplete · 0 corrupt
· 0 drops · 0 gaps · 0 sink errors · 0 corpus append failures · 0 truncated lines · **0
journal-digest disagreements between the writer and the cold child**.

### The predeclared test

Declared in the brief before the run existed: first ten markets are warm-up; over markets 11..end
the OLS slope of post-release RSS against market index must have a 95 % interval containing zero
and a point slope no greater than **+1.026 MB/market**, a tenth of the measured +10.26 all-run
failure slope.

| | post-release RSS | settled RSS |
|---|---:|---:|
| markets | 57 | 57 |
| first → last | 278.6 → 489.8 MB | 278.9 → 489.8 MB |
| all-run slope | +2.129 [+1.751, +2.506] | +2.132 [+1.756, +2.509] |
| **after warm-up, n = 47** | **+1.3607 MB/market** | **+1.3607** |
| **95 % CI** | **[+1.1689, +1.5526]** | **[+1.1689, +1.5526]** |
| CI contains zero | **no** | **no** |
| within +1.026 ceiling | **no** | **no** |
| window medians | 432.0 / 463.8 / 480.6 / 485.0 | same |
| **verdict** | **NOT PASSED** | **NOT PASSED** |

Both metrics give the same answer, so no choice of series changes it.

### The warm-up is not the reason

The staircase in this run ran to about market 33, past the predeclared ten-market warm-up, and a
longer warm-up would have flattered the slope. Moving it after seeing the numbers is precisely
what the brief forbids, so it was not moved — and it would not have mattered, because **no
window contains zero**:

| window | n | slope | 95 % CI | contains zero |
|---|---:|---:|---|---|
| **11..57 — the gate** | 47 | **+1.3607** | [+1.1689, +1.5526] | no |
| 21..57 | 37 | +0.9824 | [+0.7939, +1.1709] | no |
| 31..57 | 27 | +0.4775 | [+0.4276, +0.5274] | no |
| last 20 | 20 | +0.4819 | [+0.3919, +0.5720] | no |
| last 10 | 10 | +0.5656 | [+0.3913, +0.7399] | no |

The process is much flatter than before and it is **not flat**: a small, statistically supported
trend of about +0.5 MB/market persists to the end, and the last-ten median (486.7 MB) sits above
the markets-38-to-47 median (481.9 MB).

### What the fix did achieve

| | `p13-corpus-6` (failed) | `p13-resource-1` |
|---|---:|---:|
| all-run slope | +10.26 MB/market | **+2.13** |
| late-window slope | +31.46 (last 50) | **+0.48** (last 20) |
| RSS at end | 4,261.7 MB after 202 markets | **489.8 MB after 57** |
| `malloc_trim` at end of run | — | 63.0 MB (`p13-diag-1`: 576.3 MB) |
| journal encode step | +142.4 MB median | **0.00 MB median** |

An eight-fold reduction in all-run slope and a sixty-fold reduction in the late-window slope,
with the identified mechanism removed. It is a large, real, measured improvement that does not
meet the bar set in advance.

### Why there is no second fix of the same shape

Step attribution over the 57 markets shows the residue is **diffuse**, not concentrated:

| transition | median | total over 57 |
|---|---:|---:|
| `market_start → capture_end` | +0.59 MB | +539.4 (the market's own graph, released after) |
| `before_journal_encode → after_journal_write` | **0.00** | **+21.2** |
| `after_step_release → after_latency_write` | 0.00 | +17.5 |
| `after_settlement → after_store_close` | 0.00 | +13.2 |
| `after_store_close → after_cold_result` | 0.00 | +15.4 |
| `after_cold_result → after_release` | 0.00 | +15.6 |

The cold-path stages together contribute about +83 MB over 57 markets — roughly +1.4 MB/market,
which is the measured slope. Each stage contributes two or three tenths of a megabyte of arena
high-water and none dominates. There is no single remaining allocation to stream.

The end-of-run probe still reads `NATIVE_FREE_HEAP_RETAINED` — a full collection released
**0.0 MB**, `malloc_trim` released **63.0 MB**, and `fordblks` (169.5 MB) is 90 % of `arena`
(188.0 MB). The residue is the same kind of thing as before, at a tenth of the size: glibc heap
high-water creep across many small transient peaks.

**No mitigation for that is applied here, and the reasons are stated rather than assumed.** A
periodic `malloc_trim` is the obvious candidate and the brief forbids calling it while a market
is trading — correctly, since it takes the allocator lock and would stall the ingress owner. In a
continuous collector, live sessions are never zero between markets, so there is no safe point for
it inside a run. Allocator tuning (`M_MMAP_THRESHOLD`, `M_TRIM_THRESHOLD`) is explicitly out of
scope as a fix at this stage. Both remain open, measured options; neither is taken on a guess.

### Bounded, as required

Live sessions ≤ 3 · file descriptors ≤ 24 · threads ≤ 10 · pending tasks ≤ 11 · cold backlog ≤ 1
of 6 · market lifecycles ≤ 3 of 6. Nothing accumulated except resident bytes.

### P8C, on the frozen source

decide p50 **+68 ns (+0.24 %)** against a 3 % limit; full cycle p50 **+1,721 ns (+3.78 %)**
against a 5 % limit. Both met, neither limit moved, no hot-path clock added. The machine carried
an unrelated 439 %-CPU workload throughout and the figure is reported as measured.

---

## 9. Allocator maintenance — the smallest evidence-backed mechanism

`p13-resource-1` removed the dominant cause and missed the bar: after-warm-up slope
**+1.3607 MB/market, 95 % CI [+1.1689, +1.5526]** against a predeclared ceiling of +1.026 with a
CI required to contain zero. What it also established is that the residue is the *same kind of
thing* at a tenth of the size — a full collection released 0.0 MB where `malloc_trim` released
63.0 MB, and `fordblks` (169.5 MB) was 90 % of `arena` (188.0 MB).

Nothing about that is a Python problem, so nothing in Python fixes it. The memory is free and
glibc is holding it; `malloc_trim` gives it back. The reason that was not simply done during the
diagnostic work is that the call takes the allocator's locks for the whole process.

### The window is the market clock's

Canonical timing stops quoting at `T0+280`; the next market does not quote until its own `T0+3`,
which is `T0+303` on the closing market's clock. In that 23-second gap the closing market is
`SETTLING` and the opening one is `PREARM` — **no market is legitimately in `QUOTE` or
`ENDGAME`.** Ten seconds are reserved, so maintenance may begin no later than `T0+293` however
long the call takes, and a window already missed is skipped rather than run late.

Six conditions, evaluated together, with phases derived from each live session's own `t0_ns`
through P2's phase machine rather than read from whatever a market last observed: no session in
`QUOTE`; none in `ENDGAME`; past the stop-quoting boundary; at least the margin before the next
quote start; this rollover not already maintained; not shutting down.

One `malloc_trim(0)` per rollover, **not adaptive** — it does not consult resident memory,
`fordblks`, market activity or journal size, because a policy that responded to any of those
would turn one experiment into a search over policies. A test reads the decision's own source to
keep it that way.

**The thread is not the safety property.** The call runs off the event loop, and a feed thread
allocating a book update waits on the allocator lock wherever the call was made from. What
confines it is the window. The loop-isolation test is named for what it proves.

### Controlled real pilot — `p13-trim-pilot-2`

**`CONTROLLED_LOCAL_ALLOCATOR_MAINTENANCE_ON_REAL_MARKET_DATA`** — a local action taken
deliberately against real market data. Not a venue incident.

Eight consecutive real markets, one process, 44 minutes, source
`6af825bf3c33b41f8efc2a3cf2b2f5ca6036c933`, tree clean, `ACCEPTANCE_CLEAN`, 0 orders, 0
redemptions. Eight of eight COMPLETE, replay EXACT, eligible; 0 drops, 0 gaps, 0 sink errors, 0
lost observations; both feeds ready at every T0; `classified == actions == 2 × decisions`; PLACE
only under SAFE.

Nine trims, every one with phases `{SETTLING, PREARM}` and **none in `QUOTE` or `ENDGAME`**:

| rollover | duration | released | `fordblks` before → after | since stop-quote | to quote start |
|---|---:|---:|---|---:|---:|
| 1787995200 | 0.26 ms | 0.1 MB | 1.1 → 0.7 | 2.9 s | 20.1 s |
| 1787995500 | 1.61 ms | 4.3 MB | 5.6 → 5.3 | 2.3 s | 20.7 s |
| 1787995800 | 2.04 ms | 11.0 MB | 35.9 → 33.0 | 2.7 s | 20.3 s |
| 1787996100 | 1.02 ms | 11.8 MB | 69.4 → 66.0 | 2.1 s | 20.9 s |
| 1787996400 | 1.59 ms | 17.7 MB | 98.6 → 92.7 | 2.7 s | 20.3 s |
| 1787996700 | 4.47 ms | 16.5 MB | 103.5 → 91.2 | 2.2 s | 20.8 s |
| 1787997000 | 5.61 ms | 18.3 MB | 105.4 → 94.5 | 2.6 s | 20.4 s |
| 1787997300 | 1.40 ms | 14.1 MB | 102.3 → 99.0 | 3.0 s | 20.0 s |
| 1787997600 | 1.28 ms | 15.8 MB | 104.0 → 98.3 | 2.3 s | 20.7 s |

p50 **1.59 ms**, p95 and max **5.61 ms**; **109.8 MB** returned in total. Nine successful, none
unsupported, no errors. The guard refused roughly 277 times per market while that market quoted.

Hot-path `observe` maxima, two seconds before against two seconds after each trim: median
0.212 → 0.159 ms, max 1.150 → 0.953 ms. No elevation follows a trim. **No latency threshold is
proposed.**

### Two things that looked like failures, resolved against readings

**A reconnect.** Market `btc-updown-5m-1787996100` recorded `reconnects=1`. Its own prearm trim
(rollover 1787996100) shows `reconnects=0` across all five snapshots; the next trim (rollover
1787996400, at its stop-quoting boundary) shows `reconnects=1` **already at `window_open`, before
the trim ran**. Between those points the market was in `QUOTE`/`ENDGAME`, where the contract
refused every instant. **No trim ran while that reconnect happened.** For scale, `p13-corpus-6`
recorded 13 reconnects across 202 markets.

**Apparent buffer drops in the probe.** The around-trim readings showed transient values of 1 and
5. `ObservationBuffer.dropped` is *derived* — `accepted − drained − len(records)` — and `drain()`
clears the deque before incrementing `drained`, so a reader on another thread sees a positive
value that is not a drop. The authoritative per-market accounting reports `dropped_records=0`,
`lost_observations=0` and `observations_consumed == decisions_written` on all eight markets.
Recorded here because the probe's number is misleading and someone will read it again.

### A limitation that is not argued away

`during_trim` recorded **no observations at all** — n=0 across all nine trims. Ingress advanced
about 1,000 ordinals over each ~12-second probe, roughly one every 12 ms, against trims of 0.26
to 5.61 ms. Zero events is exactly what a no-impact trim predicts, and the measurement **cannot
distinguish "no event was due" from "an event was delayed by up to 5.6 ms".** What the pilot
establishes is that no market suffered a feed-integrity or trading-state failure; it does not
establish that no individual message was delayed by single-digit milliseconds.

### First pilot, retained

`p13-trim-pilot` (source `18108ef`, eight markets, also 8/8 clean) is kept rather than deleted.
Its per-trim ingress readings were held in memory and lost when the process exited, so it
supports the market-level claim and not the finer one. That is why the pilot was re-run, and
saying "no market broke" where "ingress did not pause" was required is the substitution this
phase exists to refuse.
