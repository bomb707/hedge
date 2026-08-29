# P13C — final corpus foundation

**Provenance: `REAL_PUBLIC_MARKET_DATA`.** The one induced fault is a local process termination on
a real market, labelled **`CONTROLLED_LOCAL_FAULT_ON_REAL_MARKET`**: our own collector was killed.
The Polymarket, Binance and Polygon feeds were real and healthy throughout, and no venue incident
is implied.

**No order. No credential. No chain write.** `LIVE_TRADING_ENABLED` and `REDEMPTION_ENABLED` are
both `False`.

**Date:** 2026-08-27 (UTC). Six corrections to the collection infrastructure, before a corpus is
collected on top of it.

## `p13-corpus-2` stopped and superseded

Stopped by `SIGTERM` to PID 3958641, after confirming the PID still belonged to this root's
collector (`/proc/3958641/cwd` → the repository). Twelve attempts launched, **ten durable rows,
all COMPLETE and replay-EXACT**; two in flight with no row — which is itself one of the reasons
the epoch cannot count.

Preserved entire at `/home/hr/p13-corpus-2/`, marked `SUPERSEDED_FOR_FINAL_P13_ACCEPTANCE` in
`EPOCH_STATUS.md`, inventoried with per-market hashes in
`docs/evidence/p13-corpus-2-superseded.json`. Nothing deleted, nothing rewritten.

## A — the live latency died with the market

P8's analyzer holds the real distributions and they were released with the session. The corpus
kept `hot_path_observe_ns`, which mixes triggering kinds and is not P8's per-trigger contract, so
the one measurement a live paper phase exists to produce was being discarded every five minutes.

**A replay cannot stand in for it.** Re-deriving decisions from a journal measures this machine
today — no sockets, no contention, warm caches. That is a useful number and it is not the latency
the market was traded at.

`receive_to_reconcile` is now split by trigger as well, derived downstream from timestamps the
observation already carries. No clock is read on Plane 1 and the existing aggregate keeps its
meaning. CLOB and spot are never merged: they arrive at different rates, through different
sockets, doing different work.

Each market writes one immutable `<slug>.latency.json.xz` on the cold path, **before** eligibility
is decided, holding every raw sample. Not a sketch — quantiles do not merge, and a corpus p99
taken as the p99 of per-market p99s discards the tail it exists to describe. The row records path,
size and SHA-256 beside the slug, market id, revision and config hash; a missing artifact, one
that hashes differently, or one with samples for only one of the two triggers makes the market
ineligible and is named in the report rather than skipped.

## B — an attempt did not exist until it finished

The corpus records markets that *finished*. `p13-corpus-1` attempted fourteen and left twelve
rows; the two in flight existed nowhere. A phase whose contract is "retain every attempted market"
cannot have its ledger start after the risky part.

`attempts.jsonl` sits beside the corpus. `ATTEMPT_STARTED` — slug, T0, epoch, config hash,
revision, tree, cleanliness, run mode, prearm, and the paths its journal, store and latency
artifact will take — is fsynced **before** the session is launched, and **if it cannot be written
the market is not launched**. Failing closed costs one five-minute window; failing open costs the
ability to say what the collector did.

Terminal records are appended beside it, never over it. A start with no terminal event is what a
process dying mid-market looks like, and the next start-up appends `ABORTED_PREVIOUS_PROCESS`,
inventories whatever survived, and counts it toward nothing.

## C — the cold cap could still be exceeded

`len(cold) < cap` was checked at launch while an already-running session owed a finalisation it
had not reserved. With a cap of three the count could reach four: two live sessions plus a full
cold queue. A launch check that a running market can walk past is not a bound.

A market now takes a **lifecycle slot before it is launched** and holds it until its terminal
record and corpus row are written, so its own transition to cold moves nothing. The cap is six —
two live or warming sessions and up to four finalisations, since a settlement watch can run four
hundred seconds before verification, replay and compression begin. Waiting happens to markets that
have not started; a running session never waits on a closed one. A slot that cannot be reserved
before the last safe launch moment is recorded as `COLD_CAPACITY_UNAVAILABLE`.

## D — readiness was "ready once", not "ready at T0"

The warm milestones are first-write-wins, so a book ready at T0-29 that disconnected at T0-10 kept
its milestone and its 29-second lead for ever. P6 now also tracks *current* warm validity: a
disconnect clears it, a recovery starts a new lead from the recovery, and the state is snapshotted
at the warming-to-live transition. `feed_ready_before_t0` reads from that snapshot — both feeds
currently valid at the boundary. The first-seen milestones are retained beside it as diagnostics
and are not reinterpreted.

Four tests drive the cases: dropped and not recovered is not warm; recovered at T0-4 is warm with
a **four**-second lead, not twenty-eight; the same for spot at T0-1; and a capture that never
reached T0 makes no readiness claim at all.

Observational only — no event ordering, health semantic, warm-message application, T0 boundary,
strategy, risk or execution change, and the P6 regressions are green.

## E — dirty exploratory rows could later qualify

`git rev-parse HEAD` does not move when tracked files are edited, so a run started with
`--allow-dirty` writes rows carrying the same revision as a clean one, and the next clean restart
would have counted them. Qualification now requires `working_tree_clean`, `run_mode ==
ACCEPTANCE_CLEAN` and, when supplied, the tree hash. A dirty market still runs, persists, replays
and verifies — it simply says in its own row that it is not final empirical evidence. 100 clean
rows and 20 dirty ones at the same HEAD qualify **100**.

## F — the settlement knobs were hashed and ignored

`settle_timeout_s` and `settle_poll_s` changed the config hash while `settle_market` used its
module constants, so the identity described a configuration nothing ran. They are passed through
now, and a test asserts the runtime call receives 17 and 0.25 when the config says so.

## The corrected pilot — `p13c-pilot-1`

Four consecutive real markets, one process, no restart, clean tree at `9ee53c0`.

| | 1787845500 | 1787845800 | 1787846100 | 1787846400 |
|---|---|---|---|---|
| attempt | started → finished | started → finished | started → finished | started → finished |
| store / replay | COMPLETE / EXACT | COMPLETE / EXACT | COMPLETE / EXACT | COMPLETE / EXACT |
| decisions | 95,076 | 189,026 | 203,867 | 167,600 |
| classified = actions = 2× | 190,152 | 378,052 | 407,734 | 335,200 |
| CLOB ready **at T0** | yes, 29.75 s | yes, 29.64 s | yes, 29.78 s | yes, 29.77 s |
| spot ready **at T0** | yes, 28.93 s | yes, 29.01 s | yes, 28.71 s | yes, 28.76 s |
| CLOB / BTC messages | 91,101 / 6,070 | 186,572 / 5,382 | 202,408 / 4,910 | 165,623 / 4,840 |
| latency artifact | 349 KB | 640 KB | 645 KB | 503 KB |
| CLOB / spot samples | 8,911 / 594 | 18,392 / 529 | 19,888 / 517 | 16,287 / 461 |
| drops / gaps / sink | 0 / 0 / 0 | 0 / 0 / 0 | 0 / 0 / 0 | 0 / 0 / 0 |
| PLACE by risk state | SAFE 169 | SAFE 726 | SAFE 860 | SAFE 575 |
| settlement | RESOLVED | RESOLVED | RESOLVED | RESOLVED |

Lifecycle high-water **2 of 6**; cold backlog high-water 1; corpus append failures 0; every row
joins exactly one attempt, and every attempt has exactly one terminal record.

**PLACE while HALTED: 0. PLACE while RECOVERING: 0.**

### The merged live latency

Exact quantiles over the concatenated raw samples of all four markets — not the quantile of
per-market quantiles:

```text
                              n        p50        p95        p99        max
CLOB receive → decide      63,478   112.6 µs   620.0 µs   862.3 µs   21.08 ms
CLOB receive → reconcile   63,478   130.0 µs   706.0 µs   971.7 µs   21.10 ms
spot receive → decide       2,101    86.3 µs   432.0 µs   606.4 µs    5.22 ms
spot receive → reconcile    2,101   103.7 µs   518.6 µs   719.8 µs    5.24 ms
decide_duration            65,595    28.8 µs   152.3 µs   211.1 µs   21.01 ms
prepare_duration           65,595     9.9 µs    52.4 µs    82.6 µs    9.66 ms
reconcile_duration         65,595     6.6 µs    34.9 µs    45.8 µs    5.52 ms
hot_path_observe          655,569    23.7 µs   123.3 µs   163.1 µs   16.33 ms
```

Latency is **sampled**, under P8's accepted policy; classification and actions are **exhaustive**.
Per-market `hot_path_observe` maxima are preserved: 6.9, 16.3, 16.0 and 15.5 ms. **No new latency
threshold is proposed here.** The P13B 426 ms outlier stands as historical evidence, and the
≥200-market corpus will say how often anything like it happens.

### Controlled restart on a real market

**`CONTROLLED_LOCAL_FAULT_ON_REAL_MARKET`.** A real market was started, its `ATTEMPT_STARTED`
record was confirmed durable, and the collector was terminated three minutes into trading. Nothing
about the venue was faked; we killed our own process.

```text
ATTEMPT_STARTED           btc-updown-5m-1787847300  0f1c4a6c  pid 4189078
  -- collector terminated mid-market, 432 MB store on disk, no corpus row --
ABORTED_PREVIOUS_PROCESS  btc-updown-5m-1787847300  0f1c4a6c  ABORTED  eligible false
                          orphan_artifacts: btc-updown-5m-1787847300.p11.sqlite3
ATTEMPT_STARTED           btc-updown-5m-1787847600  d4e7b834
ATTEMPT_FINISHED          btc-updown-5m-1787847600  d4e7b834  COMPLETE  eligible true
```

The restarted collector found the abandoned attempt, closed it off explicitly, inventoried its
orphaned store rather than deleting it, counted it toward nothing, and collected the next market
normally.

## Cost to the trading path

The accepted process-isolated benchmark, after the pilot (`p13c-overhead.json`):

| P8C limit | P13C | |
|---|---|---|
| decide p50 overhead ≤ 1,000 ns | −304 ns | MET |
| decide p50 overhead ≤ 3 % | −1.14 % | MET |
| full-cycle p50 overhead ≤ 5,000 ns | +1,073 ns | MET |
| full-cycle p50 overhead ≤ 5 % | +1.84 % | MET |

No limit was moved. Observation-buffer high-water across the pilot was 156 to 2,825 of 320,000,
and **no market dropped an observation**. Latency artifacts compress to 349–645 KB and are written
on the cold path. All new work is Plane 3; there is no synchronous file I/O on the hot path.

## Retained and superseded

* `p13-corpus-1`, `p13-corpus-2`, the P13 v1 pilot and the P13B pilots are **all retained
  unedited**, with supersession recorded beside them rather than in them.
* None of them counts toward the ≥200 acceptance corpus, which runs under a new epoch from one
  clean source revision.

## Unchanged

* **Real own-fill durable record: UNRUN / P14.**
* **Real maker fraction: UNRUN / P14.**
* **Real taker-fill persistence: UNRUN / P14.**
* **Real nonzero own-ledger settlement analytics: UNRUN / P14.**

P13C closes no OPEN item, proposes no threshold, and changes no strategy value.
