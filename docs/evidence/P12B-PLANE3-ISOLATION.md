# P12B — Plane-3 isolation closure

**Provenance: `REAL_PUBLIC_MARKET_DATA`** for the market, `REPLAY_OF_REAL_CAPTURE` for the
overhead benchmark. The bridge stall is a `CONTROLLED_LOCAL_FAULT_ON_REAL_MARKET`.

**No order. No credential. No chain write.** `LIVE_TRADING_ENABLED` and `REDEMPTION_ENABLED` are
both `False`, and no endpoint in P12 can change either.

**Capture date:** 2026-08-27 (UTC). Supersedes the architectural half of
[P12](P12-UI-CONTROL-PLANE.md), which is retained.

## Defect A — filesystem I/O was on the ingress loop

`on_tick` runs inside the single ingress consumer, and P12's first version called `inbox.drain()`
and `publisher.maybe_publish()` from it. Neither waited on the UI process — and neither needed to.

**"Does not wait for the UI process" is not "cannot block the trading loop."** A `listdir`, a
`stat`, a `read_text`, a `mkstemp` or a `rename` can stall on the *filesystem* with no UI
involved at all: a network mount hiccuping, a full disk, a device queue behind an fsync. Every
one of those would have been paid by a decision cycle. I19 is about latency, so wrapping the
calls in `try` would have changed nothing.

```
BEFORE   on_tick -> glob, stat, read_text, unlink, mkstemp, write, rename
AFTER    on_tick -> deque.popleft()
```

`CommandBridge` is now the only thing in P12 that lists, stats, reads, decodes or unlinks a
command file, and the only thing that writes a snapshot. It owns a thread. The ingress owner pops
already-decoded immutable commands from a bounded deque: no syscall, no serialization, no lock.

**Overflow does not drop.** Telemetry may lose a record; a safety command may not. A full hot
channel refuses the push, the bridge leaves the file on disk, and the next pass retries — the
deferral is counted and the command is still there.

The read model is single-owner again. `on_tick` had been mutating `publisher.counters` and
appending to its command list while the persistence worker owned `latest` — a read model
maintained by two threads by accident, reachable from Plane 1. Everything now crosses into it as
an immutable message through one bounded inbox, drained where the snapshot is built.

### Proved by removing the filesystem

The tests replace `Path.glob`, `iterdir`, `read_text`, `write_text`, `unlink`, `mkdir`, `stat`,
`replace`, `open`, `exists`, `os.listdir`, `os.replace` and `tempfile.mkstemp` with functions
that raise, then run the actual hot-side control poll and risk evaluation.

Deliberately **not** a source scan of `maker5m.ui`: every module there was already clean. The
defect was in the runner, calling those clean functions from the wrong thread — a package scan
would have passed while the bot was doing `listdir` in `on_tick`.

## Defect D — command identity was not durably linked

A `RiskRow` records that an `OPERATOR_CONTROL` signal happened. It cannot say which command
caused it, and P11's accepted V1 row shape must not be reinterpreted to make room.

So the link lives in its own `control_audit` table, with `command_id` as a **column**. Not parsed
out of `RiskSignal.detail`: a load-bearing cross-reference that depended on a log line's
formatting would be one bad f-string away from unverifiable.

The store is **V3**. Adding a table while leaving the version alone would make V2 mean two
different things depending on which build last opened it — the defect P11 closed for decision
records. Reading accepts `{2, 3}`, so every accepted P11 archive stays readable; writing is
current-version only, so a V2 store opened for writing is refused rather than silently upgraded.

The verifier cross-links **both directions**: no accepted command may name a risk row that is
absent or that disagrees about the ingress ordinal, signal flag, resulting state or
`allows_place`; and no `OPERATOR_CONTROL` row may exist that no command claims. A permission
change nobody takes responsibility for is exactly what an audit is for.

**Idempotency moved to the authority.** `CommandInbox` still deduplicates, but that guards one
delivery path — a retried POST, a restarted bridge, a second transport or a direct call each
bypassed it. `ControlIngress` is the single place that decides whether a command changes the risk
state, so it decides whether it already has. A duplicate returns `duplicate=True` with the
original's ordinal and sequence, and produces no second `RiskRecord`.

## Defect B — the risk and health view was inferred

| Was | Is |
|---|---|
| `risk_active = ()`, `risk_latched = ()` hardcoded | the exact sets P9 recorded |
| `awaiting_snapshot` derived from `not clob_healthy` | `HealthFrame.clob_awaiting_snapshot` |
| spot `HEALTHY` because a spot timestamp existed | `HealthFrame.spot_status` |
| latency fields all `None` | P8's own stage timings, with the ordinal they were sampled at |

A spot price can be present while the feed is STALE — those are different facts, and the
dashboard was reporting the wrong one. Everything now comes from the `RiskRecord` the decision
*names*, found by sequence rather than by being newest, so the view describes one coherent
moment. Where it cannot — P8 samples, so most cycles have no timing — the snapshot carries
`latency_sample_ordinal` and an `observation_points` map saying which ordinal each part came
from, rather than quietly mixing them.

An unsampled cycle reads "not sampled", never `0 ns`.

## Defect C — settlement never reached the UI

The publisher had the fields and the runner never filled them. After P10 resolves, and after P11
verifies, both are delivered to the read model and a final frame is written, so the last thing an
operator sees is the resolved market rather than a permanent "unknown".

## Performance

12,000 real captured events, four alternated triples, fresh interpreter each. P9 and P11 run in
every configuration; only the UI machinery changes.

| Metric | off p50 | healthy p50 | stalled p50 | healthy Δ | stalled Δ |
|---|---:|---:|---:|---:|---:|
| decide | 23,515 | 23,815 | 23,494 | +300 ns (+1.28 %) | **−21 ns (−0.09 %)** |
| full cycle | 52,468 | 53,236 | 52,253 | +768 ns (+1.46 %) | **−215 ns (−0.41 %)** |
| receive→reconcile | 28,482 | 28,859 | 28,273 | +377 ns | −209 ns |

p95 / p99 (ns):

| Metric | off | healthy | stalled |
|---|---|---|---|
| decide | 26,932 / 68,149 | 28,149 / 71,356 | 27,208 / 69,048 |
| full cycle | 69,597 / 104,982 | 77,331 / 119,069 | 67,515 / 103,974 |
| receive→reconcile | 33,687 / 73,951 | 37,581 / 78,881 | 33,372 / 74,428 |

Semantic output identical across all three modes and all four pairs:
`{"SAFE": 11971, "HALTED": 28, "RECOVERING": 1}`, `PLACE 73 · KEEP 10272 · CANCEL 27 · REPLACE 45`.

| P8C limit | Target | P12B healthy | Verdict |
|---|---|---:|---|
| Decide p50 overhead | ≤ 1,000 ns / ≤ 3 % | **+300 ns / +1.28 %** | **MET** |
| Full-cycle p50 overhead | ≤ 5,000 ns / ≤ 5 % | **+768 ns / +1.46 %** | **MET** |

**A stalled bridge is measurably indistinguishable from having no UI at all** — −21 ns on decide
against the P11-only baseline. That is the claim, and it is the number.

## Real market — `btc-updown-5m-1787807700`

| | |
|---|---|
| Decisions | 82,336 |
| Risk records | 82,338 (two are the operator's commands) |
| Control audit rows | **2**, accepted 2, dropped 0, `audit_errors` 0 |
| Bridge | 4,728 polls, 1,012 snapshots, 0 deferred, 0 unreadable, 0 errors |
| Hot channel | capacity 32, **high-water 1** |
| Bridge stall | +200 s → +240 s, controlled local fault |
| Drops / gaps / sink errors | **0 / 0 / 0** |
| Verification | **COMPLETE** |
| Archive | 53.1×, verified, re-verified COMPLETE from the archive |

### Control, through the real path

```text
[market_safe] phase=QUOTE risk_state=SAFE ordinal=924
[halted]      20976c37536b498c  ordinal 988   risk_sequence 964   HALTED  place=False cancel=True
[15s later]   still HALTED at ordinal 4827
[released]    d8fa82dbcedd4a0f  ordinal 5001  risk_sequence 4878  RECOVERING -> SAFE
```

Audited from the durable record, through the verified archive:

```text
places_by_risk_state   {"SAFE": 365}
risk_states            {"SAFE": 78226, "HALTED": 4109, "RECOVERING": 1}
control_audit_cross_links  True
```

**PLACE while HALTED: 0. PLACE while RECOVERING: 0**, with 4,109 decisions taken under the halt.

### UI kill and restart

```text
[ui_killed]            SIGKILL at ordinal 5008, 4,881 decisions persisted
[ui_unreachable]       reachable=False
[bot_alive_after_kill] +13,728 events, +13,440 decisions in 47s, risk SAFE
[ui_restarted]         http=200, rendered the market, showed both commands, emitted none
```

Zero drops, gaps and sink errors across the whole market — which spans both the kill and the
bridge stall — so neither caused a journal or persistence gap.

### The final snapshot

```text
phase DONE   risk SAFE   active []   latched []
clob HEALTHY   awaiting_snapshot False   spot HEALTHY
decide_ns 123,416   latency_sample_ordinal 84,731
resolution RESOLVED   winner DOWN   block 92,735,374   payout [0, 1]
redemption_enabled False   live_trading_enabled False
control_audit_complete True
observation_points {decision: 84734, risk_verdict: 84734, latency_sample: 84731, counters: latest}
```

**One honest note.** The snapshot's `verification_status` reads INCOMPLETE, because the runner
published it with the verifier as it stood during that market — before the coverage-count fix
below. The store itself verifies **COMPLETE**, shown above and again from the archive. The
snapshot is a faithful record of what the verifier said at the time, and is not edited to say
something it did not.

## A defect this market found

`persistence_log_covers_every_row` compared the storage envelope against decisions, fills, risk
records and settlements — every event-like table except the one that had just been added. Two
control-audit rows made a perfect store read INCOMPLETE, with a message that gave no clue why:

```text
164677 storage-order entries for 164675 stored records
```

The check now counts control rows and names each table's contribution when it disagrees. Read
side only, so the market was re-verified rather than re-run — and a deleted envelope entry still
fails it.

## Retained and superseded

[P12](P12-UI-CONTROL-PLANE.md) and `btc-updown-5m-1787803500` are **retained**. That market
remains evidence that HALT/RELEASE semantics worked, that the UI process dying did not stop the
bot, that a UI restart worked, and that P11 caught a missing operator RiskRecord.

Its **final architectural acceptance is SUPERSEDED**, because synchronous filesystem publication
and polling existed inside `on_tick` when it ran.

## Unchanged

* **Real own-fill durable record: UNRUN / P14.**
* **Real maker fraction: UNRUN / P14.**
* **Real taker-fill persistence: UNRUN / P14.**
* **Real nonzero own-ledger settlement analytics: UNRUN / P14.**

P12 closes no strategy open item and cannot edit a strategy value. O07 remains OPEN.
