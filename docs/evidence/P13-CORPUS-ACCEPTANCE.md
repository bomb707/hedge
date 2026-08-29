# P13 — 202-market live-paper corpus: acceptance audit

**Provenance: `REAL_PUBLIC_MARKET_DATA`.** 202 consecutive real Polymarket `btc-updown-5m`
markets, real Binance BTC spot, real Polygon settlement reads, shadow execution throughout. No
fault was injected on any counted market.

**No order. No credential. No chain write.** `LIVE_TRADING_ENABLED` and `REDEMPTION_ENABLED` are
`False` on all 202 rows; orders sent 0, redemptions sent 0.

## Two SHAs, and they are not the same

| | |
|---|---|
| **`CORPUS_SOURCE_SHA`** | `9a42031df1f46762a0a8ef958240342612586084` |
| **corpus source tree** | `29aca3d58f1b4c3cf65161b99fb4137566c3adf5` |
| **`ACCEPTANCE_EVIDENCE_COMMIT`** | the commit carrying this document — necessarily later |

The 202 markets were collected by the build at `9a42031`, on a clean tree, before any of this
documentation existed. All 202 rows carry that revision and that tree hash, and the acceptance
commit changes only `docs/`. Nothing here was collected by the documentation commit and no
document may say otherwise.

Epoch `p13-corpus-6` · config `09c82f1501e424dafe3cb9fcc55708541d00a08e344955410bee4d7cc5c355fa`
· run mode `ACCEPTANCE_CLEAN` · one process, 16 h 58 m, no restart · evidence at
`/home/hr/p13-corpus-6/`.

## Full audit, re-run against the durable files

Not the collector's own summary — the production report, re-run against `corpus.jsonl`,
`attempts.jsonl` and all 202 latency artifacts:

```text
corpus rows                        202     qualifying rows                    202
qualifying markets                 202     unique result attempt ids          202
unique market slugs                202     attempts started / finished    202 / 202
attempts failed                      0     attempts aborted                     0
open attempts                        0     terminal without start               0
duplicate starts                     0     duplicate terminals                  0
duplicate result attempts            0     duplicate market slugs               0
rows without start                   0     rows without finished terminal       0
rows refused                         0     latency artifacts refused            0
```

`one_result_per_attempt_and_market` and `every_row_joins_one_attempt` are both true.

## Stores, replays, denominators

* **202 / 202** stores `COMPLETE`, telemetry complete, with journal hash, store hash and a
  verified archive hash whose restored digest matches.
* **0 drops · 0 sequence gaps · 0 sink errors** across all 202.
* **202 / 202** replays `EXACT`, and `byte_roundtrip_identical` on all 202. No journal was
  repaired.
* **24,712,774 decisions → 49,425,548 side opportunities.** For every one of the 202 markets
  `classified == expected == action_total == 2 × decisions`. **0 mismatches.**

## L3 — exhaustive, over the market rather than a sample of it

```text
AT_FRONT            16,737,995   33.8651 %
PRICE_OK_BUT_DEEP    6,590,514   13.3342 %
NOT_QUOTING         26,090,511   52.7875 %
OFF_PRICE                    0    0.0000 %
STALE                    6,528    0.0132 %          (0.000132 as a fraction)
```

`NOT_QUOTING` explained: `POST_ONLY_BLOCK` 20,789,812 · `CENTRE_UNAVAILABLE` 2,788,244 ·
`PHASE_NOT_QUOTING` 1,670,650 · `ENDGAME_GATE` 721,858 · `NO_LIVE_ORDER` 87,937 ·
`OFF_VENUE_TICK` 32,010 · `CONTINUITY_LOST` 6,528. Quoting cycles: `QUOTING` 23,328,509.

Per-market `AT_FRONT`: min 0.2190 · p10 0.2957 · p25 0.3220 · **p50 0.3386** · p75 0.3524 ·
p90 0.3601 · max 0.3890.

Per-market `STALE`: p50 0.000000 · p90 0.000000 · p95 0.000033 · p99 0.000475 · max 0.042043.

**Actions**, same denominator: `KEEP` 23,178,586 · `BLOCKED` 20,760,114 · `NOTHING` 5,187,002 ·
`PLACE` 149,923 · `REPLACE` 87,937 · `CANCEL` 61,986 — 49,425,548 in total. (`WAIT` is not an
action this build's reconciler emits; the six above are the stored names.)

**Queue-ahead, `SHADOW_ESTIMATE`.** Median across markets of each market's own quantile: p50 **0**
· p75 12,260,000 · p90 130,000,000 · p95 219,000,000; largest per-market maximum 12,811,470,000.
This is P8's model of where our order would sit given the public book. **No own order was ever
sent and the venue never told us a queue position.**

## Prearm and feeds

**202 / 202 feed-ready at T0** — a usable book *and* a real BTC price, both still valid at the
boundary.

```text                    min        p50        p95        max
discovery lead          74.597 s   74.910 s   74.925 s   74.931 s
CLOB book-ready lead    28.795 s   29.792 s   29.849 s   29.899 s
spot first-valid lead   26.582 s   28.936 s   29.123 s   29.223 s
combined feed-ready     26.582 s   28.934 s   29.123 s   29.223 s
```

**24,202,374 CLOB messages · 1,069,559 BTC messages**, 0 malformed, 13 reconnects across the run.

## Risk and settlement

```text
risk records   SAFE 24,689,395   HALTED 23,183   RECOVERING 218
PLACE          SAFE   105,024    HALTED      0   RECOVERING   0
```

**PLACE while HALTED: 0. PLACE while RECOVERING: 0** — the hard requirement, met. 187 markets
contain some HALTED cycles: that is P9 refusing to act at market open until health is established,
and it never produced a PLACE. **0 operator commands**, 0 incidents, 0 operational faults across
the corpus.

Settlement: **202 RESOLVED**, all with an authoritative block — **UP 96, DOWN 106**.
`redemption_enabled` false on every row; no redemption transaction exists.

## Live latency — merged raw samples

Merged from the 202 identity-valid artifacts, exact over the concatenated samples. Not replay
latency, not averaged percentiles, not a p99 of p99s. Latency is **sampled** under P8's accepted
policy; classification and actions are exhaustive.

```text                              n         p50        p95         p99          max
CLOB receive → decide        2,364,238   114,643     573,229     849,083   912,071,878
CLOB receive → reconcile     2,364,238   131,870     642,833     955,314   912,102,348
spot receive → decide          106,789    78,463     315,919     535,139    26,366,908
spot receive → reconcile       106,789    93,533     357,389     609,188    26,383,556
decide_duration              2,471,835    29,226     118,188     209,515   911,961,731
prepare_duration             2,471,835     9,779      33,740      67,857   910,262,642
reconcile_duration           2,471,835     6,776      26,817      41,536    21,053,398
hot_path_observe            24,712,774    23,601      88,256     158,507 1,055,836,686
```

All figures in nanoseconds.

## The tail — recorded, not solved

Markets by their own worst `observe` cycle:

```text
> 1 ms    202 (100.0 %)      > 100 ms    40 (19.8 %)
> 5 ms    189 ( 93.6 %)      > 250 ms    40 (19.8 %)
> 10 ms   163 ( 80.7 %)      > 500 ms    25 (12.4 %)
> 25 ms    65 ( 32.2 %)      > 750 ms    14 ( 6.9 %)
> 50 ms    43 ( 21.3 %)      > 1 s        1 ( 0.5 %)
```

Worst twenty in `p13-corpus-6-gc-tail.json`, led by `…1787925900` at 1,055.8 ms.

Over the run: **215 generation-2 collections costing 134.0 s**, mean **623 ms** each, largest
1,738 ms, at thresholds `(700, 10, 400)`.

**What the evidence supports.** In the twenty worst markets the decide, prepare and reconcile
maxima are 1–20 ms — two orders of magnitude below the `observe` maximum — so the time is spent
around the cycle, not inside the strategy or the reconciler; and the mean full-collection pause is
the same magnitude as the large maxima.

**What it does not.** Full collections occur in 82 % of quiet markets as well as 90 % of loud ones,
so their mere presence separates nothing, and `GcObserver` records a **run-cumulative** maximum
rather than a per-market one, so exact market-by-market attribution is impossible from this run.
That instrumentation gap is itself a finding.

**No latency threshold is proposed here.** Classification: **P13 empirical evidence, and a P14
readiness risk.**

## Resource stability — this is the one that does not pass

Post-release RSS, sampled after each market's session was released:

```text
                    n    median      min       max
first 10           10   1,279.0     658.3   1,853.9
last 10            10   4,258.1   4,173.2   4,262.7
first 25           25   1,833.4     658.3   2,233.0
last 25            25   4,173.2   3,665.5   4,262.7
markets 1-50       50   2,350.9     658.3   2,489.1
markets 51-100     50   2,463.9   2,461.9   2,767.8
markets 101-150    50   2,780.3   2,718.2   2,804.1
markets 151-202    52   3,620.9   2,806.5   4,262.7      (MB)
```

Linear slope:

```text
all 202        +10.26 MB/market     +122.1 MB/hour
first 50       +32.23 MB/market     +383.8 MB/hour
middle 100      +4.66 MB/market      +55.5 MB/hour
last 50        +31.46 MB/market     +374.6 MB/hour
```

Process start 36.2 MB → first post-release 658.3 MB → **final post-release 4,261.7 MB**.

**Classification: `CONTINUED_PROCESS_RESIDENT_GROWTH`.** Quartile medians rise monotonically, the
run ends at its maximum, and the last-50 slope is indistinguishable from the first-50 slope and
roughly seven times the middle-100 slope. Growth did not decelerate — it resumed. **No plateau is
demonstrated at any point.**

### What the object counts do and do not prove

```text                  first-10 median   last-10 median    min          max
live sessions                  2                 2            1            3
threads                        8                12            8           13
open fds                      22                22           16           26
pending tasks                  —                 —            2           17
cold backlog                   1                 1            1        1 (cap 6)
lifecycle high-water           2                 3            2        3 (cap 6)
buffer high-water          3,672             5,036           89       18,665 (of 320,000)
gc tracked objects     2,793,708         2,532,808       39,150    4,827,529
```

Tracked-object slope: **+2,071 per market** over the whole run against a range of 39 K to 4.8 M,
with **negative** slopes over the first fifty (−37,816) and the last fifty (−6,643). Quartile
medians 2.03 M → 1.29 M → 1.17 M → 2.47 M: fluctuating with what is in flight, not trending.

So the released market graphs **really are released**. This is not a retained-session leak: live
sessions, file descriptors, cold backlog and lifecycle high-water are all bounded, and the Python
object count does not trend.

What grew is **resident memory**, and this instrumentation does not identify its source. Untracked
allocations, native buffers held by SQLite or LZMA, thread stacks and allocator fragmentation are
all consistent with the data and **none of them is established by it**. Saying "allocator
retention" would be inferring a mechanism from an absence, which is exactly the move this project
does not make. Attributing it needs measurement this run did not take.

One more observation: **thread count rose from a first-ten median of 8 to a last-ten median of
12**, maximum 13. Bounded, but not flat, and worth looking at alongside the memory question.

**No numeric pass threshold was invented after seeing the result.** The classification rests on the
shape of the data: monotone quartiles, an end-of-run maximum, and no deceleration.

## Frozen source and safety

All 202 rows share one epoch, one source revision (`9a42031…`), one source tree (`29aca3d…`), one
config hash (`09c82f15…`) and `run_mode = ACCEPTANCE_CLEAN`, all with `working_tree_clean = true`.
Not one mixed row.

`LIVE_TRADING_ENABLED` false · `REDEMPTION_ENABLED` false · orders sent 0 · redemptions sent 0 ·
zero authenticated venue writes · zero chain writes. Every `PLACE` in this corpus is a
**`SHADOW_ORDER`** against a recording transport; none of them reached a venue.

## Gates

| gate | verdict |
|---|---|
| P13 implementation | **PASSED** |
| P13 pilot | **PASSED** |
| **P13 ≥200-market empirical corpus** | **PASSED** — 202 qualifying markets |
| **P13 long-run resource stability** | **NOT PASSED** — `CONTINUED_PROCESS_RESIDENT_GROWTH` |
| **P13 overall** | **NOT COMPLETE** |

The 202-market dataset stands on its own and is **ready for P15 analysis**. The process that
collected it needs runtime engineering before it is fit to run unattended for longer, and those
are separate facts: a corpus does not become unsound because the collector's memory grew, and a
collector does not become sound because the corpus is good.

**P14 is BLOCKED** pending runtime resource engineering, with two review items:

* **A — GC tail latency.** `observe` maxima to 1,056 ms; 19.8 % of markets above 100 ms. A P14
  readiness risk regardless of the memory question, because no canonical latency threshold has
  been established. O08 owns that and remains OPEN.
* **B — resident-memory growth.** 36 MB → 4,262 MB over 202 markets with no plateau. This is the
  blocker.

Neither was fixed here. This task is evidence closure, and no runtime code was changed.

## OPEN items: unchanged

O01–O09 are **not closed**. P13 produced the dataset the experiments will run on; P15 owns the
controlled experiments and any strategy-label change. An `AT_FRONT` rate is not a queue threshold
and a stale distribution is not a staleness rule.

## Committed evidence

| file | what |
|---|---|
| `P13-CORPUS-ACCEPTANCE.md` | this document |
| `p13-corpus-6-report.json` | the full machine-readable audit |
| `p13-corpus-6-index.jsonl` | the 202 corpus rows, byte-for-byte as the collector wrote them |
| `p13-corpus-6-attempts.jsonl` | the 404 attempt events, unedited |
| `p13-corpus-6-artifacts.json` | per-market journal, store, archive and latency paths, sizes, SHA-256 |
| `p13-corpus-6-resources.json` | the resource analysis and its classification |
| `p13-corpus-6-gc-tail.json` | tail distribution, worst twenty, GC association |
| `p13-corpus-6-collector.log.txt` | the collector's own run log, unedited (`.txt` because `.gitignore` excludes `*.log`) |

The bytes stay outside git and are identified by path, size and hash: **37.71 GB of journals,
129.26 GB of stores, 2.38 GB of verified archives, 0.08 GB of latency artifacts** under
`/home/hr/p13-corpus-6/`.

## Unchanged

* **Real own-fill durable record: UNRUN / P14.**
* **Real maker fraction: UNRUN / P14.**
* **Real taker-fill persistence: UNRUN / P14.**
* **Real nonzero own-ledger settlement analytics: UNRUN / P14.**
