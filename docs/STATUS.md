# Status

Compact project tracker. Update at **every accepted git boundary** — this file plus the
commit is the audit trail.

---

## LIVE TRADING: DISABLED

`maker5m.safety.LIVE_TRADING_ENABLED` is `False`. P7 adds a complete execution architecture
including a real authenticated write adapter, and **it cannot be armed**: `VenueAdapter.arm_live`
raises before any credential is read or any socket is opened.

P10 adds settlement and redemption **planning** only. `REDEMPTION_ENABLED` is `False` and
`Redeemer.submit()` raises `RedemptionDisabledError` before touching a credential; there is no
`--redeem-live` flag, no `REDEEM=true` variable and no config bypass. Twenty-one real markets
were settled on paper against the real on-chain payout vector and **zero** transactions of any
kind were sent. Every RPC call is read-only, enforced behaviourally rather than by name: any
module that POSTs may issue only `eth_chainId`, `eth_getCode`, `eth_call`, `eth_getBlockByNumber`
and `eth_getLogs`.

P9 adds operational safety only, and its two real-market runs placed **zero** orders. The risk
package cannot construct an order at all — a halt empties intent, and there is no SELL, hedge,
flatten, merge, split, or convert anywhere in it.

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

## P13: 202-market live-paper corpus — acceptance audit

Full evidence in [`evidence/P13-CORPUS-ACCEPTANCE.md`](evidence/P13-CORPUS-ACCEPTANCE.md).

**Two SHAs, and they are not the same.** The corpus was collected by
`9a42031df1f46762a0a8ef958240342612586084` (tree `29aca3d58f1b4c3cf65161b99fb4137566c3adf5`) on a
clean tree, before this documentation existed. The acceptance commit is necessarily later and
changes only `docs/`. All 202 rows carry the collecting revision, and no document claims otherwise.

**The empirical corpus gate passes.** 202 markets, epoch `p13-corpus-6`, one process, 16 h 58 m,
no restart. Re-audited against the durable files rather than the collector's summary: 202 rows,
202 qualifying, 202 unique attempts, 202 unique slugs, 202 started and finished, **zero** failed,
aborted, open, duplicate-start, duplicate-terminal, duplicate-result, duplicate-slug, refused rows
or refused latency artifacts. 202/202 stores COMPLETE with verified archives; 202/202 replays
EXACT and byte-identical; 24,712,774 decisions → 49,425,548 side opportunities with
`classified == actions == 2 × decisions` on every market; 202/202 feeds warm at T0; **PLACE SAFE
105,024, HALTED 0, RECOVERING 0**; 0 drops, gaps and sink errors; 202 settlements RESOLVED (96 UP,
106 DOWN); one epoch, revision, tree and config across every row.

Exhaustive L3: AT_FRONT 33.87 %, PRICE_OK_BUT_DEEP 13.33 %, NOT_QUOTING 52.79 %, OFF_PRICE 0 %,
STALE 0.013 %. Merged live latency: CLOB receive→decide p50 114,643 ns / p99 849,083 ns over
2.36 M samples; spot p50 78,463 / p99 535,139 over 106,789; decide_duration p50 29,226 ns.

**The resource gate does not pass.** Post-release RSS rose 36 MB → 4,262 MB across the run.
Quartile medians climb monotonically (2,351 → 2,464 → 2,780 → 3,621 MB), the run ends at its
maximum, and the last-50 slope (+31.5 MB/market) matches the first-50 slope (+32.2) and is seven
times the middle-100 slope (+4.7). **No plateau is demonstrated: `CONTINUED_PROCESS_RESIDENT_GROWTH`.**

Object counts say what they can and no more: live sessions 2-3, fds 16-26, cold backlog 1 of 6,
lifecycle high-water 3 of 6, and Python tracked objects **not trending** (+2,071/market against a
39 K-4.8 M range, negative over the first and last fifty). So the market graphs really are
released and this is not a retained-session leak. What grew is resident memory whose source this
instrumentation does not identify — untracked allocations, native SQLite/LZMA buffers, thread
stacks and fragmentation are all consistent and none is established. Threads also rose 8 → 12
(max 13): bounded, not flat.

**Tail latency, recorded not solved.** `observe` maxima reach 1,056 ms; 19.8 % of markets exceed
100 ms, 12.4 % exceed 500 ms. 215 full collections cost 134.0 s over the run, mean 623 ms. In the
worst twenty markets the decide/prepare/reconcile maxima are 1-20 ms, so the time is spent around
the cycle — but full collections occur in 82 % of quiet markets too, and `GcObserver` records a
run-cumulative maximum, so per-market attribution is not possible from this run. No threshold is
proposed.

### Gates

* P13 implementation — **PASSED**
* P13 pilot — **PASSED**
* P13 ≥200-market empirical corpus — **PASSED** (202)
* P13 long-run resource stability — **NOT PASSED**
* **P13 overall — NOT COMPLETE**

The dataset is **ready for P15 analysis**; the collector needs runtime engineering before longer
unattended runs. **P14 is BLOCKED** on resident-memory growth, with GC tail latency recorded as a
second readiness risk. O01-O09 remain OPEN — P13 produced the evidence, P15 owns the experiments.

---

## P13 resource stability — diagnosed, fixed, still short of the bar

`fix/p13-runtime-resource-stability`, from `2eb7d9a`. `p13-corpus-6` is untouched and revalidates
to the figure in every table above. Full detail: `docs/evidence/P13-RESOURCE-DIAGNOSIS.md`.

**The cause, measured rather than guessed.** The resident bytes were free glibc heap. Across ten
real markets `uordblks` — memory actually in use — never left 13.3–20.3 MB while `arena` went 246
to 589 MB, and the end-of-run probe released 3.1 MB to a full collection against **576.3 MB to
`malloc_trim`**. Of eleven per-market memory checkpoints, exactly one transition carried the
growth: the journal encode, +1,897.8 MB over ten markets against nothing measurable from the
latency artifacts, settlement, store close, cold child or release. `encode_journal` built every
line as a separate ~1.6 KB `bytes` object — above CPython's small-object threshold, below glibc's
mmap threshold — so they came from the main arena and freeing them reached `fordblks` and stopped.
The joined journal object was mmap'd and *was* returned, which is why a `statm`-only view read
this as ordinary churn.

**The fix.** `iter_encoded_journal` plus `write_journal_stream`: one line resident at a time,
hashed as it is written. `encode_journal` is now its concatenation and keeps its name, signature
and output. Byte identity proved on every codec fixture and on three **real** corpus journals of
35.4, 167.8 and 423.1 MB — same SHA-256, same size, originals unchanged. Nothing else was
touched, because the checkpoints showed nothing else contributed.

**The result.** `p13-resource-1`, 57 consecutive real markets in one process over 4 h 48 m:
57 COMPLETE, 57 replay EXACT, 57 eligible, 0 drops, 0 gaps, 0 sink errors, 0 append failures, 0
writer/child digest disagreements, bounded resources throughout.

| | failed corpus | after the fix |
|---|---:|---:|
| all-run slope | +10.26 MB/market | **+2.13** |
| late-window slope | +31.46 (last 50) | **+0.48** (last 20) |
| RSS at end of run | 4,261.7 MB / 202 markets | **489.8 MB / 57 markets** |
| journal encode step | +142.4 MB median | **0.00 MB median** |
| `malloc_trim` at end | — | 63.0 MB (was 576.3 MB) |

**And it does not pass.** The predeclared test — markets 11..end, ceiling +1.026 MB/market, 95 %
interval containing zero — reads **+1.3607 [+1.1689, +1.5526]**. The ceiling was not moved after
the fact and the warm-up was not extended, which would have flattered it: **no** window contains
zero, including the last ten (+0.5656 [+0.3913, +0.7399]). A small, statistically real trend of
about +0.5 MB/market persists to the end of the run.

**Why no second fix.** The residue is diffuse. Every cold-path stage contributes two or three
tenths of a megabyte of arena high-water and none dominates — about +1.4 MB/market in total,
which is the measured slope. There is no remaining allocation of the journal's shape to stream. A
periodic `malloc_trim` is the obvious candidate and takes the allocator lock, so it may not run
while a market is trading; live sessions are never zero inside a continuous run. Allocator tuning
is out of scope at this stage. Both remain open, measured options, and neither is taken on a
guess.

**The collector's pauses are not fixed and are not claimed to be.** Generation-2 collections
continued throughout the post-fix runs on a much smaller heap. What did change is that they can
now be attributed: `GcObserver` kept a *running* maximum, and the corpus report read it as each
market's own — 40 markets credited with a pause only one had caused. `GcEventLog` keeps each
collection's generation, start and end, so a market with no full collection says so, and a
collection spanning two markets is recorded against both. No latency requirement for those pauses
exists, so **GC tail remains an open P14 readiness risk**, not a gate.

P8C on the frozen source: decide **+0.24 %** (3 % limit), full cycle **+3.78 %** (5 % limit).
Both met, neither limit moved.

---

## P13 allocator maintenance — the mechanism works, the result is negative

`fix/p13-resource-stability-closure`, from `1344e7f`. `p13-corpus-6` untouched and revalidating
identically. Full detail: `docs/evidence/P13-RESOURCE-DIAGNOSIS.md` §9-10.

`p13-resource-1` left the residual identified as free glibc heap that a `malloc_trim` gives back.
The smallest evidence-backed mechanism was tried: **one `malloc_trim(0)` per rollover**, confined
to the gap between one market's stop-quoting boundary at `T0+280` and the next market's quote
start at `T0+303`, with ten of those twenty-three seconds reserved, phases derived from P2's
phase machine, and a missed window skipped rather than run late.

**The contract works.** Across two pilots and a 57-market validation, 68 trims: every one with
phases `{SETTLING, PREARM}`, **none** with any market in `QUOTE` or `ENDGAME`, each beginning
within 3.0 s of the boundary with at least 20.0 s to spare. p50 1.6 ms, max 13.4 ms. The pilot's
one reconnect was shown by its own five-snapshot record to have happened while the market was
quoting, where the contract refuses every instant — no trim was running. Apparent buffer "drops"
in the probe were a cross-thread read of a derived quantity; the authoritative accounting is zero
on every market.

**And it makes resident memory worse.** `p13-resource-2` returned **1,082.6 MB** to the kernel
across 59 trims and finished at **534.2 MB** against `p13-resource-1`'s 489.8, with an
after-warm-up slope of **+2.7434 [+2.3512, +3.1357]** against +1.3607 — while doing **11 % less
work** (6,140 MB of journals against 6,905; 4.17 M decisions against 4.71 M). The late windows get
steeper, not flatter: last ten **+12.8126**. A flat band across markets 34-44 did not hold.

The gate was not changed: markets 11..end, ceiling +1.026, 95 % interval containing zero. It reads
NOT PASSED on both metrics and no window contains zero.

Nothing was retimed or retuned in response. The machinery and its 45 tests stay for the next
predeclared experiment; the **default is off**, because a runtime should not ship an action that
measurement says makes the thing it targets worse. `MALLOC_ARENA_MAX`, `MALLOC_TRIM_THRESHOLD_`,
jemalloc, mimalloc and process recycling are the next candidate families, each needing its own
experiment.

P8C on this source: decide **+1.48 %** (3 % limit), full cycle **+3.90 %** (5 % limit). Both met,
neither limit moved.

---

## P13F: Plane-3 audit isolation and result uniqueness

Full evidence in [`evidence/P13F-AUDIT-ISOLATION.md`](evidence/P13F-AUDIT-ISOLATION.md).
`p13-corpus-5` is stopped, preserved and **SUPERSEDED_FOR_FINAL_P13_ACCEPTANCE** — 52 COMPLETE
replay-exact rows, all 52 qualifying under the corrected verifier, with the runtime and durable
counters agreeing throughout. What was wrong was where the work ran.

**The audit was running on the trading loop.** Every corpus and ledger append, every fsync, and a
full re-read of the corpus plus an LZMA decompression of *every historical latency artifact*
happened inside `_finalize` — a coroutine on the loop consuming the next market's frames. By
market 52 that was 52 decompressions per market; over two hundred it is 20,100. `AuditIO` now owns
all of it on a single dedicated thread; the supervisor's `corpus` and `ledger` are views onto that
owner rather than second handles.

Measured rather than asserted: with a 250 ms audit operation in flight, a heartbeat coroutine
keeps running through the audit owner and makes **exactly zero** progress when the same call is
made inline, as the previous version made it.

**Counting is O(1) per market.** The full joined audit runs once at startup and once when the
target is reached; each finalised market is judged alone, reading one artifact — 1 read
incrementally against 201 for the full audit, in a test with 200 fixtures. `completed` is the size
of a set of qualified attempt ids, so no sequence of observations can count a market twice, and
the target is met only when a full off-loop audit agrees.

**Two rows could each count.** One result per attempt and one result per market are now enforced —
the gate is two hundred *markets*, not two hundred JSON lines — and the report pairs judgements to
rows by position, so a refused row's counts can no longer enter the aggregates on a qualifying
neighbour's ticket.

### Pilot — three real markets with the audit deliberately slowed

`CONTROLLED_LOCAL_FAULT_ON_REAL_MARKET`: 500 ms injected into every audit operation.

```text
1787874900  COMPLETE EXACT   87,223 dec  174,446 classified  audit 500-529 ms  hot max 1.3 ms
1787875200  COMPLETE EXACT  106,178 dec  212,356 classified  audit 517-533 ms  hot max 272.7 ms
1787875500  COMPLETE EXACT  153,847 dec  307,694 classified  audit 517-535 ms  hot max 720.8 ms
```

3 qualifying, 3 unique attempts, 3 unique slugs, 0 duplicates, 3 identity-valid artifacts, 0 drops,
gaps or sink errors, PLACE only under SAFE, three settlements RESOLVED. The larger hot-path maxima
are the garbage collector, and the numbers say so exactly — gen-2 maxima of 384 ms and 721 ms
against hot-path maxima of 272.7 ms and 720.8 ms. That is P13B's measured effect, unchanged.

P8C: decide p50 +481 ns (+2.01 %), full cycle +2,476 ns (+4.67 %) — both limits met, neither
moved. The benchmark exercises persistence and UI code this round did not touch, and the machine
was carrying an unrelated workload throughout; the figure is reported as measured rather than
re-run until it looked better.

---

## P13E: counter integrity

Full evidence in [`evidence/P13E-COUNTER-INTEGRITY.md`](evidence/P13E-COUNTER-INTEGRITY.md).
`p13-corpus-4` is stopped, preserved and **SUPERSEDED_FOR_FINAL_P13_ACCEPTANCE**.

**Successful markets were counted twice.** P13D added the correct increment after the terminal
record and did not remove the old one after the corpus append, so a `--markets 200` run would have
stopped at about a hundred. The collector's own log says it: **eight completion lines over four
corpus rows**, preserved unedited. Worse than the arithmetic, production was deciding completion
with a fourth simplified copy of the rule while P13D's whole point was that there is one.

The counter is no longer incremented. After the terminal record it is re-derived from the corpus
and the ledger by the shared qualifier, and the runtime total is compared against the durable
count at every boundary; disagreement is a collector-integrity fault that stops an acceptance run.
The new tests call `Supervisor._finalize` itself — P13D's recreated the intended sequence by hand
and passed while the shipped code counted twice.

**Duplicate starts were collapsed.** `starts` was a dict keyed by attempt id while terminals, three
lines away, were a list precisely so duplicates could be seen. Both are lists now.

**Absence was read as agreement.** Identity fields were compared only `if name in start`, so a
record missing `source_tree_sha` satisfied the comparison against it. Both records must now carry
every required field. This is why the **P13C pilot no longer qualifies**: its terminals predate the
fields P13D added. Its stores, replays and latency artifacts are all still valid; the records
simply do not carry what the contract now requires, so it is retained and superseded.

**"Exact equality" was a comment.** `True == 1` and `1.0 == 1` in Python, so an artifact declaring
`schema_version = True` passed. Typed comparisons now, and `condition_id` and `t0_ns` are required
rather than compared-if-present.

### Revalidation — `p13d-pilot-1`, four consecutive real markets

```text
1787856000  COMPLETE EXACT  108,892 dec  (runtime 1 / durable 1)
1787856300  COMPLETE EXACT  103,783 dec  (runtime 2 / durable 2)
1787856600  COMPLETE EXACT  183,625 dec  (runtime 3 / durable 3)
1787856900  COMPLETE EXACT  113,603 dec  (runtime 4 / durable 4)
```

4 rows, 4 joined-qualifying, 4 starts, 4 finished, 0 open, 0 duplicate starts, 0 duplicate
terminals, 4 identity-valid latency artifacts, 0 refused. 1,019,806 side opportunities classified
and actioned exhaustively; both feeds warm at T0 on every handoff; 0 drops, gaps and sink errors;
PLACE only under SAFE (2,125); four settlements RESOLVED. Merged live latency: CLOB receive→decide
p50 125,610 ns / p99 728,844 ns over 50,023 samples; spot p50 85,008 ns / p99 246,908 ns over 990.

No market-facing code changed, so the existing P8C regressions stand and no new benchmark was run.

---

## P13D: final evidence binding

Full evidence in [`evidence/P13D-EVIDENCE-BINDING.md`](evidence/P13D-EVIDENCE-BINDING.md).
`p13-corpus-3` is stopped, preserved and **SUPERSEDED_FOR_FINAL_P13_ACCEPTANCE** — 12 COMPLETE
replay-exact rows from 14 attempts, the two in flight left open in the ledger, which is what a
ledger is for.

**A row could count before its terminal record was durable.** `_finalize` appended the row,
incremented the total, and then called `ledger.finish(...)` in a `finally` block, discarding the
`False` that means the terminal event never reached the disk. A market the collector cannot prove
it finished is not an accounting rounding error. Both must now be durable before a market counts,
and a ledger write failure is a collector-integrity fault: the slot is released so nothing
deadlocks, and an ACCEPTANCE_CLEAN run stops launching. `recover()` had the same shape and now
leaves an unclosable attempt open rather than reporting it recovered.

**One definition of "counts".** `maker5m.bot.qualify` holds it and the collector, the resume
arithmetic and the final report all call it — corpus row, exactly one start, exactly one terminal
and that terminal `ATTEMPT_FINISHED` with `corpus_appended`, identity agreeing across all three,
and a valid latency artifact. 150 rows with 148 finished, one failed and one open count **148**.
Duplicate terminals are indexed as a list, because last-one-wins would hide a FINISHED sitting
beside an ABORTED.

**A hash-valid artifact is not necessarily this market's artifact.** The hash proved the bytes;
nothing proved which market they were written for. `validate_latency_identity` requires exact
equality on slug, market id, revision, tree, config, epoch, run mode, condition id, T0 and
sample_every, with missing never counting as equal. The session reads its own artifact back off
the disk before eligibility, and the report validates identity before merging a single sample.

### Revalidation — no new pilot needed

Nothing changed in feeds, capture timing, strategy, execution, risk, P8 timestamps, the latency
formulas or P11. The P13C pilot re-read under the stricter rules: **4 rows, 4 qualifying, 4
start/finish pairs, 0 open, 0 duplicates, 4 latency artifacts identity-valid, 0 refused**, with
merged live latency unchanged — CLOB receive→decide p50 112,618 ns / p99 862,348 ns over 63,478
samples; spot p50 86,288 ns / p99 606,447 ns over 2,101. The controlled restart re-read as 2
attempts, 1 finished, 1 aborted, 1 qualifying row.

---

## P13C: final corpus foundation

Full evidence in
[`evidence/P13C-FINAL-CORPUS-FOUNDATION.md`](evidence/P13C-FINAL-CORPUS-FOUNDATION.md).
`p13-corpus-2` is stopped, preserved and **SUPERSEDED_FOR_FINAL_P13_ACCEPTANCE** — ten COMPLETE
replay-exact rows, two attempts in flight with no row at all, which is one of the reasons it
cannot count.

**Live latency died with the market.** P8's own distributions were released with the session and
the corpus kept only `hot_path_observe_ns`, which mixes triggering kinds. A replay cannot stand in
for it: re-deriving decisions from a journal measures this machine today, not the market that was
traded. Each market now writes an immutable `<slug>.latency.json.xz` holding every raw sample —
not a sketch, because quantiles do not merge — hash-bound in its corpus row, with
`receive_to_reconcile` split by trigger. CLOB and spot are never merged.

**An attempt did not exist until it finished.** `attempts.jsonl` records `ATTEMPT_STARTED` and
fsyncs it *before* the session launches; if it cannot be written the market is not launched.
Terminal records append beside it, and a start with no terminal event is found by the next
start-up, closed as `ABORTED_PREVIOUS_PROCESS`, inventoried and counted toward nothing.

**The cold cap could be exceeded.** A launch check a running market can walk past is not a bound.
Markets reserve a lifecycle slot before launch and hold it until their terminal record and corpus
row are written; the cap is six and nothing can exceed it.

**Readiness was "ready once".** A book ready at T0-29 that disconnected at T0-10 kept its lead for
ever. P6 now tracks current warm validity, snapshotted at the T0 boundary; a recovery measures its
lead from the recovery.

**Dirty rows could later qualify.** HEAD does not move when tracked files are edited, so
`--allow-dirty` rows carried the same revision as clean ones. Qualification now requires
`working_tree_clean`, `run_mode == ACCEPTANCE_CLEAN` and the tree hash.

**The settlement knobs were hashed and ignored** — `settle_timeout_s` and `settle_poll_s` now
control the runtime call they claim to.

### Corrected pilot — four consecutive real markets, one process

```text
1787845500  COMPLETE EXACT   95,076 dec  190,152 classified  feed ready at T0 28.93s  349 KB latency
1787845800  COMPLETE EXACT  189,026 dec  378,052 classified  feed ready at T0 29.01s  640 KB latency
1787846100  COMPLETE EXACT  203,867 dec  407,734 classified  feed ready at T0 28.71s  645 KB latency
1787846400  COMPLETE EXACT  167,600 dec  335,200 classified  feed ready at T0 28.76s  503 KB latency
```

0 drops, gaps and sink errors; PLACE only under SAFE; four settlements RESOLVED; lifecycle
high-water 2 of 6; every row joins one attempt. Merged exact live latency over the four: CLOB
receive→decide p50 112.6 µs / p99 862.3 µs (n 63,478), CLOB receive→reconcile p50 130.0 µs / p99
971.7 µs, spot receive→decide p50 86.3 µs / p99 606.4 µs (n 2,101), decide_duration p50 28.8 µs.
**No latency threshold is proposed**, and the P13B 426 ms outlier stands.

**Controlled restart on a real market** (`CONTROLLED_LOCAL_FAULT_ON_REAL_MARKET`): the collector
was killed three minutes into `btc-updown-5m-1787847300`. The restart found the abandoned attempt,
recorded `ABORTED_PREVIOUS_PROCESS`, inventoried its orphaned 432 MB store rather than deleting
it, counted it toward nothing, and collected the next market normally.

Overhead after the pilot: decide p50 **−304 ns (−1.14 %)**, full cycle **+1,073 ns (+1.84 %)**.
Every P8C limit met, none moved.

---

## P13B: corpus integrity corrections

Full evidence in [`evidence/P13B-CORPUS-INTEGRITY.md`](evidence/P13B-CORPUS-INTEGRITY.md).
**P13 pilot v1 is SUPERSEDED FOR THE FINAL GATE** and `p13-corpus-1` is stopped, preserved and
excluded from the acceptance count.

Four collection defects, each of which would have been baked into two hundred markets:

**L3 was not exhaustive.** The analyzer classified only cycles that acted or were latency-sampled,
so `btc-updown-5m-1787826000` recorded 30,734 side classifications where 143,740 decisions imply
287,480 — and acting cycles are exactly the ones where an order was being placed or replaced, so
the sample over-weighted the worst queue moments. Same classifier, new explicit mode:
`EVERY_DECISION` for P13, P8's `SAMPLED_OR_ACTING` still the default. **Latency sampling is
untouched.** On one stream at three sampler settings, classifications went 400/40/4 before and
400/400/400 after. The denominator is exact and checked: `classified == actions == 2 × decisions`.

**"Prearm ready" was discovery ready.** It recorded when `discover_market` returned; the feeds do
not open until T0-30, so the 74.9 s lead proved metadata had resolved and nothing about a book or
a BTC price. P6 now records first CLOB message, book ready and first valid spot, observationally.
A market is warm when it has both, and `feed_ready_ns` is `None` when either is missing.

**The cold backlog was not bounded.** The constant and the helper existed; the launch loop called
neither. It now waits for capacity before a market exists — never during one — and skips the slot
with a `COLD_BACKLOG_CAP` row if capacity does not appear.

**A market could count before its row was durable.** Completion now requires the append to have
succeeded, and the target counts durable qualifying rows for the epoch, config hash and source
revision — a restart after 150 collects 50 more. A torn tail is closed off rather than welded to
the next row, and the collector refuses to start an epoch with modified tracked source.

Two more the corrected pilots found themselves. The launch loop was **holding every session it
had launched** (four markets, four live sessions), and full garbage collections — 37 of them
costing 17.1 s, worst case 1.46 s — were landing on the ingress owner, producing `observe` calls
of up to 762 ms against a 25 µs median. Sessions are now released by reference counting alone and
full collections are forty times rarer; `observe` maxima across the last three acceptance-pilot
markets were 15.6, 25.8 and 26.0 ms, with one 426 ms outlier on the first. Zero drops throughout.

### Acceptance pilot — four consecutive real markets, one process

```text
1787839200  COMPLETE  EXACT  235,924 decisions  471,848/471,848 classified  feed ready 29.13s
1787839500  COMPLETE  EXACT  219,310 decisions  438,620/438,620 classified  feed ready 29.12s
1787839800  COMPLETE  EXACT  199,752 decisions  399,504/399,504 classified  feed ready 28.90s
1787840100  COMPLETE  EXACT  159,786 decisions  319,572/319,572 classified  feed ready 29.02s
```

0 drops, gaps and sink errors; PLACE only ever under SAFE; four settlements RESOLVED; cold backlog
high-water 1 of 3; live sessions falling to 1 as the run drained. Exhaustive L3 over 1,629,544
side opportunities: AT_FRONT 34.59 %, PRICE_OK_BUT_DEEP 13.76 %, NOT_QUOTING 51.65 %, OFF_PRICE
0 %, STALE 0.00 % (26 cycles). **These do not share a denominator with the superseded fractions.**

Overhead after the pilot: decide p50 **−376 ns (−1.48 %)**, full cycle **+736 ns (+1.32 %)**.
Every P8C limit met, none moved.

---

## P13: live paper — composition root and pilot (v1, SUPERSEDED for the final gate)

Full evidence in [`evidence/P13-PILOT.md`](evidence/P13-PILOT.md). This is the **implementation
and pilot** record. The ≥200-market corpus is a separate gate and is **IN PROGRESS**.

`maker5m.bot` is now the composition root: config, session, supervisor, cold path, settlement
watch, corpus index, L3 aggregation, resource sampling and an entry point with no flag that could
send an order. Nothing re-implements a plane — P6 through P12 are composed.

**Two markets run at once, on purpose.** A capture opens its feeds at T0-30 and ends at T0+305,
five seconds past the next market's T0, so sessions overlap and are launched seventy-five seconds
ahead while the previous one still trades. Nothing is "the current market": every object belongs
to one slug, and operator commands follow one designated active market flipped at the handoff by
the market's own clock.

**Cold work runs in a child interpreter.** Decode 10.8 s, replay 3.2 s, encode 4.7 s, lzma 45 s —
a thread would hold the GIL through all of it, stealing from the one consuming a live book.
Spawned rather than forked, because forking a process holding an open SQLite connection and two
live websockets copies exactly what a child must not inherit. Discovery runs in a thread for the
same reason at a smaller scale.

### Pilot — three consecutive real markets, one process, no restart

```text
btc-updown-5m-1787826000   COMPLETE  replay EXACT  143,740 decisions  PLACE SAFE 548
btc-updown-5m-1787826300   COMPLETE  replay EXACT  154,465 decisions  PLACE SAFE 527
btc-updown-5m-1787826600   COMPLETE  replay EXACT  205,851 decisions  PLACE SAFE 748
```

Prearm lead 74.9 s on every handoff; 0 drops, 0 gaps, 0 sink errors across all three; three
settlements RESOLVED DOWN while later markets traded; archives 54.9× and verified. **PLACE while
HALTED: 0. PLACE while RECOVERING: 0.** Phases fired at +3.001 s, +240.001 s, +280.001 s and
+300.000 s — no threshold was changed, and the composition obeys them.

L3 over the three: AT_FRONT 33.3 %, PRICE_OK_BUT_DEEP 12.6 %, NOT_QUOTING 54.1 %, OFF_PRICE 0 %,
STALE 0 %. Per-market AT_FRONT ranges 25.7–36.8 %, so the classifier is non-degenerate and
market-sensitive. Queue-ahead p50 0, p95 166 M — **`SHADOW_ESTIMATE`**, never a venue queue
position.

The pilot found one real defect: the recorded event stream was held through the settlement watch,
so RSS reached 1.25 GB with three markets in flight. It is now released as soon as the journal is
on disk. Whether the plateau holds is a corpus-run question and will be answered from its trace.

Overhead against the accepted P11 stack: decide p50 **+249 ns (+1.06 %)**, full cycle **+1,926 ns
(+3.75 %)**. Every P8C limit met, no limit moved.

**No OPEN item closed. No strategy value changed.** One frozen configuration, hashed into every
entry. Real own-fill economics remains UNRUN / P14.

---

## P12E: commit boundary closure

Full evidence in [`evidence/P12E-COMMIT-BOUNDARY.md`](evidence/P12E-COMMIT-BOUNDARY.md). No new
live market: the runtime decision path is byte-identical to the reviewed P12D boundary.

**An INSERT is not a record.** The store batches on purpose — `BEGIN`, up to five hundred
inserts, `COMMIT` — so a statement the transaction accepted is not yet in the file. P12D closed
the statement-level hole and left this one: rows were counted into the manifest and announced to
the operator while the `COMMIT` that would make them durable had not happened and might not. A
failing commit was counted as a batch and forgotten, with only a `sink_error` quietly disagreeing.

Two states now, named apart. **Accepted into the open transaction** is
`rows_accepted_into_transaction`. **Committed** is `decisions_written`, `risk_written`,
`control_records_written` and `fills_written` — the counters the manifest reports. The worker
stages a row when the transaction takes it and promotes it when the transaction commits, and
`on_decision_record`, `on_risk_record` and `on_control_record` all now mean one thing: this row is
in a transaction that committed. Batching remains, at 500.

The batch check moved from the end of `_execute` to the start of each writer, so a row and its
storage envelope always share a transaction and a commit never lands inside a row's own write.
That was not theoretical: with the check at the end, the last row of a market fell into the batch
its own write had just committed, and the 40,000-row concurrency test caught it at 39,999.

**Commit failure fails closed.** Error counted, rollback attempted, staged rows dropped
unannounced, fresh transaction opened, consumption continues. The storage sequences are not
reissued, so the hole stands and the market cannot verify COMPLETE — a gap the verifier finds
beats a row an operator was told about that is not in the file. `close()` reports whether the
final commit succeeded, so a partial tail cannot vanish for never having filled a batch.

Proved by failing the `COMMIT` rather than the `INSERT`: a wrapper around a real sqlite3
connection that raises on commit and passes everything else through. Six tests, all failing
against P12D. In the operator-command case `ControlIngress` still halts the bot — trading safety
does not depend on telemetry — while nothing is counted, announced, or shown as durable, and audit
completeness reads false.

`btc-updown-5m-1787811600` re-read through the verified archive: COMPLETE, 124,272 decisions,
124,274 risk records, 2 control rows, 0 drops/gaps/sink errors, PLACE SAFE 646 / HALTED 0 /
RECOVERING 0, RESOLVED UP at block 92,737,974 with payouts [1,0] — identical to the P12D read.
The P12D latency contract re-checked on 4,883 real cycles, unchanged. Plane-3 work now arrives in
bursts of a batch, so the overhead benchmark was re-run: decide p50 **−4 ns**, full cycle
**+1,843 ns (+3.50 %)** healthy, inside a 1.6 µs run-to-run spread. Every P8C limit met.

---

## P12D: final contract closure

Full evidence in
[`evidence/P12D-FINAL-CONTRACT-CLOSURE.md`](evidence/P12D-FINAL-CONTRACT-CLOSURE.md). Four narrow
contracts the independent review of P12C left open. No new live market: nothing here touches the
trading path, and `hotpath.py`, `tools/p12_market.py`, `feeds/`, `strategy/`, `market/`,
`execution/` and `telemetry/` are byte-identical to the reviewed boundary.

**`decide_ns` meant receive-to-decide.** P8's `decide_duration` is `decide_stage - reduce_stage`;
the UI published `decide_done - raw_receive`, which is ingress, reduction, dispatch and the
strategy call together. That market's own analyzer puts decide at p50 50,956 ns and
receive-to-decide at p50 200,416 ns, and the snapshot published 268,302 ns. One metric, one
meaning: `decide_ns` is now P8's, and receive-to-decide is published under its own name rather
than dropped. 4,883 stage-sampled cycles from a real capture agree with `TelemetryAnalyzer` figure
by figure. A cycle timed end to end without stage stamps keeps what it measured and has no
`decide_ns` — absent, never zero.

**A callback did not prove persistence.** `TelemetryStore` absorbs SQLite errors by design, so
`write_control_audit(row); written += 1; on_control_record(row)` counted a refused row as written
and published it to the operator. The append-only writers now report whether the write path took
the row — envelope and row both — and counters and Plane-3 callbacks are gated on that. This
immediately found a test that opened the store on one thread and drained from another: every
write raised, and 20,480 of 40,000 "written" decisions were counted while never reaching the file.

**The audit columns were not checked against the payload.** A row whose column said RELEASE while
its payload said HALT cross-linked perfectly and answered differently depending on who asked.
Both representations are now compared, the V1 control-schema domain is explicit, and every type
is exact — `bool` is an `int` in Python and is not a schema version.

**The browser's clock ordered commands.** Pending files were named `{issued_at_ns}-{command_id}`
and read sorted, so a UI wall clock decided which command reached ingress first. The inbox now
allocates its own transport sequence, and a restarted UI continues above what is already pending.
Transport order is not causality: `ingress_ordinal` and `risk_sequence` still decide that, and
the transport sequence enters neither the command nor the audit row.

`btc-updown-5m-1787811600` re-read through the verified archive by the corrected code: COMPLETE,
124,272 decisions, 124,274 risk records, 2 control rows, 0 drops/gaps/sink errors, PLACE SAFE 646
/ HALTED 0 / RECOVERING 0, RESOLVED UP at block 92,737,974 with payouts [1,0] — identical to the
P12C read in every field. All eight accepted stores give the same verdicts and the same failure
text as before.

---

## P12C: snapshot coherence closure (superseded for the metric and transport contracts)

Full evidence in [`evidence/P12C-SNAPSHOT-COHERENCE.md`](evidence/P12C-SNAPSHOT-COHERENCE.md).
P12B's market is **retained and valid**; P12B's *architecture* and *final snapshot* are
superseded.

**Stdout is I/O.** P12B took the filesystem out of `on_tick` and left five `print(..., flush=True)`
calls on it. The rule is no synchronous I/O on the single ingress consumer — a write to a pipe
nobody is draining blocks as thoroughly as a stalled `stat`. The hot side now appends an
immutable fact to a bounded channel and the run log is rendered on the main thread after close.
Not a different logger: `logging` would be the same synchronous write behind a lock.

The P12B test that claimed to prove this reproduced the tick body *in the test file* while the
shipped one printed. Now one extracted production function is tested with `print`, `logging` and
thirteen filesystem entry points replaced by raisers, and a second test walks the AST of the
shipped `tools/p12_market.py`. It fails against the P12B file.

**The snapshot is joined to its own decision.** `controller.trace.records[-1]` was the newest
verdict, not the one the decision names, and `_latency_sample(run)` read the merger while Plane 1
was mutating it. The worker now hands over `(record, observation)`, latency comes from that
observation's P8 stamps, and the verdict is looked up by `risk_sequence` — an unarrived verdict
reads unavailable rather than borrowing a neighbour's.

**The final snapshot comes from the manifest.** P12B's last frame said 82,335 decisions / 82,337
risk / 1 drop / INCOMPLETE beside a manifest saying 82,336 / 82,338 / 0 / COMPLETE. A `closed`
message now carries the manifest's own figures and a straggling counter cannot walk them back.
Audit completeness compares acceptance against persistence instead of counting exceptions that
`BoundedChannel.publish` never raises, and command history comes from persisted audit rows.

**`None` and `False` are different audit facts.** The control cross-link compared truthiness and
proved only that two rows agreed — a halt written `flag=False` in both would have passed. Typed
comparisons now, and the command kind must imply the flag.

### Real market — `btc-updown-5m-1787811600`

124,272 decisions · 124,274 risk records · 2 control-audit rows · 248,549 storage entries exact
from 1 · 0 drops, gaps, sink errors · verification **COMPLETE** · archive 650.0 MB → 11.9 MB
(54.5×), restored and verified.

```text
halt     a52f1f38fada4dd0  ordinal 1358  risk_seq 1300  HALTED   place=False cancel=True
release  a3e7df18eaaf4cd1  ordinal 8241  risk_seq 8016  RECOVERING -> SAFE
kill     SIGKILL at ordinal 8430 -> +24,384 events, +23,820 decisions in 47s, risk SAFE
restart  same market, both commands shown from the durable audit rows
final    RESOLVED UP, block 92,737,974, payout [1,0], REDEMPTION DISABLED
snapshot 124,272 / 124,274 / 0 / 0 / complete / COMPLETE — six for six against the manifest
```

**PLACE while HALTED: 0. PLACE while RECOVERING: 0** — with 7,077 decisions under the halt.
Latency published: decide 268,302 ns · prepare 3,676 ns · reconcile 14,551 ns · receive-to-
reconcile 286,529 ns, sampled at ordinal 127,451.

Overhead against the accepted P11 stack: decide p50 **−11 ns (−0.05 %)**, full cycle **+1,477 ns
(+2.81 %)**; stalled bridge **−177 ns** and **+839 ns**. Within-mode spread is about 1.5 µs, so
these sit inside each other's noise. Every P8C limit met; no limit moved.

**A market this round cost.** The first P12C attempt, `btc-updown-5m-1787810700`, traded for the
full five minutes and then died formatting its own run log, after `worker.stop` and before the
manifest — a complete capture left with a database and no closure. Not presented as acceptance
evidence; its UI acceptance log is retained as superseded. The renderer is now tested and its
call site guarded.

---

## P12B: Plane-3 isolation closure (SUPERSEDED for architecture)

Full evidence in [`evidence/P12B-PLANE3-ISOLATION.md`](evidence/P12B-PLANE3-ISOLATION.md). The
first P12 market is **retained and superseded for architectural acceptance**. P12B itself is now
superseded by [P12C](evidence/P12C-SNAPSHOT-COHERENCE.md) for architecture and for its final
snapshot; **its store and its market remain valid** — `btc-updown-5m-1787807700` verifies
COMPLETE, and the P12C revalidation rebuilds its final frame from those same bytes.

**"Does not wait for the UI process" is not "cannot block the trading loop."** P12's first
version polled the command inbox and wrote the snapshot from inside `on_tick` — the single
ingress consumer. Nothing there waited on the UI and nothing needed to: a `listdir`, a `stat`, a
`read_text` or a `rename` can stall on the *filesystem* with no UI involved. I19 is about
latency, so wrapping the calls would have changed nothing.

```
BEFORE   on_tick -> glob, stat, read_text, unlink, mkstemp, write, rename
AFTER    on_tick -> deque.popleft()
```

`CommandBridge` owns a thread and is the only thing in P12 that touches a file. Overflow does not
drop — telemetry may lose a record, a safety command may not — so a full hot channel refuses the
push and the bridge leaves the file on disk. `SnapshotPublisher` is single-owner again;
everything crosses into it as an immutable message through one bounded inbox.

Three more, each a place the dashboard was telling an operator something nobody measured:

* **risk** — `risk_active` and `risk_latched` were hardcoded empty; health was inferred from
  whether data existed, so a STALE spot feed with a recent price read HEALTHY. All of it now
  comes from the `RiskRecord` the decision *names*, found by sequence rather than by being
  newest, with an `observation_points` map where the parts genuinely differ.
* **latency** — four fields defined and always `None`. Now P8's own timings with the ordinal they
  were sampled at; an unsampled cycle reads "not sampled", never `0 ns`.
* **settlement** — fields existed, runner never filled them. Now published after P10 resolves.

**Command identity is durably linked.** A `RiskRow` says an `OPERATOR_CONTROL` signal happened,
not which command caused it, so the link lives in a `control_audit` table with `command_id` as a
column — not parsed out of a detail string. Store V3; reading accepts {2, 3} so every accepted
P11 archive stays readable. The verifier cross-links both directions and refuses an orphan either
way. Idempotency moved from the transport to `ControlIngress`, which is the only thing that
decides whether a command changes the risk state.

### Real market — `btc-updown-5m-1787807700`

82,336 decisions · 82,338 risk records · 2 control-audit rows · bridge 4,728 polls / 1,012
snapshots / 0 errors · hot channel high-water **1** of 32 · bridge deliberately stalled +200→+240 s
· 0 drops, gaps, sink errors · verification **COMPLETE**.

```text
halt     20976c37536b498c  ordinal 988   risk_seq 964   HALTED   place=False cancel=True
release  d8fa82dbcedd4a0f  ordinal 5001  risk_seq 4878  RECOVERING -> SAFE
kill     SIGKILL at ordinal 5008 -> +13,728 events, +13,440 decisions in 47s, risk SAFE
restart  same market, both commands shown, no new command
final    RESOLVED DOWN, block 92,735,374, payout [0,1], REDEMPTION DISABLED
```

**PLACE while HALTED: 0. PLACE while RECOVERING: 0** — with 4,109 decisions under the halt.

Overhead against the accepted P11 stack: decide p50 **+300 ns (+1.28 %)**, full cycle **+768 ns
(+1.46 %)**; semantics identical across modes. A **stalled** bridge measures **−21 ns** against
P11-only — indistinguishable from having no UI at all, which is the claim.

---

## P12: operator UI and control plane (SUPERSEDED for architecture)

Full evidence in [`evidence/P12-UI-CONTROL-PLANE.md`](evidence/P12-UI-CONTROL-PLANE.md).

Two processes and a directory between them. The bot writes a snapshot by atomic rename; the UI
writes commands into a bounded inbox; the bot lists that directory on its control tick and never
waits for anyone. Files rather than a socket, a queue or a broker, because the acceptance gate is
killing the UI mid-market — which rules out any transport where the bot holds something the UI can
be holding when it dies, and rules out a broker, which would be a third process to keep alive in
order to prove that a process dying is survivable.

`UiSnapshot` is immutable and holds no reference to any trading object. It is built on the
persistence worker's thread from the `DecisionRecord` P11 already made, four frames a second, so
**Plane 1 pays nothing**. Nothing in the UI recomputes an economic quantity — the licence is
scale, and a second PnL implementation in a dashboard would be a second thing to be wrong in the
place people look for the truth. Absence renders `—`, a missing snapshot renders NO SNAPSHOT, a
stale one says so.

Control is two commands — `OPERATOR_HALT` and `RELEASE_OPERATOR_HALT` — and nothing else exists.
A command is inert until the bot accepts it into the ordered risk stream, ordering comes from the
ingress ordinal rather than the browser's clock, and the release clears **only** the operator's
own condition. Loopback only (no authentication exists; recorded as OPERATIONAL), GET has no
write path, POST answers 303 so a refresh cannot repeat a command, and **no endpoint can change
`LIVE_TRADING_ENABLED` or `REDEMPTION_ENABLED`**.

### Real market — `btc-updown-5m-1787803500`

66,174 decisions, 66,176 risk records (the two extra are the operator's commands), 0 drops, 0
gaps, 0 sink errors, verification **COMPLETE**.

```text
halt     command 4b2a5f04ed7642c4  ordinal 1835  risk_seq 1771  HALTED   place=False cancel=True
release  command 651f8ae4bbf644b9  ordinal 8131  risk_seq 7924  RECOVERING -> SAFE
kill     SIGKILL at ordinal 8229 -> +18,113 events, +17,631 decisions in 47s, risk SAFE
restart  same market, rendered from the snapshot, both commands still shown, no new command
```

**PLACE while HALTED: 0. PLACE while RECOVERING: 0** — with 6,209 decisions taken under the halt.

### The defect the first real market found

The first attempt verified **INCOMPLETE**, and the two records missing from the durable risk
stream were the operator's own two commands: `ControlIngress` applied each to the controller and
never published the result for persistence. The drop accounting was self-consistent throughout, so
only P11's sequence-exactness check noticed. A control action with no durable record is the worst
thing to lose, and it was the only thing lost. Fixed; the market above is the re-run.

---

## P11F: supported schema domain closure

Full evidence in
[`evidence/P11F-SUPPORTED-SCHEMA-DOMAIN.md`](evidence/P11F-SUPPORTED-SCHEMA-DOMAIN.md).
**Read-side only** — verifier, one added constant, and tests — so the real evidence was
re-verified rather than re-gathered.

P11E proved the column and payload said the *same* thing about the schema version. It never asked
whether the thing they said named a contract. `effective < DECISION_SCHEMA_VERSION` was the
definition of historical compatibility, so a record stamped `0` or `-1` in both places collected
V1's exemption from every V2 rule, and one stamped `3` was validated as though it were current.
And `isinstance(v, int)` accepted `True`, because `bool` subclasses `int` and `True == 1`.

`SUPPORTED_DECISION_SCHEMA_VERSIONS = {1, 2}` is now enumerated, the V1 exemption matches `1`
exactly, and the distinction is documented:

| Case | Result |
|---|---|
| exact ints, equal, known version | read under that contract |
| exact ints, equal, unknown (`0`, `-1`, `3`) | **UNSUPPORTED** — a record we cannot read |
| not an exact int, or disagreeing | **INCOMPLETE** — a record contradicting itself |

`UNSUPPORTED` outranks `INCOMPLETE`: "this build cannot read these" is a more fundamental answer
than "some are missing". Types are exact before comparison for the version and every duplicated
identity field, since `1 == True` and `1 == 1.0` would otherwise let a record agree with its
column while saying something else.

**`btc-updown-5m-1787780700` re-verified COMPLETE** — 114,287 decisions, every count 0, 678
PLACEs all under a persisted SAFE RiskRow, 0 HALTED, 0 RECOVERING. **`btc-updown-5m-1787771100`
re-verified INCOMPLETE** — schema and identity checks all pass; it fails only for the genuine
persistence loss.

---

## P11E: durable schema contract closure

Full evidence in [`evidence/P11E-SCHEMA-CONTRACT.md`](evidence/P11E-SCHEMA-CONTRACT.md).
**Read-side only** — the diff against P11D is the verifier and its tests, so the existing real
evidence was re-verified rather than re-gathered and no market was spent.

Two holes, both absence read as agreement:

* the column/payload comparison was guarded by `payload is not None`, so a payload that had
  **lost** its copy of an indexed field was exempted rather than caught. Every decision has an
  indexed `market_id`, `ingress_ordinal`, `capture_sequence`, `event_id` and `schema_version`; a
  payload missing one is a damaged record, not a nullable one.
* the effective schema version came from the column alone and was never compared with the
  payload's — a **downgrade bypass**. A row whose column said V1 while its payload said V2
  collected V1's exemption from every V2 rule, which is precisely the set P11D had just made
  load-bearing.

Both representations must now be present and equal before any version rule is applied, and the
V1 exemption requires a *proven* V1: a row whose versions disagree has proved nothing and is held
to the current contract, so the downgrade is worth nothing. A genuine V1 row — both saying V1 —
keeps its historical meaning.

**This checks internal consistency, not authenticity.** It is not a defence against rewriting the
database, sidecar and hashes together; the verified archive SHA remains the artifact-identity
layer.

**`btc-updown-5m-1787780700` re-verified COMPLETE** — 114,287 decisions, every risk/identity/
schema count 0, 678 PLACEs all under a persisted SAFE RiskRow, 0 HALTED, 0 RECOVERING.
**`btc-updown-5m-1787771100` re-verified INCOMPLETE** — its schema and identity checks both pass;
it fails only for the genuine persistence loss the controlled stall caused.

---

## P11D: reference completeness closure

Full evidence in
[`evidence/P11D-REFERENCE-COMPLETENESS.md`](evidence/P11D-REFERENCE-COMPLETENESS.md).
**Read-side only** — the diff against P11C is the verifier, its tests, and the query tool, so the
existing real evidence was re-verified rather than re-gathered and no market was spent.

The verifier skipped any decision whose `risk_sequence` was `None`, so a V2 record that declined
to name its governing verdict evaded the RiskRow join, all three copy comparisons **and** the
PLACE contract. The check that existed to stop a decision misrepresenting its verdict did not
apply to one that named no verdict at all — the worse of the two facts.

Now, for every V2 decision in a COMPLETE market: the reference and all three copied fields must
be present, the sequence must resolve to a stored `RiskRow`, and each copy must *equal* it. The
`is not None` guard on the comparison is gone — **`None` and `False` are different audit facts**
and neither is a pass. `event_id` must be non-empty, and the indexed columns must agree with the
payload they duplicate.

**P11C baseline `btc-updown-5m-1787780700` re-verified COMPLETE** through the verified archive
path: 114,287 decisions, `decisions_missing_risk_reference` 0, `incomplete_risk_copy` 0,
`absent_risk_row` 0, `copy_mismatches` 0, `no_event_id` 0, **678 PLACEs all under a persisted
SAFE RiskRow, 0 HALTED, 0 RECOVERING**.

**P11B stalled market re-verified INCOMPLETE**, now with one failure more: PLACEs naming risk
rows that were dropped. Read precisely — those PLACEs did not happen under a halt; the live run
was SAFE throughout. What was lost is the record of the verdicts. The verifier refuses because
the audit cannot produce the permission, which is the correct reading of an incomplete audit.

`p11_query` reports missing references separately from dangling ones and exposes
`verification_status` beside `evidence_eligible`: identity-verified and telemetry-complete are
different questions, and an archive can provably be the market it claims to be while missing half
of it.

---

## P11C: final audit closure

Full evidence in
[`evidence/P11C-FINAL-AUDIT-CLOSURE.md`](evidence/P11C-FINAL-AUDIT-CLOSURE.md).

Four narrow items, each a place where the audit trusted something it had not checked:

| | Was | Is |
|---|---|---|
| **A** | PLACE permission read from the decision's own copy of the verdict | joined to the persisted `RiskRow`; copies compared both ways |
| **B** | benchmark read `pipeline.clob_health`, which a replay never updates | replays the journal's own recorded `HealthEvent`s |
| **C** | `INSERT OR REPLACE` on audit tables | append-only; a duplicate is a sink error and the original stands |
| **D** | any `.xz` decompressed and queried | identity proved before anything is answered from it |

**A was circular.** A decision that misrepresented the verdict it ran under would have been
checked against its own misrepresentation. **B measured a market that never quoted** — health was
permanently UNKNOWN, so P9 halted and `risk_adjust` emptied every intent; the journal's own 2,206
recorded `HealthEvent`s fixed it without adding a second staleness authority.

`decisions`, `fills`, `risk_records`, `settlements` and `persistence_log` are **APPEND-ONLY**;
`markets` and `market_metrics` remain FINAL/METADATA and are documented as such.

### Fresh real market — `btc-updown-5m-1787780700`

114,287 decisions, 114,288 risk records, 1 settlement (DOWN), zero drops/gaps/sink errors,
`telemetry_complete=true`, **all 27 verifier checks pass**, archive 54.8× and the verified
read-back re-verified COMPLETE. Audited through the verified archive path:

```text
decision_risk_copy_mismatches   0      places_by_risk_state (from RiskRow)  {"SAFE": 678}
decisions_with_no_event_id      0      risk sequence 0…114,287 exact
storage order 1…228,576 exact          0 duplicates
```

**PLACE under a persisted RiskRow that was HALTED: 0. RECOVERING: 0.**

Performance, semantics identical across OFF/healthy/stalled and all four pairs: decide p50
**+676 ns (+2.89 %)**, full cycle **+1,519 ns (+2.98 %)**. Every P8C limit met with its original
number — and stated plainly: decide sits at 2.89 % against a 3 % limit, which is met and not
comfortable. The replay saturates the writer at far above production's ~380 decisions/second, so
it bounds the overhead above.

P11B's controlled-stall market is **retained, not rerun** — the producer/consumer hot path did not
change — and was re-verified under the new verifier to check that rather than assume it.

---

## P11B: persistence integrity closure

Full evidence in
[`evidence/P11B-PERSISTENCE-INTEGRITY.md`](evidence/P11B-PERSISTENCE-INTEGRITY.md). The first P11
real-market evidence below is **SUPERSEDED for acceptance** and retained, not rewritten.

Independent review accepted the architecture and found five closure gaps:

| | Was | Is |
|---|---|---|
| **A** | risk evaluated against `HealthFrame()` at ordinal 0, unadjusted decision executed | P6's own health, real ordinal, `risk_adjust` before prepare |
| **B** | fills counted and discarded | `FillCapture` published non-blocking, durably stored |
| **C** | a risk run starting at 5,000 verified as contiguous | exact from zero, plus drop accounting |
| **D** | `event_id` built from slug + counter | P2's real `EventMeta.event_id` |
| **E** | 853 MB/market, deferred | lossless `lzma` archive, measured |

**Gap A mattered most.** `HealthFrame()`'s defaults say CLOB UNKNOWN, awaiting snapshot, SPOT
UNKNOWN — P9 correctly refuses to trade on that. So the recorded verdict described a market the
runner had not looked at, at an ordinal that no longer existed, and did not govern the recorded
action. It now follows P9's own accepted pattern, and raw strategy intent is kept beside the
post-risk intent so a record can tell "the strategy declined to quote" from "the strategy wanted
to quote and safety refused". `DecisionRecord` is **V2**, bumped rather than reinterpreted.

### Footprint, measured not deferred

| | Raw | Archived (`lzma`) |
|---|---:|---:|
| One market | 600,547,328 B | **10,988,132 B** (54.7×) |
| Per decision | 5,230 B | **95.7 B** |
| 200-market P13 corpus | 120.1 GB | **2.20 GB** |
| 24 h continuous | 173.0 GB | **3.16 GB** |

Lossless — no sampling, no field removal, no rounding. The archive is decompressed and hashed
before it is called verified, and no raw store is deleted without that check passing.

### Real markets

* **`btc-updown-5m-1787770200`** — 114,823 decisions, 114,823 risk records persisted
  *continuously*, 1 settlement (UP). Zero drops, gaps, sink errors. Storage order exact
  1…229,647; risk sequence exact 0…114,822; every event id real. All 25 verifier checks pass.
  Read back **from the compressed archive**: 532 PLACEs, all under SAFE. **PLACE while HALTED: 0.
  PLACE while RECOVERING: 0.**
* **`btc-updown-5m-1787771100`** — controlled local stall. 48,296 market events during 90 s,
  decision drops exact at 46,718 over 4 gaps, **risk drops explicit at 47,234 of 112,156**,
  `telemetry_complete=false`, verification INCOMPLETE for six reasons including 516 decisions
  naming a risk record that is gone. Trading and risk untouched: PLACE only ever under SAFE.

Performance, with P9 in every configuration so only the persistence delta is charged to P11:
decide p50 **+328 ns (+1.43 %)**, full cycle **+309 ns (+0.76 %)**; stalled within noise of
healthy. Every P8C limit met with its original number.

---

## P11: durable telemetry persistence (SUPERSEDED for acceptance)

Full evidence in
[`evidence/P11-TELEMETRY-PERSISTENCE.md`](evidence/P11-TELEMETRY-PERSISTENCE.md).

Two gates, kept apart:

| Gate | Status |
|---|---|
| Implementation | **PASSED** |
| Real-market persistence | **PASSED** — one healthy market, one controlled stalled sink |
| Real own-fill / maker fraction / nonzero own-ledger metrics | **UNRUN / DEFERRED TO P14** |

### The claim, measured

**Stalling Plane 3 does not slow Plane 1.** 12,000 real captured events, four alternated
triples, each configuration alone in a fresh interpreter — P8C's method unchanged:

| Metric | off p50 | healthy Δ | stalled Δ |
|---|---:|---:|---:|
| decide | 22,387 ns | **−154 ns (−0.69 %)** | **−352 ns (−1.57 %)** |
| full cycle | 36,802 ns | **−184 ns (−0.50 %)** | **−365 ns (−0.99 %)** |

Every P8C limit met with the original numbers; none was moved. It did not start there: the first
honest measurement was **+5.3 %**, because encoding one record cost 92 µs on a GIL-holding
thread. `asdict` recursion, a recursive JSON pre-pass and `sort_keys` came out; a one-level field
walk costs 26 µs.

### Real markets

* **`btc-updown-5m-1787748900`** — 171,467 cycles, **171,467 decisions persisted**, 173,460 risk
  records, 1 settlement (UP). **Zero drops, zero gaps, zero sink errors.** Buffer high-water
  **214 of 320,000** — continuous draining, directly measured. `telemetry_complete = true`,
  verification **COMPLETE** on all 15 checks.
* **`btc-updown-5m-1787749500`** — `CONTROLLED_LOCAL_FAULT_ON_REAL_MARKET`. Our consumer stopped
  for 90 s while the feeds stayed real and healthy. **64,416 market events processed during the
  stall**, drops exact at **62,428**, one gap recorded with both ends, sink resumed without
  error, market still settled. `telemetry_complete = false`, verification **INCOMPLETE**.
  Telemetry was lost; nothing else was.

### An operational finding, recorded rather than buried

The store is ~**5 KB per decision** — about 850 MB per five-minute market, ~10 GB per trading
hour. That is impractical to keep indefinitely. The payload column duplicates the indexed
columns and could be narrowed; nothing was truncated to make the figure look better.

### Three defects the real market found that the unit tests could not

The connection was opened on the wrong thread (sqlite refused every write while the tests passed,
because they drained on the main thread); a bare `except: sink_errors += 1` hid 1,789 failures
per run whose cause turned out to be `RawCentre` being a rational rather than a price; and the
closing writes repeated the first defect in the tool, so the first complete-looking market
verified INCOMPLETE — caught by the verifier, which is what it is for.

---

## P10C attestation-binding closure

Independent review accepted the P10B boundary below and found one narrow defect left in it: **an
attestation was not bound to the provider or endpoint that produced the reading.** P10B is not
withdrawn — it proved providers were distinct and each endpoint passed identity. What it did not
prove is that a given identity check described the endpoint whose reading it was attached to.

`ProviderAttestation.valid` checked chain id, CTF bytecode, pUSD bytecode and pUSD decimals —
all correct, all *internal*. **The proof supplied both sides of its own comparison**, so a valid
attestation for endpoint A made a reading from endpoint B count.

The chain is now fastened at every link:

```
identify()          records the endpoint it ACTUALLY identified
to_attestation()    takes NO endpoint — no argument left to re-point
AttestedProvider    refuses to exist unless identity names its endpoint  -> AttestationBindingError
CtfReader           refuses a foreign proof BEFORE any request; stamps its OWN fingerprint
ProviderResolution  records source_endpoint_fingerprint independently of the proof
verify()            re-checks the binding on readings it never created  -> ATTESTATION_BINDING_MISMATCH
```

`ATTESTATION_BINDING_MISMATCH` is deliberately distinct from `PROVIDER_NOT_ATTESTED`: a missing
proof is usually a wiring mistake, a proof belonging to somebody else is evidence being moved
around, and an auditor should not have to guess which happened.

**A false claim, corrected.** The P10B docs said `AttestedProvider` was "the only way to obtain"
an attestation. It was not — these are ordinary Python values and a constructor is not a
cryptographic capability. The defensible claim, which is what is now tested: `attest_all` is the
production factory, every object validates its own binding, the verifier re-checks it, and a
hand-built mismatch fails closed at every layer. A caller can still construct nonsense; it will
not count.

### Revalidated

* **55-market corpus:** 55 RESOLVED, 27 UP, 28 DOWN, 0 mismatches — unchanged, and on the same
  footing: real historical market data, attestations contemporaneous with the *replay*.
* **7 fresh consecutive real markets** (`1787739600`–`1787741400`), newer than all P10B evidence.
  **444 real provider readings, 0 binding faults**; every counted provider matched exactly at
  endpoint, identity and attestation on both `provider_id` and fingerprint. `1rpc` rejected again
  on a real vendor usage limit.
* **Every P10B protection asserted as still standing** — finalized tag, concrete block, three
  distinct providers, three distinct endpoints, no duplicate vote — because this round changed
  the type those rules operate on.
* **O16 still latches** on real market `btc-updown-5m-1787740500`: fresh real readings resolved
  cleanly at sequence 2 and the halt held; only the explicit reconciliation lifted it.

Sixteen of the new tests fail against the P10B code. **GENUINE REAL SETTLEMENT CONTRADICTION:
UNOBSERVED** — now across 83 real markets.

---

## P10 trust-boundary closure

Independent review accepted the settlement logic and rejected the phase on three production
trust-boundary defects. All three had one shape: a rule the code described correctly in prose
and never enforced.

| Defect | Was | Is |
|---|---|---|
| **A** — provider independence | `("a", "a", "a")` satisfied a three-provider quorum; two ids could share one URL | `DUPLICATE_PROVIDER_ID` fails closed; `EndpointSet` refuses both at configuration time |
| **B** — identity attestation | `identify()` checked the right things and the runner built readers from every endpoint anyway | `ProviderAttestation` is carried; an unattested reading is refused with `PROVIDER_NOT_ATTESTED`; the runner excludes untrusted endpoints and refuses to start below quorum |
| **C** — moving-tag fallback | payout calls fell back to the moving tag exactly when the block lookup failed | no concrete block, no reading — proven by a scripted transport issuing **zero** `eth_call`s |

Also fixed: the finality rule came from `provider_readings[0].block_tag`, so whichever provider
was first defined what the audit claimed (`FINALITY_POLICY_MISMATCH` now); a RESOLVED verdict
could name no block (`MISSING_AUTHORITATIVE_BLOCK` now); and `confirmation_depth` was exposed and
did nothing (removed).

**Independence is asserted, not proved.** The fingerprint compares normalised URLs. It cannot
show two vendors are organisationally independent, and does not claim to — that stays an
OPERATIONAL assumption, recorded in `OPEN_ITEMS.md`.

### Revalidated

* **55-market P10A corpus:** 55 RESOLVED, 27 UP, 28 DOWN, 0 mismatches, every market on exactly
  three distinct attested providers. No historical data was modified and the boundary was not
  weakened to keep the count. The attestation is contemporaneous with the *replay*, not the
  capture, and the record says so; the corpus was captured at `latest`, and a `finalized` policy
  correctly refuses it.
* **7 fresh consecutive real markets** (`1787733300`–`1787735100`), newer than all prior P10
  evidence: every verdict on 3 distinct trusted providers and 3 distinct endpoint URLs, every
  block concrete and `finalized`, no duplicate vote, no moving-tag fallback, no false ambiguity.
  `1rpc` failed identity for real and contributed nothing.

### O16 — CLOSED, OPERATIONAL safety policy

`RESOLUTION_AMBIGUOUS` now latches. Proven on real market `btc-updown-5m-1787734200` under
`CONTROLLED_LOCAL_FAULT_ON_REAL_MARKET`:

```
seq 1  RESOLUTION_SAFETY_UPDATE   HALTED      latched=[RESOLUTION_AMBIGUOUS]  place=False
seq 2  RESOLUTION_SAFETY_UPDATE   RECOVERING  latched=[RESOLUTION_AMBIGUOUS]  place=False   <- fresh real read was RESOLVED
seq 3  RECONCILIATION_CONFIRMED   SAFE        latched=[]                      place=True
```

Sequence 2 is the point: fresh **real** readings resolved cleanly, the generic clearing signal
took the condition away, and the halt stayed up. **GENUINE REAL SETTLEMENT CONTRADICTION:
UNOBSERVED** — in 76 real markets the venue never contradicted itself; every ambiguity seen was
injected by us or was the verifier's own defect.

---

## P10: settlement against real markets

Full evidence in
[`evidence/P10-SETTLEMENT-REAL-MARKET.md`](evidence/P10-SETTLEMENT-REAL-MARKET.md).

Three gates, deliberately kept apart:

| Gate | Status |
|---|---|
| Implementation | **PASSED** |
| Real-market resolution | **PASSED** — 21 markets, 4 runs, 1 full lifecycle |
| Authenticated redemption | **UNRUN / DEFERRED TO P14** |

### The defect running it found

The verifier halted **6 of the first 15 live settlements** with `FINALITY_DISAGREEMENT`, every
time on a healthy market. Because a P9 `RESOLUTION_AMBIGUOUS` halt does not self-clear, that is
a bot which stops placing roughly every second or third market and stays stopped.

The first explanation was wrong, and is recorded rather than quietly replaced. The obvious
reading was finality-head skew — P10A had measured providers 1–4 blocks apart — so a fix was
written comparing block numbers. The recorded polls disproved it: the silent provider was at the
*same* block twice and *two blocks ahead* once. A provider cannot be behind a block it is ahead
of, and that fix would not have prevented a single halt.

Two real defects were underneath:

* **The reads were not atomic.** `read_condition` resolved `finalized` separately for the block
  number and for each `eth_call`, so at a load-balanced provider the block in the audit record
  was not the block the payout came from. Every call is now pinned to the block already
  resolved. Measured effect on real polls: splits where the silent provider was genuinely
  behind went from **0 of 3 to 3 of 3**.
* **The quorum counted the wrong thing.** `minimum_agreeing_providers` was applied to providers
  that merely *answered*, requiring unanimity by accident. It now counts providers positively
  agreeing on one payout vector; a provider reporting nothing leaves the state `UNRESOLVED`.
  Contradiction is checked first, so a real disagreement is never reported as "waiting".

The corrected run settled 6 of 6 consecutive markets with no ambiguity, no blocker, and no risk
signal emitted at all.

### Ambiguity reaches execution permission

Injected ambiguity — labelled `CONTROLLED_LOCAL_FAULT_ON_REAL_MARKET`, ours and never a venue
incident — produced `AMBIGUOUS`, **no** `RedeemPlan`, and an ordered `RESOLUTION_SAFETY_UPDATE`
in the P9 trace with `allows_place=False` and `allows_cancel=True`. Removing the corruption and
reading the real chain afresh returned `RESOLVED` while the halt stayed up, which is intended.

**Recorded, not fixed:** P9 does not latch `RESOLUTION_AMBIGUOUS`, so that stickiness rests on
`maker5m.settlement.safety` never emitting `flag=False` rather than on the risk engine's own
contract. Left as **O16** rather than settled by editing P9's latch set.

### What P10 does not show

Every position ledger settled is **empty**, because this bot has never placed an order. The
end-to-end reconciliation "matching to the last money unit" is an agreement between two zeros.
The arithmetic, the `redeemPositions` encoding (8 of 8 accepted by the real contract via
`eth_call`) and the authorisation gate all behave on real data; **no money moved, and none can
in this build**.

---

## P10A: O11 closed from real settlement evidence

**P10 itself is NOT implemented.** This round closed the prerequisite only — no `Redeemer`, no
`ResolutionVerifier`, no wallet, no key, no transaction. Evidence:
[`evidence/P10A-O11-RESOLUTION-RESEARCH.md`](evidence/P10A-O11-RESOLUTION-RESEARCH.md).

O11 asked which source authoritatively determines a settled market's outcome. The answer keeps
three things apart that are easy to collapse into one:

```text
AUTHORITATIVE FINAL   CTF payout vector on Polygon, 0x4D97DCd9...0476045
                      payoutDenominator > 0 gates redeemability
ADVISORY CROSS-CHECK  Gamma outcomePrices, CLOB tokens[].winner
RULE SOURCE           Chainlink BTC/USD TWAP 60 s, named by the markets themselves
PRE-ON-CHAIN          denominator == 0 is not yet authoritative, whatever a venue says
```

### The evidence

55 **consecutive** real settled markets over 4.5 hours, plus 6 **consecutive** settlements watched
live, plus four independent Polygon RPC providers. No synthetic data.

```text
agreement with final CTF payout   Gamma 55/55    CLOB 55/55    disagreements 0    missing 0
outcome-index mapping             slot 0 = Up (27)   slot 1 = Down (28)   unanimous
RPC agreement                     55/55, minimum 3 independent providers per market
payout shapes observed            [1,0] x27, [0,1] x28 - no fractional, no non-binary
on-chain resolution after end      p50 85 s (min 52, max 172) from block timestamps
live CTF availability after end    p50 +85.6 s (min 54.3, max 86.6)
live Gamma / CLOB availability     NOT observed within ~206 s in 6 of 6 markets
Polygon finality lag               1-4 blocks (1-6 s), measured across 3 providers
```

The chain is not only authoritative but **earliest**, by more than two minutes. The usual
speed-versus-correctness trade-off does not arise, and the strategy has already stopped quoting
and is holding balances by then (Canonical §18) so there is nothing to gain by guessing.

### The finding that most needed checking

These markets do **not** use the UMA adapter. Reading `ConditionResolution` for all 55 gives
oracle `0x58e1745bedda7312c4cddb72618923da1b90efde`, against the officially documented UMA
Adapter `0x6A9D2226…` — a different address. It is a real deployed contract whose `ctf()` returns
the official Conditional Tokens address, so it is a purpose-built adapter for this market family.
Its official **name could not be verified** and is recorded as unverified rather than guessed.
Gamma still reports `umaResolutionStatus: "resolved"`, which is a field name and not evidence of
the UMA path.

### Stated honestly rather than assumed

* **Chainlink raw recomputation: UNAVAILABLE / UNRUN.** Data Streams is a credentialed API. The
  research proves the markets *name* that source; it does not prove the source's arithmetic.
* **Confirmation depth stays `OPERATIONAL`** — no Polymarket requirement was found, so P10 must
  expose it rather than hard-code a number.
* **Non-binary payouts stay representable.** None occurred in 55 markets, which is an observation
  about 55 markets and not a property of the contract. Anything that is not exactly one non-zero
  slot summing to the denominator is an explicit ambiguous branch.
* **O14 is not closed.** `twapLookbackSeconds: 60` is suggestive and is not O14's evidence.

### Two flaws in the research method, found and fixed

The live study originally **stopped polling at the chain event**, so only sources that beat the
chain could ever have been observed — the question would have been answered by the method rather
than the data. It now follows for 120 s afterwards.

The RPC agreement count originally treated a **rate-limited provider as part of the agreeing
set**. Providers that did not answer are now recorded as absent, and the minimum independent
confirmation count is reported.

---

## P9C: the risk sequence had to be proved, not just used

Independent review accepted everything about P9B except one narrow audit-integrity defect in the
replay verifier. Evidence:
[`evidence/P9C-RISK-AUDIT-CLOSURE.md`](evidence/P9C-RISK-AUDIT-CLOSURE.md).

### The defect

The verifier addressed records *by* their risk sequence without ever verifying it.

```python
expected = records[0].risk_sequence if records else 0
```

The expectation came from the data, so a trace whose prefix had been lost — `3, 4, 5` — verified
as "internally contiguous". The scan only looked for values that skipped ahead, so `0, 1, 1, 2`
and `0, 1, 2, 1` passed too. And `verify_risk_replay` checked `state`, `active`, `latched`,
`allows_place`, and `allows_cancel` against each record while never comparing the sequence it
produced against the sequence recorded.

### The contract now

**`record[i].risk_sequence == i`**, positional and absolute, checked before any replay. The
expectation is not inferred from the file: a trace starting at 5 is a trace whose first five
permission decisions are unaccounted for. `produced.risk_sequence` is then compared to
`recorded.risk_sequence` alongside the verdict fields.

Partial replay stays unsupported — it would need an explicit initial sequence *and* an explicit
initial `RiskSnapshot`, since a tail without the state it inherited proves nothing.

A bounded `RiskTrace` that has **dropped** records therefore cannot verify. That is correct:
drop-oldest is right for the hot path, and a dropped trace is **audit incomplete**. Trading may
continue under the existing safety policy; the evidence may not claim deterministic full-risk
replay. Nothing renumbers the tail or invents the state it inherited.

### Malformed traces, all rejected

```text
valid            0,1,2,3…      PASS
lost prefix      3,4,5…        FAIL  expected 0, actual 3
missing middle   0,1,3,4…      FAIL  expected 2, actual 3
duplicate        0,1,1,2…      FAIL  expected 2, actual 1
backwards        0,1,2,1…      FAIL  expected 3, actual 1
shifted          100,101,102…  FAIL  expected 0, actual 100
tampered index   verdicts OK   FAIL  on risk_sequence
overflowed trace cap 16/100    FAIL  expected 0, actual 84
```

The tests discriminate: restoring the previous verifier fails seven of them. Two further tests
pin what must not change — several distinct risk sequences may legally share one
`as_of_ingress_ordinal`, and the out-of-order and reconciliation rules are untouched.

### Fresh real market

`btc-updown-5m-1787692200`, `CONTROLLED_LOCAL_FAULT_ON_REAL_MARKET`, 159,952 cycles, 155,549
CLOB and 7,548 BTC messages, **0 orders sent**.

```text
first risk sequence        0
last risk sequence   159,972
record count         159,973      distinct 159,973
duplicates 0    gaps 0    dropped 0    contiguous_from_zero true
verify_risk_replay   PASS
non-evaluation signals    18

11 halts, 33 transitions, EVERY one through RECOVERING
PLACE  473, ALL in SAFE.  Zero while HALTED, zero while RECOVERING.
CANCEL 361 in SAFE, 3 while HALTED   keep_ratio 0.99534
```

The passing replay is itself the proof of integrity: a trace that started late, skipped,
repeated, or went backwards could not have reached that line.

Risk behaviour is unchanged — the change is confined to `risk/replay.py`. The P9B market
findings stand; only P9B's *sequence-integrity claim* is superseded, and both P9B traces were in
fact complete and contiguous from zero, which the verifier of the day simply could not prove.

---

## P9B: staleness ownership and the ordered risk audit

Independent review accepted the risk model, the overlay, the halt semantics, the real-market
evidence, and the `STALE`-recovery defect it found — then identified two architectural gaps.
Evidence: [`evidence/P9B-REAL-MARKET-BASELINE.md`](evidence/P9B-REAL-MARKET-BASELINE.md) and
[`evidence/P9B-REAL-MARKET-FAULTS.md`](evidence/P9B-REAL-MARKET-FAULTS.md).

### Gap A — two authorities for one question

`RiskEngine` carried its own `_stale(last_at, now, threshold)` against its own copies of
`DEFAULT_CLOB_STALE_AFTER` and `DEFAULT_SPOT_STALE_AFTER`, while P6 already owned
`StalenessMonitor`, `StreamHealth`, and the `STALE` transition. Two answers to "has this stream
been quiet too long?" can disagree, and the wrong one stays invisible until it matters.

P6 now owns it entirely. P9 reads `HealthStatus` and decides what to do about it. Removed rather
than deprecated: `_stale`, both `last_message_at` inputs, and both thresholds — without the
inputs the comparison cannot come back by accident. Structural tests assert `risk/` imports no
staleness constant, builds no monitor, holds no last-message timestamp, and compares no age
against a limit; and that changing P6's threshold changes when `STALE` fires with no `RiskConfig`
edit at all.

Detection did not become lazy. `pipeline.check_staleness(now)` already ran on three capture-loop
paths — the queue-idle timeout, the fault-gated path, and after each payload — all before
`on_tick`, so silence is noticed without waiting for a market event. Proven live: the real BTC
pause halted **4,083 ms** after injection, which is P6's 5-second threshold.

### Gap B — permission could change with no ordered record

`RiskEngine.reconciled` mutated the latched snapshot directly and the fault scheduler called it.
Clock drift, API error rate, rate-limit uncertainty, and every reconciliation result could flip
`allows_place` without entering any ordered stream. A run could be replayed for its economics and
not for its permissions.

Risk now has its own versioned stream, **beside** the P5 journal and never inside it:

```text
RiskSignal      kind, as_of_ingress_ordinal, timestamp, provenance, reason, payload
RiskRecord      schema version, strict risk_sequence, the signal, the health frame it saw,
                and the resulting state / active / latched / allows_place / allows_cancel
RiskController  the single owner of risk state, and the only path that may change it
```

`as_of_ingress_ordinal` means: the signal was applied after every market event through that
ordinal had been consumed, and before the next permission decision referencing this risk
sequence. Nothing depends on coroutine scheduling. Feed health is not duplicated — `HealthEvent`
already exists in the market stream.

Order is enforced: a signal preceding the last applied ordinal is refused, and a
`RECONCILIATION_CONFIRMED` for a reason that is not latched is refused as a duplicate or a claim
about something never in doubt. A structural test asserts nothing outside the controller calls
`engine.reconciled`.

`verify_risk_replay` re-derives every verdict and fails closed at the first divergence, naming
the risk sequence, the ingress ordinal, and both values. A sequence gap fails immediately: an
audit missing records cannot explain the cycles it lost.

### Fresh real-market evidence

Baseline `btc-updown-5m-1787678100` — 153,082 cycles, 152,738 CLOB and 3,196 BTC messages, 0
malformed, 0 reconnects.

```text
risk records 153,082   dropped 0   gaps 0   REPLAY VERIFIED
SAFE 152,914 (99.89%)  HALTED 167 (the explained T0 spot-UNKNOWN halt)  RECOVERING 1
PLACE 679, ALL in SAFE     1,217 actions each attributed to a risk sequence
```

Faults `btc-updown-5m-1787679300` — 162,644 cycles, 12 halts, **36 transitions, every one
through RECOVERING**.

```text
risk records 162,666   non-evaluation signals 18   dropped 0   gaps 0   REPLAY VERIFIED
PLACE  805, ALL in SAFE.  Zero while HALTED, zero while RECOVERING, across 29,401 halted cycles.
CANCEL 693 in SAFE, 9 while HALTED  -- resting quotes withdrawn.

btc_stale             halt seq  24,857   4,083 ms reaction (P6's threshold)   SAFE seq  34,268
clob_disconnect       halt seq  61,862      24 ms                             SAFE seq  61,865
genuine venue drop    halt seq  84,498   real incident, not induced           SAFE seq  84,502
continuity_uncertain  halt seq  99,983       0 ms                             SAFE seq  99,987
clock_drift           halt seq 111,105       0 ms                             SAFE seq 112,983
order_state_uncertain halt seq 120,352  LATCHED                               SAFE seq 124,971
position_mismatch     halt seq 128,786  LATCHED                               SAFE seq 131,556
cost_ledger_mismatch  halt seq 137,119  LATCHED                               SAFE seq 141,957
api_error_rate        halt seq 147,791       0 ms                             SAFE seq 150,779
rate_limit_uncertain  halt seq 155,892       0 ms                             SAFE seq 158,117
resolution_ambiguous  halt seq 159,993       0 ms                             SAFE seq 160,662
```

The three latching reasons show the required three-step in the trace: the condition clears and
**permission does not return** — the state goes to `RECOVERING` with the reason still latched —
and only the explicit `RECONCILIATION_CONFIRMED` signal restores `SAFE`.

**One halt was a genuine venue incident**, not induced: the feed counters record two reconnects
where only one was forced. It is labelled as such rather than folded into the induced count, and
it recovered in 1,071 ms by the same path.

### Two defects these runs found

The first P9B fault run halted on `API_ERROR_RATE` for **0 ms**: the injector forced the
condition on and the real `ApiErrorMonitor` cleared it on the next evaluation. Two sources
writing one condition. Fixed in the injector; the corrected run holds it for 3,907 ms. **The
ordered trace is what made it visible** — two opposite `API_ERROR_STATE_UPDATE` records one
sequence apart.

Measuring the ordered path also found the third frozen-dataclass cost in this codebase.
`RiskInputs` alone cost 1,907 ns to construct; as a `NamedTuple` the whole path went **6,390 →
4,728 ns**. P8 found the same thing twice.

---

## P9: risk, health, and recovery (first round — read with the correction above)

The governing rule, from Canonical §28 and Detailed §38: **if the bot cannot trust its own
state, stop new quoting, reconcile, and resume only when safe.** Detailed §38 closes with the
sentence the whole phase turns on — *accuracy is more important than continuing to trade*.

Evidence: [`evidence/P9-REAL-MARKET-BASELINE.md`](evidence/P9-REAL-MARKET-BASELINE.md) and
[`evidence/P9-REAL-MARKET-FAULTS.md`](evidence/P9-REAL-MARKET-FAULTS.md).

### Three gates, deliberately not merged into one word

```text
implementation          PASSED    all twelve conditions exist, trip, and recover
real-market integration PASSED    one healthy live market + one live market with
                                  controlled local faults
authenticated execution UNRUN / DEFERRED TO P14
```

The third covers taker fill, real order uncertainty, real account position and cost
reconciliation, and real write-API behaviour. None of it can be claimed: no credential exists,
no authenticated socket has been opened, and no order of any size has been sent. The mechanisms
are implemented and unit-tested; their **empirical** status is unrun, and a mock result would
not change that.

### What it is, and three things it is not

`RiskState` is `SAFE` / `HALTED` / `RECOVERING` — one verdict carrying its typed reasons, not a
bag of unrelated booleans. Twelve reasons: Canonical §28.1's eleven plus Detailed §38's taker
fill. A fresh engine starts `RECOVERING`, because before anything is observed permission must
not be the default.

* **Not a stop-loss.** Canonical §28 opens by saying the target strategy does not use one. No
  SELL, hedge, flatten, merge, split, convert, or directional rescue exists in the package —
  asserted structurally over function, class, and attribute names. A halt withdraws quotes and
  **holds the balances** (I15).
* **Not `band_hard`.** P4 owns the one-sided wall and it stays a wall: at `I >= +band_hard` UP
  is blocked while DOWN remains a legitimate order (I17).
* **Not part of the strategy.** `StrategyEngine.decide` is untouched and has no stale-feed,
  drift, API-error, or order-state branch. A healthy verdict returns the **identical**
  `DecisionResult` object, not an equal copy.

### Halt and recovery semantics

A halt turns desired intent into *nothing*, which is exactly what P7's minimal-action
reconciler needs to plan `CANCEL` for anything resting — withdrawal falls out of the existing
rule rather than needing a code path that knows how to retreat. `CANCEL` is permitted
throughout; `PLACE` and the placement half of `CANCEL_THEN_PLACE` are not. An `UNKNOWN` order is
`WAIT`ed on, never cancelled again and never replaced.

`HALTED → RECOVERING → SAFE`, always. `SAFE` needs no active condition, nothing latched, **and**
two consecutive clear evaluations. Unknown order state, position mismatch, cost-ledger
mismatch, and taker fill latch past their own condition: an unknown order does not become known
because a socket reconnected. A CLOB reconnect alone never restores `SAFE` — the condition
clears only when P6 reports `HEALTHY` and is no longer awaiting a snapshot.

### Real market: baseline

`btc-updown-5m-1787672100`, 154,882 cycles, 148,204 CLOB and 9,777 BTC messages, 0 malformed,
0 reconnects, 0 observation drops, **0 orders**.

```text
SAFE       154,876 cycles   99.996%
HALTED           5 cycles   one halt, at T0, explained below
RECOVERING       1 cycle
PLACE          399, ALL in SAFE          keep_ratio 0.99608
RiskEngine.evaluate  p50 9,549 ns   p99 59,052 ns
```

The single halt is `SPOT_STALE` at ingress ordinal 2 and is **correct, not a false positive**:
during pre-arm, spot payloads are parsed for precision but deliberately not routed through
`on_spot`, so at `T0` the bot has not yet seen a BTC price through its own pipeline and the feed
status is genuinely `UNKNOWN`. Refusing to quote a BTC-referenced market before seeing a BTC
price is the behaviour the engine is supposed to have. It cleared in 457 ms.

### Real market: controlled local faults

`btc-updown-5m-1787674200`, 112,958 cycles. Market data real throughout; the faults are induced
**local** failures and are labelled as such, never as venue incidents.

```text
fault                  inject ord   halt ord   reaction   reason                       SAFE ord
btc_stale                  28,087     30,822   4,754 ms   SPOT_STALE                     41,265
clob_disconnect            68,519     68,521      25 ms   CLOB_CONTINUITY_UNCERTAIN      68,525
continuity_uncertain       96,129     96,130       0 ms   CLOB_CONTINUITY_UNCERTAIN      96,133
clock_drift               101,594    101,594       0 ms   CLOCK_DRIFT                   102,688
order_state_uncertain     104,451    104,451       0 ms   ORDER_STATE_UNCERTAIN         105,365
position_mismatch         106,331    106,331       0 ms   POSITION_MISMATCH             107,388
cost_ledger_mismatch      108,999    108,999       0 ms   COST_LEDGER_MISMATCH          109,430
api_error_rate            110,339    110,340      16 ms   API_ERROR_RATE                110,809
rate_limit_uncertain      111,761    111,761       0 ms   RATE_LIMIT_UNCERTAIN          112,142
resolution_ambiguous      112,899    112,899       0 ms   RESOLUTION_AMBIGUOUS          113,258

33 transitions, EVERY one through RECOVERING. No direct HALTED -> SAFE anywhere.
PLACE  245, ALL in SAFE      0 while HALTED      0 while RECOVERING
CANCEL 175 in SAFE, 2 while HALTED, 1 while RECOVERING   -- resting quotes withdrawn
```

The 4,754 ms reaction to `btc_stale` **is** the 5 s staleness threshold, not a delay; halting
sooner would mean halting on ordinary quiet periods. The 723 ms `clob_disconnect` halt is the
genuine reconnect round trip — socket close, backoff, reconnect, resubscribe, fresh
authoritative snapshot — and `SAFE` was unreachable for its whole duration.

`ORDER_STATE_UNCERTAIN`, `POSITION_MISMATCH`, and `COST_LEDGER_MISMATCH` visibly stayed
**latched** through `RECOVERING` and only cleared when `RiskEngine.reconciled` was called
explicitly. Releasing the signal alone did not restore `SAFE`, which is the point.

**Two conditions were not induced and are not claimed.** `TAKER_FILL` is **UNRUN / DEFERRED TO
P14** — no order was sent, so no fill occurred. `MAKER_ONLY_UNCERTAIN` has supporting unit tests
only. `CLOB_STALE` was never reached because the CLOB never went quiet for 10 s; the same
staleness code path was exercised by `SPOT_STALE`.

### The defect fault injection found

The **first** fault run halted correctly on `btc_stale` and then never recovered, staying
`HALTED` for 132,717 of 160,917 cycles and masking the two later faults entirely.
`StreamHealth` had no path out of `STALE`: `mark_message` updated only the timestamp, and
`mark_snapshot` was reachable only while `awaiting_snapshot` was set — which `STALE` does not
set. One quiet BTC feed would have halted the bot for the rest of a market.

Fixed in `1584dee`. `DISCONNECTED` and `SEQUENCE_GAP` are deliberately still not cleared by a
message, because they set `awaiting_snapshot` and one message after a continuity break says
nothing about the messages that were missed.

**A green unit-test suite did not catch this.** It appeared the moment a real adapter was paused
during a real market, which is precisely the case for the evidence policy now recorded in
[`ARCHITECTURE_SSOT.md`](ARCHITECTURE_SSOT.md) §4.4.

---

## P8C: the telemetry offload

P8B's measurement was correct and synchronous, and that was the problem: every cycle mutated
shadow queue slots, counted actions, classified both sides, and updated distributions before the
trading loop could continue. **+4,902 ns (+15.1%)** on an ordinary unsampled book update, for
work no trading decision depends on. That gate was reported as failed rather than waived.

Full evidence: [`evidence/P8C-PERFORMANCE-CLOSURE.md`](evidence/P8C-PERFORMANCE-CLOSURE.md).

### Observation is now split in two

The trading path captures *facts* — the displayed depth at our own price, the reconcile plan,
stage timestamps for sampled cycles — into a bounded non-blocking buffer, and returns.
`TelemetryAnalyzer` reconstructs queue estimates, classification, counters, and distributions
downstream, in ingress order, after the market.

The split is at **analysis**, not at **simulation**. Preparation, reconciliation, and the shadow
order-table lifecycle model what production does every cycle, so they stay hot and run in both
benchmark arms. Charging them to telemetry is exactly the error that produced P8's earlier +133%
and +217% figures. Depth cannot move either: the book is mutable, and the size at our own price
has to be read at the moment the cycle sees it.

Representation was measured, not assumed — a tuple of references costs 76 ns against 1,791 ns
for the "clean" frozen dataclass, and `deque.append` beat a hand-rolled ring.

### Order is authoritative, gaps are not bridged

Observations carry a capture sequence and are folded strictly in order. Out-of-order input
**fails closed**; a sequence gap means an unseen depth change at our own price, so the estimate
goes `STALE` rather than being continued. Trading is unaffected by a telemetry drop — the loss
is in observation, not execution — but the measurement says so.

### Sampling now prevents timing work, not just output

The decision is made before reduce and decide, so an unsampled ordinary event takes no
perf-counter readings at all. An action discovered after reconciliation is still recorded, with
one reading for the action and its earlier stages left `NOT_CAPTURED`. Nothing is imputed.

### Equivalence proven, not assumed

The offloaded pipeline reproduces the synchronous model **exactly** — slot counts, typed loss
reasons, every action and quality counter, and the complete 220-element ordered queue-ahead
sequence — checked against a golden snapshot taken by running the same tool inside a worktree at
`c5cec7f`. Frozen at `tests/telemetry/golden/synchronous_queue_semantics.json`.

### Limits met, without moving them

```text
                                      target        P8B        P8C
unsampled full-cycle p50 overhead   <= 5,000 ns    4,902        955   MET
unsampled full-cycle p50 overhead   <= 5%          15.1%       2.9%   MET
decide p50 (process-isolated)       <= 1,000 ns    1,968*       454   MET
decide p50 (process-isolated)       <= 3%           7.7%*     1.73%   MET
```

`*` P8B's decide figures were same-process and therefore contaminated by allocator and cache
pressure; they are shown for continuity, not as a comparable measurement. P8C runs each
configuration in a fresh interpreter across twelve alternating pairs.

Hot-path capture is now **347 ns** on an unsampled cycle, from ~1,970 ns. Sampled cycles
(+1,360 ns, +4.1%) legitimately cost more and are reported separately. The p99 tail is
GC-dominated — disabling GC halved the instrumented p99 delta — and that is stated rather than
smoothed away.

### Confirmed on a full real market

`btc-updown-5m-1787663400`, **204,440 cycles**, `live_trading_enabled: false`, **0 orders
sent**, **0 observation drops**, **0 gaps**, 0 malformed feed messages, 0 reconnects.

```text
observations captured / capacity   204,440 / 320,000     zero dropped, zero lost
cycles with stage timing            20,440 = 10.0%       sampling is 1-in-10
keep_ratio                         0.99259
shadow slots acquired                1,500               = PLACE actions exactly
shadow slot losses                   1,500               reconciles to typed reasons
reconcile_duration p50              11,154 ns            O15 holds; smallest stage
receive_to_reconcile p50           178,857 ns
queue_ahead                        p50 0, p95 166, p99 434 shares
BLOCKED                    173,215 of 408,880 sides (42.4%)
```

`OFF_PRICE` is 0 structurally — instantaneous shadow acknowledgement means no order can rest at
a stale price while a replacement is in flight. `STALE` is 0 because continuity held throughout.
The `POST_ONLY_BLOCK` finding is unchanged and still **not acted on**; no spread was introduced.

### One defective run, retained

The first P8C market produced stage timings for **126 cycles instead of ~15,400**, and nothing
raised. The sampling decision was being made twice — once on the hot path with
`meta.ingress_ordinal`, once downstream with the observation's ordinal — and `next_meta`
increments after assigning, so at `sample_every = 10` the two answers could essentially never
agree. Whether a cycle was sampled is now a captured fact rather than a recomputation, and a
regression test asserts the arithmetic that would have caught it. That run is retained as
`p8c-measurement-SUPERSEDED-*`; its queue and action results were unaffected and agree with the
corrected run.

---

## P8 correction: shadow queue lifecycle, O15, and telemetry overhead

Independent review found three closure issues in the accepted P8 work. All three were real.
The original evidence is retained and labelled, not rewritten
([`evidence/P8-MEASUREMENT.md`](evidence/P8-MEASUREMENT.md)); corrected evidence is in
[`evidence/P8B-MEASUREMENT.md`](evidence/P8B-MEASUREMENT.md).

### 1. Shadow queue slots followed desired price, not the order lifecycle

`ShadowQueueTracker.on_desired(outcome, desired_price, depth)` opened and advanced a slot
whenever the strategy *wanted* a price — including every side the reconciler had just refused
to submit. The first P8 market produced **119,116** `POST_ONLY_BLOCK` sides, so this was not a
corner case: blocked intent acquired queue estimates, aged them, banked every depth decrease at
that level as consumption ahead of it, and reported itself at the front of a queue no order had
ever joined.

Slots now follow the executable lifecycle and are keyed by **client order id**, never by
`(outcome, price)`:

```text
PLACE                    acquire, at the depth displayed immediately before dispatch
KEEP                     preserve, update from current depth
partial fill + KEEP      preserve the same identity; ahead becomes zero
CANCEL                   close
REPLACE                  close and grant nothing - P7 is CANCEL_THEN_PLACE, so the
                         replacement's slot begins only when a later cycle reaches PLACE
BLOCKED / WAIT / NOTHING no slot whatsoever
continuity loss          slot survives, confidence does not
```

`classify()` no longer takes a `resting_price` argument at all: the resting price is read from
the queue estimate, and an estimate exists only while an order holds a slot. `AT_FRONT` is
therefore structurally unreachable without a real shadow order, rather than merely unlikely.

The regression suite was checked for discrimination the same way the P7 concurrency test was:
restoring the desired-price model makes **5** of the 13 lifecycle tests fail, including the
mandatory post-only regression.

### 2. O15 — `current()` scanned all retained history

Measured, then fixed. `LiveOrderTable` now maintains an incremental per-outcome index of
occupying order ids, updated on every lifecycle transition and never rebuilt by rescanning.
History retention is untouched — it is required for idempotency — and the occupancy index sits
alongside it.

```text
retained terminal orders        0     200     1,049      10,000
before (ns per cycle)       1,311  52,097   251,406   2,512,039
after  (ns per cycle)         477     467       498         461
```

Slope: **251.0 ns per retained order before, -0.0016 after**. At 10,000 retained orders the
lookup is ~5,450x cheaper and no longer grows at all.

### 3. Instrumentation overhead was real, and was described too kindly

The previous report's +21.3% was measured by a method that had already produced three wrong
answers. It is replaced by a paired, interleaved, warmed, per-repeat, tier-split benchmark run
against a production-shaped steady-state stream as well as the replay corpus.

State maintenance and emission are now separated: shadow slot transitions and action counters
run on every cycle of a measuring run — skipping them for unsampled events would make the queue
estimate depend on the sampling rate — while stage timestamps, distributions, classification
and the sink run only for traced cycles. Removing a discarded six-field `QueueEstimate` from
`on_keep` alone was worth about 1 µs per side.

**The performance limit was not met in this round, and was reported as not met.** On an
ordinary unsampled production-shaped cycle: **+4.90 µs (+15.1%)**. The residue was the two-side
analytical state loop (1,570 ns) that maintained slot depth and action counts synchronously.
That loop no longer runs on the trading path at all — see the telemetry offload above, which
closes this gate at +955 ns (+2.9%).

### Confirmed on a fresh real market

`btc-updown-5m-1787658900`, 117,772 cycles, `live_trading_enabled: false`, **0 orders sent**,
0 telemetry drops. Full evidence: [`evidence/P8B-MEASUREMENT.md`](evidence/P8B-MEASUREMENT.md).

```text
reconcile_duration p50   171,659 ns  ->   14,882 ns    -91.3%   (O15 CLOSED)
receive_to_reconcile p50 323,138 ns  ->  240,367 ns    -25.6%
keep_ratio                 0.99339   ->    0.99568
shadow slots acquired        1,049   ->        462     = PLACE actions exactly
queue_ahead p99          249 shares  -> 367 shares     phantom-slot optimism removed
```

Reconciliation is now the **smallest** stage on the critical path, below `decide` (66,105 ns)
and `prepare` (17,764 ns). Slots acquired equalling PLACE actions exactly is the defining
property of the corrected model.

Two counts that need reading carefully: `BLOCKED` is exact over every side (**97,534 of
235,544, 41.4%**), while the quality classification is now sampled 1-in-10, so
`POST_ONLY_BLOCK` reads 10,117. The finding is unchanged and still **not acted on** — no
spread was introduced. `OFF_PRICE` is 0 for a structural reason: shadow acknowledgement is
instantaneous, so no order can rest at a stale price while a replacement is in flight. It is
unit-tested at the classifier and needs real dispatch latency (P13/P14) to occur in a run.

---

## P8: what measurement found (first run — read with the correction above)

Instrumentation only. **No strategy parameter was changed to improve any number below.**
Queue figures in this section are superseded; latency, actions, and `keep_ratio` stand.
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
| **Current phase** | **P13 — live shadow / paper mode** |
| P13 implementation gate | **PASSED** (P13F) — every audit read and write on a dedicated thread, O(1) qualification per market with full audits at the boundaries, one result per attempt and per market |
| P13 pilot gate | **PASSED** (P13F) — three consecutive real markets with the audit path deliberately slowed by 500 ms, all qualifying, zero drops. Earlier pilots retained and superseded |
| P13 ≥200-market empirical corpus gate | **PASSED** — 202 qualifying markets, epoch `p13-corpus-6`, collected by `9a42031` |
| P13 long-run resource stability gate | **NOT PASSED** — two evidence-backed attempts, neither meeting the bar. `p13-resource-1` (journal streaming): after-warm-up **+1.3607** [+1.169, +1.553]. `p13-resource-2` (adds one `malloc_trim` per rollover): **+2.7434** [+2.351, +3.136] — worse, on 11 % less work, despite returning 1,082 MB. Ceiling +1.026 with a CI containing zero, unchanged. Against the original 4,262 MB failure the streaming fix still stands: all-run +10.26 → +2.13, end RSS 489.8 MB after 57 markets |
| **P13 overall** | **NOT COMPLETE** — corpus accepted, collector runtime not |
| P14 | **BLOCKED** — residual resident-memory growth with no window whose 95 % interval contains zero, and the first mitigation made it worse; GC tail latency a second readiness risk, unchanged by either attempt |
| P12 implementation gate | **PASSED** — Plane-3 UI, immutable snapshot, ordered control, no trading reference |
| P12 real-market gate | **PASSED** — UI killed mid-market, trading continued |
| P12 Plane-3 isolation gate | **PASSED** (P12C) — no synchronous I/O of any kind on the ingress path, stdout included |
| P12 snapshot coherence gate | **PASSED** (P12C) — every published figure joined to the observation it describes; final frame equals the manifest |
| P12 transaction-durability gate | **PASSED** (P12E) — a persisted callback and a written counter mean the row is in a transaction that committed; a failed commit announces nothing and cannot verify COMPLETE |
| P12 ordering/metric-contract gate | **PASSED** (P12D) — `decide_ns` is P8's decide_duration; "written" means durably written; audit columns equal their payload; delivery order is the transport's, not a wall clock's |
| P12 control-audit gate | **PASSED** — command id durably cross-linked to its RiskRow |
| P11 implementation gate | **PASSED** — versioned schemas, non-blocking worker, exact analytics, verifier |
| P11 real-market gate | **PASSED** — P11B healthy market + controlled stalled sink |
| P11 durability-integrity gate | **PASSED** — every V2 decision names and matches its governing RiskRow; exact risk and storage order; real, self-consistent event ids; self-consistent schema version drawn from a defined domain; append-only audit rows; verified archive reads |
| P11 storage representation | **CLOSED** — lossless `lzma` archive, 54.7×, 2.2 GB per 200-market corpus |
| P11 authenticated fill/economics | **UNRUN / DEFERRED TO P14** |
| P10 implementation gate | **PASSED** — verifier, payout arithmetic, plan, encoder, audit record |
| P10 real-market resolution gate | **PASSED** — 35 real markets over 6 live runs, 1 full lifecycle |
| P10 trust-boundary gate | **PASSED** — provider independence, bound identity attestation, atomic finality |
| P10 authenticated redemption gate | **UNRUN / DEFERRED TO P14** — no key, no credential, no transaction |
| P9 implementation gate | **PASSED** |
| P9 real-market integration gate | **PASSED** — fresh baseline + controlled-fault markets |
| P9 deterministic-risk-audit gate | **PASSED** — exact sequence contract, replay verified on real data |
| P9 authenticated execution gate | **UNRUN / DEFERRED TO P14** |
| P8 implementation gate | **PASSED** |
| P8 performance gate | **PASSED** — see the telemetry offload below |
| P7 implementation gate | **PASSED** (with the concurrency correction below) |
| Live execution | **NOT ARMED and not armable** — P14 owns that |
| Target-wallet empirical replay | **UNRUN / BLOCKED** |
| Real order round-trip latency | **UNRUN — P14** (measuring it requires sending an order) |
| Current branch | `feature/p8-queue-latency` |
| Venue-tick correction | `55977f4` — `fix: update current venue tick capabilities` |
| P7 boundary commit | `de96681` — `feat: add post-only execution reconciler` |
| P7 concurrency correction | `d333aeb` — `fix: dispatch independent outcome orders concurrently` |
| P8 boundary commit | `16bd4d4` — `feat: add queue and latency instrumentation` |
| P8 correction branch 1 | `fix/p8-measurement-hotpath-closure` (`c5cec7f`) |
| P8 correction commits 1 | `f96815f` O15 · `e1604bf` shadow lifecycle · `a6f903e` telemetry overhead |
| P8 correction branch 2 | `fix/p8-telemetry-offload` |
| P8 correction commits 2 | `bbeb9b5` analytics offload · `88a7807` stage sampling · `4cb9d5f` benchmarks |
| Current branch | `feature/p9-risk-recovery` |
| P9 boundary commit | `4c2ab1d` — `test: exercise every inducible risk condition on a real market` |
| P9 correction branch | `fix/p9-risk-ordering-staleness` |
| P9 correction boundary | `3ab5fa3` — `docs: record corrected P9 real-market evidence` |
| P9 correction commits | `ea2625e` staleness authority + ordered risk stream · `5478da5` NamedTuple hot types · `3ab5fa3` evidence |
| P9C audit-closure branch | `fix/p9-risk-sequence-integrity` |
| P9C boundary commit | `44e05e1` — carries the P9C evidence and docs |
| P9C commits | `63cc2b5` verifier + integrity tests · `44e05e1` evidence, docs, manifest block |
| P1 parser correction | `226663d` — `fix: accept leading-dot exact decimals` |
| P10A research branch | `research/p10-o11-resolution` |
| P10 branch | `feature/p10-settlement` |
| P10 commits | `eae8b6f` verifier · `c9414c8` settlement + redemption planning · `fde4b38` risk bridge · `4f3789d` first (wrong-premise) split fix · `8b60685` provider-quorum correction · `9fe374a` real-market evidence |
| P10 own-ledger settlement economics | **UNRUN / DEFERRED TO P14** — every ledger settled is empty |
| P10 trust-boundary branch | `fix/p10-settlement-trust-boundary` |
| P10 trust-boundary commits | `836cc87` defects A + B + C and the finality/block corrections · `381e21e` O16 latch · `14ce2d9` corpus revalidation · `fbdc669` fresh real markets · `f4d793d` docs |
| Where defect C landed | `836cc87`, in `settlement/reader.py`. Its message enumerates A, B, the finality tag and the missing block but not the moving-tag fallback; the change is there and is documented in the evidence. Recorded here rather than corrected by rewriting the commit. |
| Configured provider independence | **OPERATIONAL assumption** — duplicate ids and URLs refused; organisational independence unproved |
| P10C branch | `fix/p10-attestation-binding` |
| P10C commits | `f5011ef` binding · `47f1fa1` refusal tests · `236f369` real revalidation |
| Attestation binding | identity → endpoint → reading → attestation, checked at each layer; **software trust boundary, not cryptographic** |
| GitHub default branch | `main` (verified 2026-08-26; the earlier `bootstrap/phase-0` observation no longer holds) |
| P9 commits | `4be0032` risk engine · `6576de0` recovery + runner · `1584dee` stale recovery fix · `b2e715e` real-market evidence · `4c2ab1d` full fault market |
| Last accepted milestone | P11F — supported schema domain (`ea892c4`, now `main`) |
| Next milestone | P14 — **not started and blocked**. The corpus is complete; the collector's resident-memory growth is not resolved |
| `main` | `ea892c4` — fast-forwarded through the whole accepted P11 lineage, pushed |
| P11 branch | `feature/p11-telemetry-persistence` |
| P11B branch | `fix/p11-persistence-integrity` |
| P11C branch | `fix/p11-final-audit-closure` |
| P11D branch | `fix/p11-reference-completeness` |
| P11E branch | `fix/p11-schema-contract-integrity` |
| P11F branch | `fix/p11-supported-schema-domain` |
| P12 branch | `feature/p12-ui-control-plane` |
| P12B branch | `fix/p12-plane3-isolation` |
| P12C branch | `fix/p12-snapshot-coherence` |
| P12D branch | `fix/p12-final-contract-closure` |
| P12E branch | `fix/p12-commit-boundary` |
| P13 branch | `feature/p13-live-paper` |
| P13 base | `feb7e2b` — merge of P12E into main, no rebase, no force |
| P13 corpus epoch (superseded) | `p13-corpus-1`, source `bb67b18` — 12 COMPLETE rows preserved, excluded from the count |
| P13B branch | `fix/p13-corpus-integrity` |
| P13C branch | `fix/p13-final-corpus-foundation` |
| P13D branch | `fix/p13-final-evidence-binding` |
| P13E branch | `fix/p13-final-counter-integrity` |
| P13F branch | `fix/p13-plane3-audit-isolation` |
| P13 corpus source SHA | `9a42031df1f46762a0a8ef958240342612586084`, tree `29aca3d58f1b4c3cf65161b99fb4137566c3adf5` |
| P13 accepted corpus | `p13-corpus-6` — 202 markets, config `09c82f15…`, at `/home/hr/p13-corpus-6/` |
| P13 corpus epochs superseded | `p13-corpus-1` (12), `-2` (10), `-3` (12), `-4` (4), `-5` (52) — all preserved, all excluded |
| P12C final snapshot | retained unedited; its `decide_ns` label is corrected in `p12d-p12c-snapshot-latency-correction.json`, not in the file |
| P12B final snapshot | **known-inaccurate read-model artifact**, retained unedited; see `p12c-p12b-revalidation-btc-updown-5m-1787807700.json` |
| P11 store size | 5,230 B/decision raw → **95.7 B archived**; engineered in P11B, not deferred |
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
| Suite | 1 404 passed (1 242 at the P8C boundary; +162 across P9 and its two corrections) |
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

Full detail in [`OPEN_ITEMS.md`](OPEN_ITEMS.md). **P8 added O15 and closed it**, confirmed on
real market data. O08 and O09 are now *measurable* but remain **OPEN**: neither can be settled
without real resting orders, which P8 does not place.

**P9 closed none and added none.** Operational safety observations do not resolve strategy
parameters, and the risk thresholds are `OPERATIONAL` engineering configuration rather than
reconstructed constants — so they get no open item either. The telemetry and risk architectures
are engineering concerns, not questions about the reconstructed strategy.

```text
O01 quote-centre source            OPEN      O08 latency for queue dominance OPEN
O02 volatility sigma               OPEN      O09 spot-to-CLOB timing model   OPEN
O03 base-lot L selection rule      OPEN      O10 venue precision / scales    CLOSED
O04 grid-target selection          OPEN      O11 resolution source           CLOSED
O05 endgame tilt magnitude         FITTED    O12 BTC spot scale              CLOSED
O06 endgame gate magnitude         FITTED    O13 tick tie-breaking           OPEN
O07 fee/rebate calibration         OPEN      O14 strike chaining unverified  OPEN
                                             O15 current() linear in orders  CLOSED
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
