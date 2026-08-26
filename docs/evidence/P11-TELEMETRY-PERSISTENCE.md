# P11 — durable telemetry persistence

> **SUPERSEDED FOR FINAL P11 ACCEPTANCE.** Everything below happened and is not withdrawn: the
> SQLite thread-ownership bug, the `RawCentre` serialization bug and the closing-store ownership
> bug were all real and are recorded here because finding them is what the exercise was for, and
> the ~850 MB footprint measured here is the number the storage work started from.
>
> It does not constitute P11's real-market acceptance, for five reasons found by independent
> review: P9's health overlay was never actually wired into execution; risk durability would
> accept a truncated prefix; the canonical `FillRecord` had no path to storage; `event_id` was
> synthetic; and the durable footprint had not been engineered. The accepted evidence is
> **[P11B](P11B-PERSISTENCE-INTEGRITY.md)**.

**Provenance: `REAL_PUBLIC_MARKET_DATA`,** except the stalled-sink market, which is labelled
`CONTROLLED_LOCAL_FAULT_ON_REAL_MARKET`, and the overhead benchmark, which is
`REPLAY_OF_REAL_CAPTURE`. Every market below is a real Polymarket `btc-updown-5m` with a real
BTC spot feed and real Polygon settlement. No synthetic market appears in any acceptance gate.

**No order was placed. No credential was requested. No redemption transaction exists.**
`LIVE_TRADING_ENABLED` is `False`; `REDEMPTION_ENABLED` is `False`.

**Capture date:** 2026-08-26 (UTC).

## What this closes, and what it does not

| Claim | Status |
| --- | --- |
| Every real decision is durably persisted | **CLOSED** — 171,467 of 171,467 |
| Persistence never blocks the trading path | **CLOSED** — measured, healthy and stalled |
| A stalled sink loses telemetry and nothing else | **CLOSED** — controlled fault on a real market |
| Drops and gaps are exact and visible | **CLOSED** |
| An incomplete market cannot verify as complete | **CLOSED** |
| Real own-fill durable record | **UNRUN / P14** |
| Real maker fraction | **UNRUN / P14** |
| Real taker-fill persistence | **UNRUN / P14** |
| Real nonzero own-ledger settlement analytics | **UNRUN / P14** |

The bot has still never placed an order, so every ledger is empty and every fill record in the
suite is constructed. The schemas are exercised; the economics are not.

## Architecture

```
PLANE 1/2   event -> reduce -> decide -> prepare -> reconcile -> capture -> RETURN
                                                                    |
                                                          bounded deque, non-blocking
                                                                    |
PLANE 3     one thread: drain -> project -> analyze -> batch -> SQLite -> manifest -> verify
```

One hot append per decision, extended from P8's existing capture rather than added beside it.
Six slots were appended — `DecisionTelemetry`, `BookUpdate`, `SpotTick`, the event timestamp,
the venue timestamp when one exists, and P9's recorded verdict. Every one is a reference to a
value the cycle already built and which is immutable, so the whole of Canonical §25 becomes
available downstream for six pointer stores and no recomputed economics. Existing P8 indices are
untouched, so the analyzer reads the stream it always did.

The producer uses `collections.deque`, not `queue.Queue`: `put` takes a lock the consumer holds,
and I19 forbids Plane 1 waiting on anything Plane 3 controls. That rests on `deque.append` and
`deque.popleft` being individually atomic under **CPython 3.12** — documented, relied on by
`queue.Queue` itself, and a CPython property rather than a language guarantee, so it is
exercised under sustained two-thread load rather than asserted.

Storage is standard-library SQLite, one connection owned by one thread for its whole life,
batched 500 rows per transaction, `PRAGMA user_version` checked on open.

## Performance: stalling Plane 3 does not slow Plane 1

12,000 real captured events from `btc-updown-5m-1787647500`, four alternated triples, each
configuration alone in a fresh interpreter. P8C's method, unchanged and for its reasons.

| Metric | off p50 | healthy p50 | stalled p50 | healthy Δ | stalled Δ |
| --- | ---: | ---: | ---: | ---: | ---: |
| decide | 22,387 | 22,233 | 22,035 | **−154 ns (−0.69%)** | **−352 ns (−1.57%)** |
| full cycle | 36,802 | 36,618 | 36,437 | **−184 ns (−0.50%)** | **−365 ns (−0.99%)** |
| receive→reconcile | 14,282 | 14,223 | 14,161 | −59 ns | −121 ns |

p95 / p99, nanoseconds:

| Metric | off | healthy | stalled |
| --- | --- | --- | --- |
| decide | 25,586 / 71,770 | 26,648 / 73,377 | 25,691 / 71,169 |
| full cycle | 43,303 / 87,639 | 50,473 / 91,082 | 47,819 / 87,479 |
| receive→reconcile | 16,497 / 29,349 | 17,039 / 36,483 | 16,551 / 32,005 |

Every p50 overhead is **negative** — inside this machine's noise — for a healthy sink and for a
deliberately stalled one.

### The P8C gate, unchanged

| Limit | Target | P8C | P11 healthy | Verdict |
| --- | --- | ---: | ---: | --- |
| Full-cycle p50 overhead | ≤ 5,000 ns | 955 | **−184** | **MET** |
| Full-cycle p50 overhead | ≤ 5 % | 2.9 % | **−0.50 %** | **MET** |
| Decide p50 overhead | ≤ 1,000 ns | 454 | **−154** | **MET** |
| Decide p50 overhead | ≤ 3 % | 1.73 % | **−0.69 %** | **MET** |

No limit was moved.

### It did not start there

The first honest measurement was **+5.3 % on full-cycle p50** — past the limit. Encoding one
decision record cost **92 µs**, on a thread holding the GIL: `dataclasses.asdict` recurses and
deep-copies every value, a recursive JSON pre-pass rebuilt the result, and `sort_keys` sorted it
again. A one-level walk over the fields costs **26 µs**, and `sort_keys` went with it — field
order comes from the dataclass and was already deterministic, so sorting re-established a
property the output already had. §37 says do not explain such a regression away, and it was not.

## Real market, healthy sink

`btc-updown-5m-1787748900`

| | |
| --- | --- |
| Strategy cycles | 171,467 |
| CLOB messages | 170,222 (4,140 books, 162,630 price changes, 1,962 trades) |
| BTC spot messages | 4,693 |
| Reconnects / malformed | 0 / 0 |
| **Decision records** | **171,467** |
| Risk records | 173,460 |
| Settlement records | 1 — resolved **UP** (payout `[1,0]`, block 92696216) |
| **Dropped** | **0** |
| **Sequence gaps** | **0** |
| **Sink errors** | **0** |
| Buffer high-water | **214** of 320,000 (0.07 %) |
| Batches / transaction time | 343 / 8.84 s |
| Database | 853,237,760 bytes, ~4,976 bytes per decision |
| **`telemetry_complete`** | **true** |
| **Verification** | **COMPLETE** — all 15 checks pass |

A high-water mark of 214 against a 320,000 bound is the continuous-drain property directly
measured: the market was never held in memory.

**Store size is a real operational finding.** ~5 KB per decision is roughly 850 MB per
five-minute market, or ~10 GB per trading hour. That is impractical to keep indefinitely and is
recorded here rather than buried: the payload column duplicates the indexed columns and could be
narrowed, or decisions could be column-stored, both of which are P12/P15 decisions about what
the data is *for*. Nothing was silently truncated to make the number look better.

## Real market, controlled local sink stall

`btc-updown-5m-1787749500` — **`CONTROLLED_LOCAL_FAULT_ON_REAL_MARKET`**

The Polymarket and BTC feeds were real and healthy throughout. The persistence consumer is ours
and we stopped it. **This is not a venue incident.** The buffer was deliberately reduced to
2,000 so overflow could be proven without wasting a market.

```
[+60s]  sink stalled (controlled local fault)
[+150s] sink resumed after 90s; 64416 market events processed during the stall
        persisted 87821 decisions, dropped 62428, gaps 1, sink errors 0
        settlement -> DOWN
```

| | |
| --- | --- |
| Stall window | +60 s → +150 s (90 s) |
| **Market events processed during the stall** | **64,416** |
| Strategy cycles | 150,249 |
| Accepted / persisted | 150,249 / 87,821 |
| **Dropped** | **62,428** (exactly accepted − persisted) |
| Sequence gaps | **1**, first at 44,487, last at 106,915 |
| Lost observations | **62,428** |
| Sink errors | 0 |
| Buffer high-water | 2,000 of 2,000 — the bound was reached |
| Hot-path `observe` p50 | 35,661 ns (baseline market: 41,834 ns) |
| **`telemetry_complete`** | **false** |
| **Verification** | **INCOMPLETE** |

```
failures:
  62428 records were dropped by the bounded buffer
  1 gap(s), 62428 observations lost
```

Trading continued for the entire stall, the sink resumed and wrote the rest without error, and
the market still settled. Telemetry was lost; nothing else was. **Persistence can fail without
stopping trading.**

## Completeness is load-bearing

A market is `telemetry_complete` only if it was explicitly closed *and* dropped nothing, gapped
nothing, lost nothing, hit no sink error, and persisted exactly what it accepted. Trading having
gone well is not evidence for any of that.

A crash therefore leaves `closed = 0` and the market reads INCOMPLETE, because closing is the
last write. An unfinished market can never be mistaken for a whole one.

**P15 must not use an incomplete market to close an open strategy item.** The stalled market
above is exactly the case that rule exists for: 150,249 real cycles, 87,821 of them recorded,
and no way to tell from the surviving records which 62,428 are missing.

## The durable artifacts

Not committed — 853 MB and 486 MB. The sidecar manifests are, and verification reproduces from
them.

| Market | Path | Bytes | SHA-256 (first 16) |
| --- | --- | ---: | --- |
| `…748900` | `/home/hr/p11-stores/btc-updown-5m-1787748900.p11.sqlite3` | 853,237,760 | `2d08378f1c7d676f` |
| `…749500` | `/home/hr/p11-stores/btc-updown-5m-1787749500.p11.sqlite3` | 486,252,544 | `2d8560c796626163` |

```
reproduce   .venv/bin/python tools/p11_market.py <out-dir>
            .venv/bin/python tools/p11_market.py <out-dir> --stall-from 60 --stall-to 150 --buffer 2000
            .venv/bin/python tools/p11_persistence_bench.py --journal <p6-journal> --limit 12000
```

A file cannot contain its own hash, so the digest lives in the sidecar rather than in the
manifest row: writing it in would change the file and invalidate it in the same act.

## Three defects the real market found that the unit tests could not

1. **The connection was opened on the wrong thread.** sqlite3 refuses a connection used from
   another thread, correctly — the ownership claim has to be true of the object, not just of the
   design. 4,000 sink errors and one row, while the unit tests passed because they drained on
   the main thread.
2. **A bare `except: sink_errors += 1` hid a real defect.** 1,789 records per run were failing
   to build and the only symptom was a counter that could equally have meant a full disk.
   Recording the description named it immediately: `RawCentre` has no `.price` — it is a
   rational, because the CLOB midpoint of an odd bid+ask sum is a genuine half unit that P3
   refuses to round early. Persisting it as an integer would have destroyed exactly what it
   exists to preserve.
3. **The closing writes repeated defect 1 in the tool**, so the first complete-looking market
   verified INCOMPLETE. The verifier caught it, which is the whole reason there is a verifier.

## Still not done

* **Real own-fill durable record: UNRUN / P14.** No order has been placed, so no venue fill
  exists to record. `FillProvenance` keeps `REAL_VENUE` and `SHADOW_MODEL` apart so that when
  one does exist it cannot be confused with a modelled one.
* **Real maker fraction: UNRUN / P14.** Stored as a numerator/denominator pair over real fills;
  there are none.
* **Real taker-fill persistence: UNRUN / P14.** P9 owns the halt; P11 would record the
  liquidity exactly as reported and repair nothing.
* **Real nonzero own-ledger metrics: UNRUN / P14.** Term1, Term2 and net PnL are exercised
  against constructed ledgers, which §43 permits, and the identity
  `term1 + term2 == gross_payout − total_cost` holds exactly. No real market has produced a
  nonzero one.

**No strategy open item was closed by P11.** O01–O09, O13 and O14 remain OPEN; O07 in
particular is untouched, and every reported PnL names the rebate view that produced it.
