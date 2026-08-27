# P12C — snapshot coherence closure

**Provenance: `REAL_PUBLIC_MARKET_DATA`** for the market, `REPLAY_OF_REAL_CAPTURE` for the
overhead benchmark. No fault was injected on this market.

**No order. No credential. No chain write.** `LIVE_TRADING_ENABLED` and `REDEMPTION_ENABLED` are
both `False`, and nothing in P12 can change either.

**Capture date:** 2026-08-27 (UTC). Supersedes the architectural half of
[P12B](P12B-PLANE3-ISOLATION.md), which is retained.

## Defect A — stdout is I/O, and it was on the ingress owner

P12B removed the filesystem from `on_tick` and left five `print(..., flush=True)` calls behind:
every accepted command, every controlled sink stall, every bridge stall transition. The rule was
never "no filesystem call". It is **no synchronous I/O on the single ingress consumer**, and a
write to a pipe nobody is draining blocks as thoroughly as a stalled `stat` — a terminal that has
stopped reading, a full pipe buffer, a log collector that has wedged.

```
P12B   on_tick -> deque.popleft(), risk apply, print, print, print
P12C   on_tick -> deque.popleft(), risk apply, deque.append
```

The hot side now appends an immutable `("command", outcome)` fact to a bounded channel. The run
log is rendered from that channel on the main thread once the market has closed. Formatting is
not free; it is simply not paid by the thread that must never wait.

It is not a different logger, either. Replacing `print` with `logging` would have moved the same
synchronous write behind a lock and a handler.

### The test that could have caught it, and the one that did not

P12B had a test asserting the hot-side body did no I/O. It reproduced that body in the test file
— `pop_all` and `apply` — while the shipped `on_tick` printed three lines beside them. A test
that paraphrases the production shape proves the paraphrase.

Two things changed:

* the hot control path is one extracted production function, `drain_operator_commands`, which
  the test drives with `print`, `logging` and thirteen filesystem entry points replaced by
  raisers;
* a second test reads `tools/p12_market.py` **itself** and walks the AST of `on_tick`, failing on
  any call to `print`, `open`, `write`, `flush`, `dump`, `listdir`, `stat`, `rename`, `sleep`,
  `maybe_publish` or the rest. It fails against the P12B file.

The overhead benchmark now calls the same production function with a real `ControlIngress`
behind it, rather than an inlined `for _pending in hot_commands.pop_all(): pass`.

## Defect B — the snapshot was not joined to the decision it described

`on_persisted` did two cross-thread reads from the persistence worker:

```python
publisher.observe(record, controller.trace.records[-1] if controller.trace.records else None)
sample = _latency_sample(runs[0])  # reads run.pipeline.merger
```

`controller.trace.records[-1]` is the **newest** verdict, not the one this decision names. The
worker runs behind the market, so the newest record is routinely several sequences ahead: the
dashboard showed one moment's decision beside another moment's risk state and called it one
observation. `_latency_sample` read the merger's mutable stage clocks while Plane 1 was still
writing them.

Both are gone. The worker now offers three Plane-3 callbacks:

```text
on_decision_record(record, observation)   decision + the immutable observation it was built from
on_risk_record(record)                    each persisted RiskRecord, keyed by risk_sequence
on_control_record(row)                    each persisted ControlAuditRow
```

Latency is derived from the observation's own P8 stage stamps, so the four figures belong to that
cycle by construction. The verdict is looked up by `record.risk_sequence` in a bounded map of
persisted risk records; **an unarrived verdict reads unavailable rather than borrowing a
neighbour's**. A test mutates a merger-like object after the fact and the snapshot does not move.

## Defect C — the final snapshot was stale

P12B's last published frame and the manifest written beside it disagreed:

| | P12B snapshot | P12B manifest |
|---|---|---|
| decisions | 82,335 | 82,336 |
| risk records | 82,337 | 82,338 |
| dropped | 1 | 0 |
| audit | INCOMPLETE | complete |

The counters were a running estimate that stopped one record early, and the close never corrected
them. The "dropped" figure came from `audit_errors == 0` — but `BoundedChannel.publish` never
raises when it drops, so that number proved only that nothing threw.

A `closed` message now carries the manifest's own figures and wins over every live counter, and a
counter arriving **after** the close is ignored rather than allowed to walk the totals backwards.
Audit completeness compares acceptance against persistence. Command history comes from
`on_control_record` — what is durably written — instead of an in-memory list the ingress thread
appended to.

## Defect D — the control audit compared truthiness

```python
if bool(row["accepted"]) != bool(outcome["accepted"]):   # None and False both falsy
```

`None` and `False` are different audit facts: "we did not record this" is not "we recorded that it
did not happen". The checks now compare with `type(x) is bool` / `is int` / `is str` and refuse a
null where a boolean belongs.

The cross-link also proved only that the two rows *agreed*. Two rows agreeing on `flag=False` for
an `OPERATOR_HALT` would have cross-linked perfectly while recording the opposite of what
happened, so the kind must now imply the flag:

```python
REQUIRED_CONTROL_FLAG = {"OPERATOR_HALT": True, "RELEASE_OPERATOR_HALT": False}
```

An unknown kind is refused rather than skipped.

## P12B snapshot revalidation

`p12c-p12b-revalidation-btc-updown-5m-1787807700.json`. The P12B store, opened through the
verified archive path, re-verified, and its final snapshot rebuilt by the corrected read model:

```text
decisions 82,336   risk 82,338   dropped 0   sink errors 0
telemetry_complete true   verification COMPLETE   control_audit_complete true
risk joined at sequence 82,337 (the one the last decision names)
RESOLVED DOWN   block 92,735,374   payout [0, 1]
halt     20976c37536b498c  ordinal 988   risk_seq 964   HALTED
release  d8fa82dbcedd4a0f  ordinal 5001  risk_seq 4878  RECOVERING
```

This is a **revalidation, not a rewrite**. `p12b-final-snapshot.json` is deliberately unchanged:
it is the evidence that the old read model contradicted its own manifest, and editing it would
destroy the only record of that.

The rebuild reads `UNKNOWN` for the feed statuses, because P11's risk rows do not persist P6's
`HealthFrame` and the read model refuses to infer health from the presence of data. That is the
read model behaving correctly on a rebuild, recorded rather than papered over.

## Cost to the trading path

Process-isolated, fresh interpreter per configuration, four alternated pairs, replayed from a
real P6 capture (`p12c-overhead.json`):

| Metric | off | healthy | stalled |
|---|---|---|---|
| decide p50 | 24,015 ns | 24,004 ns | 23,838 ns |
| full cycle p50 | 52,531 ns | 54,008 ns | 53,370 ns |

Decide p50 **−11 ns (−0.05 %)** healthy and **−177 ns (−0.74 %)** stalled — no measurable cost.
Full cycle **+1,477 ns (+2.81 %)** healthy and **+839 ns (+1.60 %)** stalled, against a
within-mode run-to-run spread of about 1.5 µs; the numbers sit inside the noise of each other and
are not claimed as a measured improvement or regression.

Every P8C limit is met, and no limit was moved:

| P8C limit | P12C | |
|---|---|---|
| decide p50 overhead ≤ 1,000 ns | −11 ns | MET |
| decide p50 overhead ≤ 3 % | −0.05 % | MET |
| full-cycle p50 overhead ≤ 5,000 ns | +1,477 ns | MET |
| full-cycle p50 overhead ≤ 5 % | +2.81 % | MET |

## Real market — `btc-updown-5m-1787811600`

124,272 decisions · 124,274 risk records · 2 control-audit rows · 248,549 storage entries, exact
from 1 with no duplicates · bridge 6,139 polls / 1,167 snapshots / 0 errors · hot channel
high-water **1** of 32 · hot event channel 2 accepted, **0 dropped** · 0 drops, 0 gaps, 0 sink
errors · verification **COMPLETE** · archive 650.0 MB → 11.9 MB (54.5×), restored and verified.

```text
halt     a52f1f38fada4dd0  ordinal 1358  risk_seq 1300  HALTED   place=False cancel=True
release  a3e7df18eaaf4cd1  ordinal 8241  risk_seq 8016  RECOVERING -> SAFE
kill     SIGKILL at ordinal 8430 -> +24,384 events, +23,820 decisions in 47s, risk SAFE
restart  same market, both commands shown from the durable audit rows, no new command
final    RESOLVED UP, block 92,737,974, payout [1,0], REDEMPTION DISABLED
```

Audited from the durable record, through the verified archive:

```text
places_by_risk_state       {"SAFE": 646}
risk_states                {"SAFE": 117194, "HALTED": 7077, "RECOVERING": 1}
decisions_missing_risk_reference       0
decisions_naming_an_absent_risk_row    0
decision_risk_copy_mismatches          0
```

**PLACE while HALTED: 0. PLACE while RECOVERING: 0**, with 7,077 decisions taken under the halt.

### The final snapshot, against the manifest

```text
                     snapshot     manifest
decisions            124,272      124,272
risk records         124,274      124,274
dropped                    0            0
sink errors                0            0
telemetry_complete      true         true
verification        COMPLETE     COMPLETE
```

Six for six (`p12c-snapshot-vs-manifest.json`). The frame itself:

```text
phase DONE   risk SAFE   sequence 124,273   active []   latched []
clob HEALTHY   awaiting_snapshot False   spot HEALTHY   order_stream UNKNOWN
decide_ns 268,302   prepare_ns 3,676   reconcile_ns 14,551   receive_to_reconcile_ns 286,529
latency_sample_ordinal 127,451   observation_points {decision: 127456, risk_verdict: 127456,
                                                    latency_sample: 127451, counters: null}
control_audit_complete True   accepted_commands 2 (from the audit table)
resolution RESOLVED   winner UP   block 92,737,974   payout [1, 0]
live_trading_enabled False   redemption_enabled False
```

`counters: null` is the close speaking: after the market ends the counters come from the manifest,
not from an observation point in the stream.

## A market this round cost

The first P12C attempt, `btc-updown-5m-1787810700`, ran the full five minutes, drained its buffer
and stopped its worker cleanly — and then died formatting its own run log. `ControlEvent.of(kind,
payload)` is the pair `("command", outcome)`, so the payload *is* the outcome, and the renderer
subscripted it. The traceback landed after `worker.stop` and before the manifest was written, so
a complete capture was left with a database and no closure.

That capture is not presented as acceptance evidence and its store was not closed. Its UI
acceptance log is retained as `p12c-ui-acceptance-SUPERSEDED-btc-updown-5m-1787810700.json`; every
control step in it passed, including a SIGKILL at ordinal 8,262 followed by 19,967 events and
19,539 decisions in 47 seconds. It is kept because it is what happened.

The renderer is now a named function a test drives with events built by the production hot
function, and its call site is guarded: this is evidence formatting running after the market has
closed, and nothing it does should be able to end a run.

## Compatibility

Every store this project has accepted, re-opened and re-verified by the P12C code
(`p12c-store-compatibility.json`):

| Market | Store | Decision schema | Verdict |
|---|---|---|---|
| `1787748900` | V1 | V1 | UNSUPPORTED — pre-P11B layout, refused by the domain rule P11F closed |
| `1787749500` | V1 | V1 | UNSUPPORTED — same |
| `1787770200` | V2 | V2 | COMPLETE |
| `1787771100` | V2 | V2 | INCOMPLETE — the controlled stalled-sink market; real bounded-buffer loss |
| `1787780700` | V2 | V2 | COMPLETE |
| `1787803500` | V2 | V2 | INCOMPLETE — the first P12 market's two unpublished operator RiskRecords |
| `1787807700` | V3 | V2 | COMPLETE — P12B, two control rows |
| `1787811600` | V3 | V2 | COMPLETE — P12C, two control rows |

Four of the eight are expected not to verify COMPLETE, each for a reason already in the record. A
verifier that passed all eight would be the defect.

The pre-P12C verifier gives **the same verdict and the same failure text** on all eight, so the
typed control-audit comparisons changed nothing about how existing stores read — they only refuse
things no accepted store contains.

## Retained and superseded

* **P12B's store is valid.** `btc-updown-5m-1787807700` verifies **COMPLETE** with 82,336
  decisions, 82,338 risk records, 0 drops, 0 gaps and 0 sink errors — before this round and again
  from the archive during it. Nothing about that market's durable telemetry is in question.
* **P12B's final snapshot is a known-inaccurate read-model artifact.** It is retained unedited,
  and `p12c-p12b-revalidation-...json` states what the corrected read model says about the same
  bytes.
* **P12B's architecture is superseded**, because stdout I/O ran on the ingress owner and the read
  model reached across threads for its verdict and latency. The files are retained.
* [P12](P12-UI-CONTROL-PLANE.md) and `btc-updown-5m-1787803500` remain retained and superseded.

## Unchanged

* **Real own-fill durable record: UNRUN / P14.**
* **Real maker fraction: UNRUN / P14.**
* **Real taker-fill persistence: UNRUN / P14.**
* **Real nonzero own-ledger settlement analytics: UNRUN / P14.**

P12C closes no strategy open item and cannot edit a strategy value. O07 remains OPEN.
