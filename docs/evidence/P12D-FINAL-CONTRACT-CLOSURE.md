# P12D — final contract closure

**Provenance: `REAL_PUBLIC_MARKET_DATA`** for the market revalidation, `REPLAY_OF_REAL_CAPTURE`
for the latency comparison. No fault was injected, and no new live market was run: nothing here
changes the trading path, and spending a five-minute market to prove arithmetic and type checking
would be spending it for nothing.

**No order. No credential. No chain write.** `LIVE_TRADING_ENABLED` and `REDEMPTION_ENABLED` are
both `False`.

**Date:** 2026-08-27 (UTC). Closes the four contract issues left open by the independent review
of [P12C](P12C-SNAPSHOT-COHERENCE.md), which is retained.

## A — `decide_ns` was not P8's decide duration

The read model published

```python
decide_ns = OBS_DECIDE_DONE_NS - OBS_RAW_RECEIVE_NS
```

which is *receive to decide*: ingress, reduction, dispatch and the strategy call together. P8
already has that figure and calls it `clob_receive_to_decide`. P8's `decide_duration` is
`OBS_DECIDE_STAGE_NS - OBS_REDUCE_STAGE_NS` — the strategy call itself.

One metric, one meaning. `decide_ns` is now P8's definition, and the receive-to-decide figure is
published under `receive_to_decide_ns` rather than dropped: it answers a real question, it simply
answers a different one.

`prepare_ns`, `reconcile_ns` and `receive_to_reconcile_ns` keep P8's definitions unchanged.

### Sampling stays honest

Stages are sampled independently, so the projection now returns whichever durations were
captured. A cycle timed end to end without stage stamps keeps its `receive_to_reconcile_ns` and
has **no** `decide_ns` — absent, never zero, because zero is a measurement and there was not one.
Discarding the whole sample for one missing stage would throw away real measurements; that is the
other half of the same rule.

### Proved against P8 on real cycles

`p12d-latency-replay.json`: a complete P6 capture of a real `btc-updown-5m` market replayed
through the production ingress path, with P8's own deterministic sampler choosing which cycles
are stage-stamped. **4,883 stage-sampled cycles**, every one compared figure by figure against
`TelemetryAnalyzer`:

```text
decide_duration        4883 cycles   agree
prepare_duration       4883 cycles   agree
reconcile_duration     4883 cycles   agree
receive_to_reconcile   4883 cycles   agree
first mismatch         none
```

One of them, to show the size of the difference:

```text
decide_ns              10,504    <- what the UI now publishes
receive_to_decide_ns   15,690    <- what P12C published under that name
prepare_ns                689
reconcile_ns            3,522
receive_to_reconcile_ns 19,901
```

### The P12C snapshot is retained, not edited

`p12c-final-snapshot.json` records what the P12C dashboard actually displayed, mislabelled field
and all. `p12d-p12c-snapshot-latency-correction.json` states the old field, its old meaning and
the correct one, and reads the same market's own P8 analyzer:

```text
decide_duration          p50    50,956 ns
clob_receive_to_decide   p50   200,416 ns
published as decide_ns          268,302 ns
```

The published figure belongs to the second distribution. That cycle's true decide duration cannot
be recomputed — P8's observation buffer is live and is not persisted — and that is said rather
than worked around.

## B — a callback did not prove persistence

`TelemetryStore` absorbs every SQLite exception by design. The worker read "the write method
returned" as "the row is in the file":

```python
store.write_control_audit(row)
stats.control_records_written += 1
on_control_record(row)
```

A refused row took exactly that path: `_execute` counted a `sink_error`, returned, and the worker
counted it as written and told the read model it had persisted. The manifest would claim a row the
file does not have; the operator's history would list a command whose audit row was rejected.

The append-only writers now return whether the write path took the row — **both** statements, the
persistence-log envelope and the row itself — and the worker gates its counters and its Plane-3
callbacks on that. A failed write increments `sink_errors` and a new `write_failures`, leaves the
written count alone, fires no callback, and leaves the market unable to verify COMPLETE. Errors
are still absorbed; nothing new raises into Plane 1.

Applied to all four event-like rows, not only the two the review named: the false-success path
was identical in each.

### What it found immediately

`test_sustained_concurrent_append_and_popleft_loses_nothing` opened the store on the main thread
and drained from a second one, so every write raised

```text
SQLite objects created in a thread can only be used in that same thread
```

and the test passed anyway, because **20,480 of the 40,000 "written" decisions were counted
despite never reaching the file**. The draining thread now opens the store, as the production
worker does in `start()`, and the test asserts zero sink errors. The production runner was never
affected — the worker opens its connection on its own thread — but a test that cannot tell a
write from a refusal is not testing persistence.

## C — the audit columns were not checked against the payload

`control_audit` keeps six indexed columns and a payload. The verifier read the payload and
trusted it, so a row whose column said `RELEASE_OPERATOR_HALT` while its payload said
`OPERATOR_HALT` cross-linked perfectly — and answered differently depending on who asked, since
the table is *queried* by its columns and the audit is *read* from its payload.

Both are now read and compared: `market_id`, `command_id`, `kind`, `accepted`, `risk_sequence`,
`schema_version`.

**The control schema domain is explicit.** V1 is the whole of it. A row claiming 0, -1, 2, `true`
or `"1"` is refused rather than guessed at — P11F's rule for decisions, applied to the table
P12B added.

**Types are exact, with no coercion in any comparison.** `schema_version`, `issued_at_ns` and
`ingress_ordinal` must be `int` and not `bool`, which is an `int` in Python and is not a schema
version. `market_id`, `source`, `kind` and `risk_state` must be `str`. `accepted`, `signal_flag`
and `allows_place` must be `bool`, so `None` still means "not recorded" rather than "recorded as
false". `OPERATOR_HALT` still requires `signal_flag` true and `RELEASE_OPERATOR_HALT` false, and
two commands can no longer claim the same risk row.

## D — the browser's clock decided delivery order

`OperatorCommand.issued_at_ns` says it is recorded for audit and used for nothing else. The
transport then named every pending file `{issued_at_ns}-{command_id}.json` and read the directory
sorted, so the sender's wall clock decided which of two waiting commands reached the bot first —
and with it which received the earlier authoritative ingress ordinal. A UI whose clock had
drifted backwards could put a later command ahead of an earlier one.

The inbox now allocates its own transport sequence on submission, independent of any clock, and
names files by that. A restarted UI continues **above** the highest sequence already pending, so a
fresh process cannot sort ahead of — or collide with — a command waiting since before the
restart. The submit path already lists the directory for its bound, so this costs no extra
syscall.

`next` on an `itertools.count` rather than a lock and a `+= 1`: it completes inside one C call,
the same atomicity P11 relies on for `deque.append`. The UI modules are forbidden a lock by a
test, and the right answer to a guard that catches your design is a different design, not a
looser guard.

**Transport order is not market causality.** What order a command happened in is still decided
when the bot accepts it — `ingress_ordinal` and `risk_sequence` — and that is what the durable
audit and any replay use. The transport sequence appears on neither the delivered command nor the
`ControlAuditRow`.

`channel.py` still described the bot listing the directory itself, which stopped being true in
P12B. It now documents the real path:

```text
UI process -> filesystem inbox -> Plane-3 CommandBridge -> bounded memory channel -> ingress owner
```

## Real P12C market, revalidated

`btc-updown-5m-1787811600`, re-read through the verified archive by the corrected code
(`p12d-market-audit-query.json`):

```text
verification            COMPLETE          telemetry_complete   true
decisions               124,272           risk records         124,274
control rows                  2           storage entries      248,549 exact from 1
drops 0    gaps 0    sink errors 0
places_by_risk_state    {"SAFE": 646}     HALTED 0   RECOVERING 0
risk_states             {"SAFE": 117194, "HALTED": 7077, "RECOVERING": 1}
settlement              RESOLVED UP, block 92,737,974, payout [1, 0]
control_audit_cross_links  True — both rows pass column/payload identity
```

Identical to the P12C read in every field but the source path.

## Performance

No hot-path file changed. `src/maker5m/ui/hotpath.py`, `tools/p12_market.py`,
`src/maker5m/feeds/`, `src/maker5m/strategy/`, `src/maker5m/market/`, `src/maker5m/execution/`
and `src/maker5m/telemetry/` are **byte-identical** to the reviewed P12C boundary — `git diff
44188fd..HEAD` over them is empty. Everything changed here runs on the persistence worker, on the
bridge thread, in the UI process, or offline in the verifier.

No new P8C benchmark was run, on that basis. The existing performance and equivalence regressions
pass: P8 latency, stage sampling, offload equivalence, replay determinism and journal codec
identity.

## Compatibility

All eight accepted stores, re-verified (`p12d-store-compatibility.json`): the same verdicts and
the same failure text as the P12C verifier. Four verify COMPLETE; the other four are expected not
to, for reasons already in the record (two pre-P11B V1 stores, the controlled stalled-sink
market, and the first P12 market's unpublished operator RiskRecords).

Frozen strategy checksums unchanged; no strategy parameter changed; P12 still cannot edit one.

## Unchanged

* **Real own-fill durable record: UNRUN / P14.**
* **Real maker fraction: UNRUN / P14.**
* **Real taker-fill persistence: UNRUN / P14.**
* **Real nonzero own-ledger settlement analytics: UNRUN / P14.**

P12D closes no strategy open item and cannot edit a strategy value. O07 remains OPEN.
