# P12E — commit boundary closure

**Provenance: `REAL_PUBLIC_MARKET_DATA`** for the market revalidation, `REPLAY_OF_REAL_CAPTURE`
for the latency comparison and the overhead benchmark. No fault was injected on a real market and
no new live market was run: the runtime decision path is byte-identical to the reviewed P12D
boundary.

**No order. No credential. No chain write.** `LIVE_TRADING_ENABLED` and `REDEMPTION_ENABLED` are
both `False`.

**Date:** 2026-08-27 (UTC). Closes the durability defect left open by the independent review of
[P12D](P12D-FINAL-CONTRACT-CLOSURE.md), which is retained.

## The defect — an INSERT is not a record

`TelemetryStore` batches on purpose: `BEGIN`, up to five hundred inserts, `COMMIT`. P12D made the
write path report whether a statement was accepted, which closed the *statement-level* hole — a
row the database refused was no longer counted or announced. It left the transaction-level one
open:

```python
if store.write_control_audit(row):  # the open transaction took it
    stats.control_records_written += 1  # counted as durable
    on_control_record(row)  # announced as persisted
```

Between those lines and the file there is still a `COMMIT` that has not happened and might not.
And `_flush` set `_pending = 0` unconditionally, so a commit that raised was counted as a batch,
forgotten, and never reflected anywhere except a `sink_error` nobody was comparing against.

The rows had already been counted into the manifest and announced to the operator by then.

## Two states, named apart

| | |
|---|---|
| **accepted into the open transaction** | `_execute` returned `True`; `rows_accepted_into_transaction` |
| **committed** | `COMMIT` returned; `decisions_written`, `risk_written`, `control_records_written`, `fills_written` |

Only the second is durable, and only the second feeds the manifest, the verifier or a callback.
The store's `on_commit` reports the outcome of every commit attempt; the worker **stages** each
row when the transaction accepts it and **promotes** it when the transaction commits. Promotion
is where the counter moves, where the market metrics fold it in, where the manifest's ingress
span extends, and where `on_decision_record`, `on_risk_record` and `on_control_record` fire.

Every one of those callbacks now means one thing: **this row is in a SQLite transaction that
committed successfully.** They arrive in bursts of a batch, in storage order, exactly once each.

`on_decision_record` was already documented as persisted and was already the UI's source, so it
moved with the others rather than being split into a pre-commit projection — the alternative the
review allowed, but only with a rename, and a projection nobody asked for is not worth a second
name. Its latency still comes from the immutable observation; only its timing changed.

**Batching remains.** `batch_size` is still 500. No row commits on its own.

### Two ordering details this depended on

The batch check moved from the end of `_execute` to the **start** of each writer. A row and its
storage-order envelope now always share a transaction, and a commit never happens *inside* a
row's own write.

That second part was not theoretical. With the check at the end, the last row of a market landed
in the batch that its own write had just committed — so the worker staged it against a
transaction that no longer existed, and the close found nothing pending to announce it with. The
40,000-row concurrency test caught it, reporting 39,999.

## Commit failure fails closed

```text
1. record the error and count commit_failures    -> sink_errors += 1
2. attempt ROLLBACK                              -> its own failure is recorded, not assumed
3. drop the staged rows unannounced              -> rows_lost_to_failed_commit += n
4. BEGIN a fresh transaction if the connection allows one
5. keep consuming
```

The storage sequences those rows held are **not** reissued, so the hole stands and the market
cannot verify COMPLETE. A gap the verifier finds is better than a row an operator was told about
that is not in the file. The exact transaction state after a successful commit, a failed commit
and a failed rollback is documented on `_flush`.

`close()` returns whether the final commit succeeded. The last batch is almost never a full one,
and a partial batch dropped for never reaching `batch_size` would be a market losing its own tail.

Nothing raises into Plane 1.

## Proved by failing the commit, not the insert

**SUPPORTING UNIT TESTS ONLY.** The failure is induced through the real transaction path: a
wrapper around a genuine sqlite3 connection that raises `OperationalError` on `COMMIT` and passes
everything else through. The inserts must actually reach SQLite and succeed, because the gap under
test is exactly the one between a statement the transaction accepted and a transaction that
committed. Faking the insert would test the wrong half. All six fail against P12D.

```text
failed commit      published []   risk_written 0   rows_lost_to_failed_commit 1
                   sink_errors +1   commit_failures 1   committed_rows 0
producer           capture and drain continue; no exception reaches Plane 1
verification       a market with a failed commit is not COMPLETE
successful batch   nothing before COMMIT; 7 decisions + 5 risk rows after it,
                   in storage order, once each, counters == SQLite row counts,
                   a second flush announces nothing again
final partial      9 decisions + 3 risk rows commit at close;
                   committed counters == SQLite row counts
```

### The operator command test

One accepted `OPERATOR_HALT`, its `OPERATOR_CONTROL` RiskRow and its `ControlAuditRow` all
inserted, then the commit fails:

```text
ControlIngress     accepted=True   risk_state=HALTED   allows_place=False
                   the controller is HALTED, and no store was involved in that
on_control_record  fired 0 times
control_records_written   0
command history    empty
audit completeness False
sink_errors        > 0
```

**Trading safety does not depend on telemetry.** The halt happened on the ingress owner's thread,
through P9, and nothing about persistence can undo it. What the durable record and the operator's
history may not do is claim a row that is not in the file.

### Command latency

An accepted operator command forces a flush after its audit row. Commands are rare — two in a
busy market — and an operator watching a halt should not wait for a telemetry batch to fill before
their command becomes durable evidence. Risk records are drained before control records, so the
`OPERATOR_CONTROL` RiskRow the audit row names is in the same transaction, or in one that has
already committed, before that row is announced. Plane 3 throughout; Plane 1 neither participates
nor waits.

## Real market, revalidated

`btc-updown-5m-1787811600`, re-read through the verified archive (`p12e-market-audit-query.json`):

```text
verification            COMPLETE          telemetry_complete   true
decisions               124,272           risk records         124,274
control rows                  2           storage entries      248,549 exact from 1
drops 0    gaps 0    sink errors 0
places_by_risk_state    {"SAFE": 646}     HALTED 0   RECOVERING 0
risk_states             {"SAFE": 117194, "HALTED": 7077, "RECOVERING": 1}
settlement              RESOLVED UP, block 92,737,974, payout [1, 0]
```

Identical to the P12D read in every field but the source path.

## P12D contracts, unchanged

`p12e-latency-replay.json`: 4,883 stage-sampled cycles from a real capture, every one agreeing
with `TelemetryAnalyzer` on `decide_duration`, `prepare_duration`, `reconcile_duration` and
`receive_to_reconcile`; no mismatch. The per-cycle nanosecond figures differ from the P12D run
because they are freshly measured readings on a live machine — the definitions are what is being
compared, and they agree exactly. `receive_to_decide_ns` keeps its own name. The command-order
and control-verifier contracts are covered by their own tests, all green.

## Cost to the trading path

The runtime decision path is byte-identical, so nothing was expected to move. Plane-3 work does
now arrive in bursts of a batch rather than one row at a time, which is a real change in *when*
that thread does its work, so the benchmark was re-run rather than argued about
(`p12e-overhead.json`, four alternated pairs, fresh interpreter per configuration):

| Metric | off | healthy | stalled |
|---|---|---|---|
| decide p50 | 24,094 ns | 24,090 ns | 24,092 ns |
| full cycle p50 | 52,662 ns | 54,505 ns | 54,068 ns |

Decide p50 **−4 ns (−0.02 %)** healthy and **−2 ns (−0.01 %)** stalled. Full cycle **+1,843 ns
(+3.50 %)** healthy and **+1,406 ns (+2.67 %)** stalled, against a within-mode run-to-run spread
of about 1.6 µs (off: 52,360–53,988; healthy: 53,288–55,015). P12C measured +2.81 % on the same
benchmark; these sit inside each other's noise and neither is claimed as a change.

Every P8C limit is met, and no limit was moved:

| P8C limit | P12E | |
|---|---|---|
| decide p50 overhead ≤ 1,000 ns | −4 ns | MET |
| decide p50 overhead ≤ 3 % | −0.02 % | MET |
| full-cycle p50 overhead ≤ 5,000 ns | +1,843 ns | MET |
| full-cycle p50 overhead ≤ 5 % | +3.50 % | MET |

## Compatibility

All eight accepted stores re-verified (`p12e-store-compatibility.json`): the same verdicts and the
same failure text as the P12D verifier. The commit boundary is a write-side and accounting
change; the read path is untouched. Frozen strategy checksums unchanged; no strategy parameter
changed.

## Unchanged

* **Real own-fill durable record: UNRUN / P14.**
* **Real maker fraction: UNRUN / P14.**
* **Real taker-fill persistence: UNRUN / P14.**
* **Real nonzero own-ledger settlement analytics: UNRUN / P14.**

P12E closes no strategy open item and cannot edit a strategy value. O07 remains OPEN.
