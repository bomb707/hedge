# P13D — final evidence binding

**Provenance: `REAL_PUBLIC_MARKET_DATA`** for every market referenced. **No order. No credential.
No chain write.** `LIVE_TRADING_ENABLED` and `REDEMPTION_ENABLED` are both `False`.

**Date:** 2026-08-27 (UTC). Two integrity defects, both about the difference between *having
evidence* and *being able to prove the evidence is what it claims to be*.

## `p13-corpus-3` stopped and superseded

Stopped by `SIGTERM` to PID 90635, verified through `/proc/90635/cwd` and its command line as this
root's collector before signalling. **12 durable rows, all COMPLETE and replay-EXACT, from 14
attempts** — the two in flight remain open in `attempts.jsonl`, which is exactly what a ledger is
for. Preserved entire, marked `SUPERSEDED_FOR_FINAL_P13_ACCEPTANCE`, inventoried in
`p13-corpus-3-superseded.json`.

Re-read under the corrected rules (`p13-corpus-3-audit.json`): 12 rows, 12 qualifying, 12 started
and finished pairs, 2 open attempts, 0 duplicate terminals, 0 latency identity mismatches. Good
data — but a corpus has to be collected *by* the build that enforces the rules, not merely survive
them afterwards, so it does not count toward the ≥200 gate.

## A — a row could count before its terminal record was durable

`_finalize` appended the corpus row, incremented the completion total, and then called
`ledger.finish(...)` in a `finally` block, discarding its return value. `finish` returns `False`
when the terminal event did not reach the disk. So:

```text
corpus row durable  +  terminal attempt record missing  ->  counted toward 200
```

A market the collector cannot prove it finished is not an accounting rounding error. The order is
now: append the corpus row, append the terminal event carrying `corpus_appended`, and only if
**both** are durable and the row is otherwise eligible does the market count.

**Ledger failure is a collector-integrity fault.** The in-memory lifecycle slot is released so
markets already running finish normally and nothing deadlocks, and an `ACCEPTANCE_CLEAN` run then
**stops launching new markets**. A corpus whose audit trail cannot be written is not a corpus. An
exploratory run records the fault and carries on, because nothing there is being counted. The
policy is stated in the code rather than inferred from it.

`recover()` had the same shape: it called `finish(ABORTED)` and ignored the answer, so an
abandoned attempt that could not be closed was reported as recovered. It now stays open, is
reported as open, and an acceptance run refuses to start while any remain.

## The one definition of "counts"

The collector's target, the resume arithmetic and the final report have to mean the same thing by
two hundred markets. Three near-identical implementations is how a run ends with the collector
saying 200 and the report saying 198. `maker5m.bot.qualify` is the rule, and all three call it:

```text
corpus row       COMPLETE, evidence-eligible, clean source, this epoch/config/revision/tree
attempt          exactly one ATTEMPT_STARTED
terminal         exactly one terminal event, and it is ATTEMPT_FINISHED
                 recording corpus_appended = true
identity         slug, epoch, config, revision, tree and run mode agree across all three
latency          hash-valid, schema-supported, identity-valid, CLOB and spot samples present
```

`ATTEMPT_FAILED`, `ABORTED_PREVIOUS_PROCESS`, an open attempt, a missing attempt and duplicate
terminal events all fail it. Duplicates are indexed as a **list** rather than a dict, because
last-one-wins would hide a `FINISHED` sitting beside an `ABORTED` — the case most worth seeing.

A corpus holding 150 otherwise-eligible rows of which 148 have a finished terminal, one failed and
one has none, counts **148**, and a target of 200 has 52 to go.

## B — a hash-valid artifact is not necessarily *this market's* artifact

`read_latency` checked SHA-256 and schema version. Both are true of another market's artifact.
A row pointing at the wrong file — an edited index, a copied file, a rebuilt directory — passed
every check while describing a different five minutes.

The payload already carried the identity; nothing compared it. `validate_latency_identity` now
requires exact equality, with no coercion, on:

```text
schema_version   kind = P13_LIVE_LATENCY   provenance = REAL_PUBLIC_MARKET_DATA
slug   market_id   source_revision   source_tree_sha   config_sha256   epoch   run_mode
condition_id and t0_ns when the row carries them      sampling.sample_every
```

**Missing is not equality**: an artifact that omits a field cannot satisfy a comparison against
it. The session reads its own artifact *back off the disk* and validates it before the market is
eligible — not the object it just wrote, because what a row points at is what a later reader gets.
The report validates identity per market before merging a single sample, and a mismatch lands in
`markets_refused` with its exact reason rather than quietly contributing to a distribution.

The swap test is the discriminating one: market A's row is pointed at market B's artifact, which
is perfectly hash-valid and perfectly schema-valid. A is refused on slug, market id, condition id
and T0, and none of B's samples enter A's numbers.

## The report audits every row, not the answer

`accounting` previously received only the eligible rows, which is auditing the answer. It now runs
over **all** rows for the epoch and reports attempts started, finished, failed, aborted, open,
duplicate terminals, corpus rows, qualifying rows, rows without a start, rows without a terminal,
rows with no attempt at all, and the exact reasons each refused row was refused.

## Revalidation — no new pilot required

Nothing changed in `feeds/`, capture timing, `strategy/`, `execution/`, `risk/`, P8 timestamp
capture, the latency formulas or P11 persistence. The change is accounting, validation, reporting
and tests. So the existing real evidence is re-judged under the stricter rules rather than
re-collected.

**P13C pilot (`p13c-pilot-1`), re-read:**

```text
corpus rows              4        qualifying rows          4
attempts started         4        finished                 4
failed 0   aborted 0     open 0   duplicate terminals      0
rows without start       0        rows without terminal    0
latency merged           4        refused                  0
```

Merged live latency, unchanged: CLOB receive→decide p50 **112,618 ns**, p95 620,029, p99 862,348,
max 21,079,107 over n = 63,478; spot receive→decide p50 **86,288 ns**, p95 432,009, p99 606,447,
max 5,219,806 over n = 2,101 — identical to the P13C figures, now with every artifact's identity
verified against its row.

**Controlled restart (`p13c-restart-1`), re-read:** 1 corpus row, 1 qualifying; 2 attempts started,
1 finished, **1 aborted**, 0 open. `btc-updown-5m-1787847300` — the market whose collector was
killed — has a start and an `ABORTED_PREVIOUS_PROCESS` terminal and no qualifying row;
`btc-updown-5m-1787847600` remains COMPLETE, replay-EXACT and qualifying.

## Retained and superseded

`p13-corpus-1`, `p13-corpus-2`, `p13-corpus-3`, the P13 v1 pilot, the P13B pilots and the P13C
pilot are all retained unedited, with supersession recorded beside them. None counts toward the
≥200 gate, which runs under a new epoch from one clean revision.

## Unchanged

* **Real own-fill durable record: UNRUN / P14.**
* **Real maker fraction: UNRUN / P14.**
* **Real taker-fill persistence: UNRUN / P14.**
* **Real nonzero own-ledger settlement analytics: UNRUN / P14.**

P13D closes no OPEN item, proposes no threshold, and changes no strategy value.
