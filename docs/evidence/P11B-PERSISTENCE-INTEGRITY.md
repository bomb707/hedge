# P11B — persistence integrity closure

**Provenance: `REAL_PUBLIC_MARKET_DATA`,** except the stalled market, which is
`CONTROLLED_LOCAL_FAULT_ON_REAL_MARKET`, and the overhead benchmark, which is
`REPLAY_OF_REAL_CAPTURE`. Real Polymarket CLOB, real BTC spot, real Polygon settlement.

**No order was placed. No credential exists. No chain write occurred.**
`LIVE_TRADING_ENABLED` is `False`; `REDEMPTION_ENABLED` is `False`.

**Capture date:** 2026-08-26 (UTC). Supersedes the real-market half of
[P11](P11-TELEMETRY-PERSISTENCE.md), which is retained rather than rewritten.

## The five gaps

| | Was | Is |
|---|---|---|
| **A** | risk evaluated against `HealthFrame()` at ordinal 0, and the *unadjusted* decision handed to the executor | P6's own health, the merger's real ordinal, `risk_adjust` before prepare |
| **B** | `FillRecord` existed; the worker counted fills and discarded them | `FillCapture` published through a bounded channel and durably stored |
| **C** | a risk run starting at 5,000 verified as contiguous | `risk_sequence[i] == i` from zero, plus explicit drop accounting |
| **D** | `event_id = f"{slug}-{capture_sequence}"` | P2's real `EventMeta.event_id` |
| **E** | 853 MB per five-minute market, deferred | lossless `lzma` archive, measured |

### A — the risk verdict was not the verdict

`HealthFrame()`'s defaults are not neutral. They say CLOB UNKNOWN, awaiting snapshot, SPOT
UNKNOWN, and P9 correctly refuses to trade on that. The runner passed one, at
`as_of_ingress_ordinal=0`, and then gave the executor the raw strategy decision. So the recorded
verdict described a market the runner had not looked at, at a point in the stream that no longer
existed, and did not govern the recorded action — three separate ways of not being the thing it
claimed to be.

It now follows the pattern P9 already accepted in `tools/risk_market.py`: health read from P6's
`StreamHealth`, evaluated at the merger's real ordinal against the event's own timestamp, and
`risk_adjust` applied before prepare and reconcile. A halt empties intent and the reconciler
plans CANCEL, which is P9's existing minimal-action rule rather than a second retreat path.
`StrategyEngine` is untouched, and P6 remains the sole staleness authority.

Raw strategy intent is captured alongside the post-risk intent, so a record can distinguish
**"the strategy declined to quote"** from **"the strategy wanted to quote and safety refused"**.
`DecisionRecord` is therefore **V2**, bumped rather than reinterpreted: a V1 row still means what
it meant when it was written.

### C — the load-bearing invariants

A `PLACE` creates new risk, so the verifier now refuses a market where one was recorded against a
verdict that forbade it, or against a `risk_sequence` that is not stored. `CANCEL` under a halt
stays allowed: a halt withdraws quotes, it must not trap us in them.

Storage order is now provable. Each typed table had its own primary key and nothing could show
the *combined* order had no hole; a `persistence_log` envelope records one row per stored record
in a single total order, so a missing prefix, a gap, or a duplicate across two different tables
all appear as a break in one contiguous run. It remains storage order, not causality —
`ingress_ordinal`, `risk_sequence` and the settlement block are unchanged.

Manifest bounds were previously stored and never checked, which is documentation rather than
integrity. First/last persistence sequence and first/last ingress ordinal are now verified
against what is actually in the file.

### E — the footprint, measured on the real file

On the 853,237,760-byte baseline store from P11:

```text
raw           853,237,760
gzip -6        21,381,697   40x    3.7 s
zstd -19       13,965,457   61x   65.8 s
xz / lzma -6   11,167,576   76x    8.5 s
```

`lzma`: best ratio by a wide margin, seconds of a background thread once per market, and in the
standard library — no service, no dependency, no format nobody can open in five years. Whole-file
rather than per-row, which is why the ratio is extreme: consecutive decision records are nearly
identical and a whole-file window sees that where a per-row encoder cannot.

**Lossless, and nothing was dropped to get there.** No sampling, no field removal, no rounding,
no float. The archive is decompressed and hashed before `verified` is set, and this module never
deletes a raw store without that check passing.

Measured on the fresh baseline market:

| | Raw | Archived |
|---|---:|---:|
| One market | 600,547,328 B | **10,988,132 B** |
| Per decision | 5,230 B | **95.7 B** |
| 200-market P13 corpus | 120.1 GB | **2.20 GB** |
| 24 h continuous (288 markets) | 173.0 GB | **3.16 GB** |

Compression 44.3 s; restore-and-hash 1.73 s. Both on a Plane-3 thread after the market has
closed, and neither touches the trading path.

**Is it practical?** 2.2 GB for the P13 corpus and 3.2 GB per trading day is, on any ordinary
disk. The cost is that a closed market must be decompressed before it can be queried, which takes
a couple of seconds — `tools/p11_query.py` does exactly that and is the supported P12/P15 read
path, exercised here rather than promised.

## Performance

12,000 real captured events, four alternated triples, each configuration alone in a fresh
interpreter. **P9 runs in every configuration including `off`** — charging the risk evaluation
itself to persistence would inflate P11's number with work production performs regardless, which
is the mistake P8's first benchmark made three times with simulation. The ON path includes event
capture, real risk evaluation and overlay, risk publication and decision publication.

| Metric | off p50 | healthy p50 | stalled p50 | healthy Δ | stalled Δ |
|---|---:|---:|---:|---:|---:|
| decide | 22,884 | 23,212 | 23,184 | +328 ns (+1.43 %) | +300 ns (+1.31 %) |
| full cycle | 40,822 | 41,131 | 41,269 | +309 ns (+0.76 %) | +447 ns (+1.09 %) |
| receive→reconcile | 17,321 | 17,376 | 17,522 | +55 ns | +201 ns |

p95 / p99 (ns):

| Metric | off | healthy | stalled |
|---|---|---|---|
| decide | 28,136 / 66,328 | 27,495 / 68,296 | 27,101 / 66,643 |
| full cycle | 55,929 / 88,378 | 64,472 / 94,869 | 55,852 / 88,348 |
| receive→reconcile | 21,324 / 60,401 | 21,414 / 61,278 | 21,054 / 60,454 |

### The P8C gate, unchanged

| Limit | Target | P8C | P11B healthy | Verdict |
|---|---|---:|---:|---|
| Full-cycle p50 overhead | ≤ 5,000 ns | 955 | **+309** | **MET** |
| Full-cycle p50 overhead | ≤ 5 % | 2.9 % | **+0.76 %** | **MET** |
| Decide p50 overhead | ≤ 1,000 ns | 454 | **+328** | **MET** |
| Decide p50 overhead | ≤ 3 % | 1.73 % | **+1.43 %** | **MET** |

Stalled sits within noise of healthy: **stalling Plane 3 does not slow Plane 1.** No limit moved.

## Real market — healthy sink

`btc-updown-5m-1787770200`

| | |
|---|---|
| Decisions persisted | **114,823** |
| Risk records persisted | **114,823** — continuously, not dumped at DONE |
| Fills | 0 (no order has ever been placed) |
| Settlement | 1 — resolved **UP** |
| Drops / gaps / sink errors | **0 / 0 / 0** |
| Risk accepted / persisted / dropped | 114,823 / 114,823 / **0** |
| Buffer high-water | 230 of 320,000 |
| Storage order | **1 … 229,647**, exact, no duplicate |
| Risk sequence | **0 … 114,822**, exact from zero |
| Decisions with no real event id | **0** |
| `telemetry_complete` | **true** |
| Verification | **COMPLETE** — all 25 checks |
| Archive | 600,547,328 → 10,988,132 B (54.7×), restore verified |

Read back **out of the compressed archive**, not from the runner's counters:

```text
places_by_risk_state   {"SAFE": 532}
risk_states            {"SAFE": 114726, "HALTED": 96, "RECOVERING": 1}
```

**PLACE while HALTED: 0. PLACE while RECOVERING: 0.** The 96 HALTED and 1 RECOVERING records are
the legitimate startup window before P6 had established health — P9 behaving exactly as designed,
and now visible because the runner is finally asking it real questions.

## Real market — controlled local sink stall

`btc-updown-5m-1787771100` — **`CONTROLLED_LOCAL_FAULT_ON_REAL_MARKET`**. The Polymarket and BTC
feeds were real and healthy throughout; the persistence consumer is ours and we stopped it. **Not
a venue incident.** The buffers were reduced to 2,000 so overflow could be proven without wasting
a market.

```text
[+60s]  sink stalled (controlled local fault)
[+150s] sink resumed after 90s; 48296 market events processed during the stall
        persisted 65438 decisions, dropped 46718, gaps 4, sink errors 0
        settlement -> DOWN
        archived 341,766,144 -> 6,244,884 bytes (54.7x) verified=True
```

| | |
|---|---|
| Market events during the stall | **48,296** |
| Decision drops | **46,718**, 4 gaps, first at 23,186, last at 69,909 |
| Risk accepted / persisted / **dropped** | 112,156 / 64,922 / **47,234** |
| Sink errors | 0 |
| `telemetry_complete` | **false** |
| Verification | **INCOMPLETE** |

```
risk sequence spans 0..112155 but holds only 64922 of 112156
516 decision(s) name a risk_sequence that is not stored
47234 risk record(s) were dropped
112156 risk records accepted, 64922 persisted
46718 records were dropped by the bounded buffer
4 gap(s), 46718 observations lost
```

The 516 dangling references are the cross-record check catching **real** loss rather than a
constructed one. Trading and risk were untouched by the telemetry failure: PLACE still occurred
only under SAFE (327), never under HALTED or RECOVERING. Storage order for what *was* written
remains exact at 1…130,361 — the file is coherent about being incomplete.

## Still not done

* **Real own-fill durable record: UNRUN / P14.** The path is complete and exercised by
  constructed captures; no venue fill exists to travel it.
* **Real maker fraction: UNRUN / P14.**
* **Real taker-fill persistence: UNRUN / P14.** P9 owns the halt; P11 records liquidity exactly
  as reported and repairs nothing.
* **Real nonzero own-ledger settlement analytics: UNRUN / P14.**

No mock upgrades any of these, and **P11 closes no strategy open item.** O07 remains OPEN.
Incomplete telemetry — the stalled market above — may not be used by P15 to close one.
