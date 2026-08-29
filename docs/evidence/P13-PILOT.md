# P13 — live paper: composition root and pilot

**Provenance: `REAL_PUBLIC_MARKET_DATA`.** Three consecutive real `btc-updown-5m` markets, real
Polymarket books, real Binance BTC, real Polygon settlement reads, one process, no restart.

**No order. No credential. No chain write.** `LIVE_TRADING_ENABLED` and `REDEMPTION_ENABLED` are
both `False`. Execution is P7 in shadow throughout; every resting order is a `SHADOW_ORDER`, every
queue figure a `SHADOW_ESTIMATE`, and no `OwnFill` exists because none has happened.

**Date:** 2026-08-27 (UTC). This is the **implementation and pilot** record. The ≥200-market
corpus is a separate gate and is **in progress**; nothing here claims it.

## What was built

`maker5m.bot` was a docstring and a promise. It is now the composition root, and
`tools/p12_market.py` is what it always should have been — a tool that proved a wiring, not the
production bot.

| module | owns |
|---|---|
| `bot.config` | the frozen strategy configuration, hashed; the OPERATIONAL thresholds |
| `bot.session` | one market, every plane, one identity |
| `bot.supervisor` | the market cadence, prearm, the active-market designation, cold work |
| `bot.cold` | verification, replay and lzma, in a child interpreter |
| `bot.settle` | P10's settlement watch, on a thread, after its own market |
| `bot.corpus` | one appended line per attempted market, fsynced, never rewritten |
| `bot.quality` | P8's L3 classifications, counted by side, phase, time and event source |
| `bot.resources` | RSS, threads, file descriptors, pending tasks, per market |
| `bot.runner` | the entry point, with no flag that could send an order |

Nothing here re-implements a plane. P6's pipeline, P7's shadow executor, P8's instrumentation and
classifier, P9's risk, P10's settlement, P11's worker and store and P12's publisher and control
ingress are composed, not copied.

### Threads and tasks

```text
main thread        asyncio loop: the supervisor, both live sessions, cold coroutines
ingress owner      the loop's consumer: reduce, decide, risk, shadow reconcile, capture
persistence        one per session: P11 worker, its SQLite connection, P8 analyzer, L3 counting
ui bridge          one per process: the only thing that lists, reads or unlinks a command file
cold interpreter   spawned: verify_store, P5 replay, lzma archive
```

### Two markets at once, on purpose

A capture opens its feeds at T0-30 and runs to T0+305 — five seconds *past* the next market's T0.
Sessions therefore overlap, and the first version of the supervisor awaited each one before
looking at the next, which would have skipped every second market forever. Sessions are now
launched seventy-five seconds before their own T0 while the previous one is still trading.

Nothing is "the current market". Every object belongs to one slug, and operator commands follow a
single designated active market, flipped at the handoff by the market's own clock rather than by
whichever session's tick ran first. A session that is not active takes no command and leaves it
queued for the one that is.

### Cold work never touches a trading path

Measured on a real journal: decode 10.8 s, replay 3.2 s, encode 4.7 s, and the archive is another
45 s of lzma. A thread would hold the GIL through all of it, and the thread it would steal from is
consuming a live book. So verification, replay and compression run in a **spawned** child
interpreter — spawned rather than forked, because forking a process that holds an open SQLite
connection and two live websockets copies exactly the state a child must not inherit.

Discovery is blocking urllib and runs in a thread, well before T0. Called from the event loop it
would stall the ingress consumer — the failure P12B spent a round removing, by a different route.

## Pilot — three consecutive real markets

One process, launched 12:15 local, no restart, no intervention.

```text
[12:18:45] btc-updown-5m-1787826000 launched (prearm lead 74.9s)
[12:23:45] btc-updown-5m-1787826300 launched (prearm lead 74.9s)
[12:25:13] btc-updown-5m-1787826000 closed: 143,740 decisions, 0 dropped, 0 sink errors
[12:28:45] btc-updown-5m-1787826600 launched (prearm lead 74.9s)
[12:30:13] btc-updown-5m-1787826300 closed: 154,465 decisions, 0 dropped, 0 sink errors
[12:35:10] btc-updown-5m-1787826600 closed: 205,851 decisions, 0 dropped, 0 sink errors
```

Market 1 settled while markets 2 and 3 were trading, which is the overlap the design is for.

| | 1787826000 | 1787826300 | 1787826600 |
|---|---|---|---|
| verification | COMPLETE | COMPLETE | COMPLETE |
| replay | EXACT | EXACT | EXACT |
| decisions | 143,740 | 154,465 | 205,851 |
| risk records | 143,740 | 154,465 | 205,852 |
| drops / gaps / sink errors | 0 / 0 / 0 | 0 / 0 / 0 | 0 / 0 / 0 |
| PLACE by risk state | SAFE 548 | SAFE 527 | SAFE 748 |
| settlement | RESOLVED DOWN | RESOLVED DOWN | RESOLVED DOWN |
| archive | 751.9 MB → 13.7 MB (54.9×) | verified | verified |

**PLACE while HALTED: 0. PLACE while RECOVERING: 0**, across 172 HALTED and 3 RECOVERING risk
records — the opening moments of each market, where P9 correctly refuses to act until health is
established.

Totals over the three: 504,056 decisions, 504,057 risk records, 486,028 CLOB messages, 26,404 BTC
messages, **0 drops, 0 gaps, 0 sink errors**, 3/3 journals replaying exactly and re-encoding to
the bytes that were hashed.

### The state machine obeyed its own thresholds

Market 1, from its own recorded decisions:

```text
PREARM     offset   0.017s   ordinal      2
QUOTE      offset   3.001s   ordinal  1,799
ENDGAME    offset 240.001s   ordinal 140,303
SETTLING   offset 280.001s   ordinal 144,706
DONE       offset 300.000s   ordinal 146,385
```

No threshold was changed. P13 measures whether the composition obeys them, and it does, to the
millisecond the phase producer fires.

### L3 classification

P8's classifier, aggregated by side, phase, time bucket and event source. There is one classifier
in this project and this is not a second one.

```text
AT_FRONT            35,846   33.3%
PRICE_OK_BUT_DEEP   13,610   12.6%
NOT_QUOTING         58,330   54.1%
OFF_PRICE                0    0.0%
STALE                    0    0.0%
```

Non-degenerate, market-sensitive and continuously measured: the per-market `AT_FRONT` fraction
ranges 25.7 % to 36.8 % across three markets, and the by-bucket table moves through the market.
`NOT_QUOTING` is explained rather than lumped — `POST_ONLY_BLOCK` 43,195, `QUOTING` 49,456,
`CENTRE_UNAVAILABLE` 5,164, `OFF_VENUE_TICK` 3,653, `PHASE_NOT_QUOTING` 2,732, `ENDGAME_GATE`
2,272, `NO_LIVE_ORDER` 1,314.

Shadow queue-ahead across the three markets: p50 **0**, p75 4.29 M, p90 94 M, p95 166 M units,
max 1.24 G. **`SHADOW_ESTIMATE`**, not a venue queue position: P8's model of where our order would
sit given the public book, with the bias documented in `telemetry/queue_estimate.py` and not
corrected here. No own order was sent, so nobody told us where we were.

`STALE` is 0.0 across all three. That is P6's verdict, carried through P8, and it is a
*measurement of three markets on a healthy connection*, not a claim about the world. Zero here is
also exactly what a broken health path would produce, which is why the OPERATIONAL floor exists
and why the distribution — not a threshold — is the result.

### Resources

```text
                first market start        last market end
RSS                     37.5 MB                 1,251.5 MB
threads                       3                          7
open fds                     14                         16-21
pending tasks                 3                       2-9
```

RSS growth is the recorded event stream: every step with its complete `DecisionResult`, roughly
two hundred megabytes for a busy market, held until the journal was written and the cold path
finished. The pilot found it, and the stream is now released as soon as the journal is on disk
rather than being held through a settlement watch that can run for minutes. Whether the resulting
plateau holds is a question for the corpus run, and the answer will be in its per-market trace
rather than asserted here.

## Cost to the trading path

The accepted process-isolated benchmark, four alternated pairs on a real capture
(`p13-overhead.json`):

| P8C limit | P13 | |
|---|---|---|
| decide p50 overhead ≤ 1,000 ns | +249 ns | MET |
| decide p50 overhead ≤ 3 % | +1.06 % | MET |
| full-cycle p50 overhead ≤ 5,000 ns | +1,926 ns | MET |
| full-cycle p50 overhead ≤ 5 % | +3.75 % | MET |

No limit was moved. The composition adds two dict increments per cycle to the ingress owner — the
risk-state tally and the first-seen phase — and the L3 aggregation runs on the persistence
worker's thread, off the path, fed by an observer P8 already had the data for. From the corpus run
onwards each market also records the measured tiers of its own `observe` cycle, so the hot-side
cost is reported from real markets rather than only from a replay. The pilot's three entries
predate that field.

## What this does not claim

* **Not the corpus gate.** Three markets are a pilot. The ≥200-market corpus is separate and is in
  progress.
* **No OPEN item is closed.** O01 through O09 are untouched. `AT_FRONT` rates and queue
  distributions are evidence for P15's experiments, not answers to them.
* **No strategy value changed.** One frozen configuration, hashed into every corpus entry
  (`86ecaad7…` for the pilot epoch), and the frozen strategy checksums are unchanged.
* **No paper PnL.** The authoritative own ledger is empty because there are no own fills. Real
  own-fill records, maker fraction, taker-fill persistence and nonzero own-ledger economics remain
  **UNRUN / P14**.

## Artifacts

| file | what |
|---|---|
| `p13-pilot-corpus-index.jsonl` | the three corpus entries, exactly as the collector appended them |
| `p13-pilot-corpus.json` | the aggregate report over them |
| `p13-overhead.json` | the accepted overhead benchmark, run after the pilot |

Journals, stores and archives live outside git at `/home/hr/p13-pilot/markets/`, identified by
SHA-256 in the corpus entries. A 217 MB journal and a 752 MB store per market do not belong in
history; their hashes do.
