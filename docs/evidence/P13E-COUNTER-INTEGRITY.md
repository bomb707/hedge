# P13E — counter integrity

**Provenance: `REAL_PUBLIC_MARKET_DATA`.** **No order. No credential. No chain write.**
`LIVE_TRADING_ENABLED` and `REDEMPTION_ENABLED` are both `False`.

**Date:** 2026-08-27 (UTC). One shipped bug that would have ended a two-hundred-market run at
about a hundred, and two verifier gaps that let a half-written audit trail pass an audit.

## `p13-corpus-4` stopped and superseded

Stopped by `SIGTERM` to PID 350548, verified through `ps`, `/proc/350548/cwd` and
`/proc/350548/cmdline` as this root's collector before signalling. Four durable rows, all COMPLETE
and replay-EXACT, from six attempts; the two in flight remain open in the ledger.

Its own log is the defect, in its own words:

```text
btc-updown-5m-1787854800: COMPLETE ... appended=True (7 durable)
btc-updown-5m-1787854800: COMPLETE ... appended=True terminal=True (8 durable)
```

**Eight completion lines over four corpus rows** — the same market counted at the corpus append
and again after the terminal record. Preserved unedited: a log that recorded the bug is worth more
than a tidy one.

Re-read with the corrected verifier the durable material is sound — 4 rows, **4 joined-qualifying**,
6 attempts started with 4 finished and 2 open, no duplicate starts, no duplicate terminals, 4
identity-valid latency artifacts. The defect was in the counting, not in what was collected.

## The double count

P13D added the correct increment after the terminal record and did not remove the old one after
the corpus append. Both shipped:

```python
appended = self.corpus.append(entry)
if appended and COMPLETE and eligible:
    self.completed_this_process += 1  # the old one, still there
...
terminal = self.ledger.finish(...)
if terminal and appended and COMPLETE and eligible:
    self.completed_this_process += 1  # the new one
```

Worse than the arithmetic: production was deciding completion with a fourth, simplified copy of
the qualification rule while P13D's whole point was that there is one.

**The counter is not incremented at all now.** After the terminal record it is re-derived from the
corpus and the ledger by `maker5m.bot.qualify` — the same implementation the resume arithmetic and
the final report use — and the runtime total is compared against the durable count at every
finalisation boundary. If they disagree that is a collector-integrity fault and an
`ACCEPTANCE_CLEAN` run stops, because a run that cannot count its own evidence has nothing to say
about two hundred markets.

A counter maintained by hand beside a rule maintained somewhere else drifts. This one did.

### The test that would have caught it

P13D's test recreated the intended sequence by hand and passed while the shipped `_finalize`
counted twice. A test that reproduces the code it is checking checks the reproduction. The new
ones call `Supervisor._finalize` itself, stubbing only what would reach a network, a store or a
child process:

```text
one market            completed_this_process 1, completed 1, qualifying_now() 1
ten markets           10 / 10 / 10
every boundary        completed == qualifying_now()
failed terminal       0, row retained, integrity fault, acceptance run halted
target 100 / 199      keeps going;  target 200  stops
```

Four of them fail against `7e585b1`.

## Duplicate starts were collapsed

`AttemptIndex.starts` was a dict keyed by attempt id, so two `ATTEMPT_STARTED` records for one
attempt became one and the index quietly picked a winner — while terminals, three lines away, were
kept as a list precisely so duplicates could be seen. Both are lists now, "exactly one start" is
enforced, and `duplicate_start_attempts` appears in the report beside the terminal figure. Two
disagreeing starts fail on the duplication itself; which of them is right is not the question.

## Absence was being read as agreement

The joined identity check compared a field only `if name in start` and `if name in terminal`, so a
record missing `source_tree_sha` satisfied the comparison against it. The field nobody wrote is
the field nobody can check. Both records must now carry every required identity field — slug,
epoch, config, revision, tree, run mode — and match the corpus row.

This is why the **P13C pilot no longer qualifies under the stricter verifier**: its terminal
records predate the identity fields, which P13D added afterwards. Its four latency artifacts are
individually identity-valid, its stores verify COMPLETE and its journals replay EXACT — the
records simply do not carry what this contract now requires of them. It is retained and
superseded, and the revalidation runs on markets collected by a build that writes them.

## "Exact equality, no coercion" was a comment, not the code

`got != want` accepts `True` for `1` and `1.0` for `1`, so an artifact declaring
`schema_version = True` satisfied a check for version 1. The load-bearing fields are typed now:

```text
type(x) is int   schema_version, t0_ns, sampling.sample_every
type(x) is str   slug, market_id, condition_id, source_revision, source_tree_sha,
                 config_sha256, epoch, run_mode
```

And `condition_id` and `t0_ns` were compared only when both sides carried them, which contradicted
the rule stated three lines above them. For a v1 artifact they are part of the contract, and their
absence is a refusal.

## Revalidation — `p13d-pilot-1`

Four consecutive real markets on the corrected build, one process, clean tree. The counters are in
the log, one line per market rather than two:

```text
btc-updown-5m-1787856000  COMPLETE  EXACT  appended=True terminal=True  (runtime 1 / durable 1)
btc-updown-5m-1787856300  COMPLETE  EXACT  appended=True terminal=True  (runtime 2 / durable 2)
btc-updown-5m-1787856600  COMPLETE  EXACT  appended=True terminal=True  (runtime 3 / durable 3)
btc-updown-5m-1787856900  COMPLETE  EXACT  appended=True terminal=True  (runtime 4 / durable 4)
```

Under the corrected verifier: **4 corpus rows, 4 joined-qualifying**, 4 attempts started and 4
finished, 0 open, **0 duplicate starts, 0 duplicate terminals**, every row joining exactly one
attempt, and **4 identity-valid latency artifacts, 0 refused**. Classification and actions
exhaustive at 1,019,806 side opportunities each; both feeds warm at
T0 on all four handoffs; 0 drops, gaps and sink errors; PLACE only under SAFE (2,125); four
settlements RESOLVED; lifecycle high-water 2 of 6.

Merged live latency: CLOB receive→decide p50 **125,610 ns**, p99 728,844 ns over
50,023 samples; spot receive→decide p50 85,008 ns, p99
246,908 ns over 990.

Full figures in `p13d-pilot-corpus.json` and `p13d-pilot-corpus-index.jsonl`.

## No market-facing code changed

`feeds/`, `capture.py`, `strategy/`, `execution/`, `risk/`, `telemetry/`, `ui/hotpath.py`,
`persistence/` and the latency formulas are byte-identical to the reviewed P13D boundary. The diff
is runtime accounting, the attempt verifier, latency identity typing, tests, the report and docs.
The existing P8C regressions stand; no new benchmark was required and none was run.

## Retained and superseded

`p13-corpus-1` through `p13-corpus-4`, the P13 v1 pilot, both P13B pilots and the P13C pilot are
all retained unedited with supersession recorded beside them. None counts toward the ≥200 gate.

## Unchanged

* **Real own-fill durable record: UNRUN / P14.**
* **Real maker fraction: UNRUN / P14.**
* **Real taker-fill persistence: UNRUN / P14.**
* **Real nonzero own-ledger settlement analytics: UNRUN / P14.**

P13E closes no OPEN item, proposes no threshold, and changes no strategy value.
