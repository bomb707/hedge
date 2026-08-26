# P11C — final audit closure

**Provenance: `REAL_PUBLIC_MARKET_DATA`** for the baseline market, `REPLAY_OF_REAL_CAPTURE` for
the overhead benchmark. The controlled-stall evidence is retained from
[P11B](P11B-PERSISTENCE-INTEGRITY.md) and is `CONTROLLED_LOCAL_FAULT_ON_REAL_MARKET`.

**No order was placed. No credential exists. No chain write occurred.**
`LIVE_TRADING_ENABLED` is `False`; `REDEMPTION_ENABLED` is `False`.

**Capture date:** 2026-08-26 (UTC).

## The four items

| | Was | Is |
|---|---|---|
| **A** | PLACE permission read from the decision's own copy of the verdict | joined to the persisted `RiskRow`; copies compared both ways |
| **B** | benchmark read `pipeline.clob_health`, which a replay never updates | replays the journal's own recorded `HealthEvent`s |
| **C** | `INSERT OR REPLACE` on audit tables | append-only `INSERT`; a duplicate is a sink error |
| **D** | any `.xz` decompressed and queried | `open_verified_archive` proves identity first |

### A — the copy was checked against itself

The verifier read `risk_sequence`, `risk_allows_place` and the action out of each decision's
payload and checked the PLACE invariant against them. Those are the decision's **copy** of the
verdict, so a record that misrepresented the verdict it ran under would have been compared with
its own misrepresentation and passed.

Every decision now joins to the `RiskRow` it names. Permission comes from the row — a PLACE
requires it to exist, to allow placement, and to be `SAFE`. The copied `state`, `allows_place`
and `allows_cancel` are compared against it **in both directions**: a copy claiming a halt the
row never recorded is a mismatch too, because it means one of the two is wrong and the audit
cannot say which. A verdict recorded at a later ingress ordinal than the cycle it governed is
also refused.

Found while writing it: my own first comparison was `bool(copied) != bool(authoritative)`, which
silently exempted every state mismatch — `SAFE` and `HALTED` are both truthy strings.

### B — the benchmark measured a market that never quoted

`pipeline.clob_health` is updated by P6's feed lifecycle, which a journal replay does not run. So
every cycle saw CLOB UNKNOWN and awaiting-snapshot, P9 correctly halted, `risk_adjust` emptied
every intent, and the shadow order table never placed anything.

The journal already contains the answer: P6 emits health as normalized `HealthEvent`s and P2
reduces them, so **2,206 real recorded health events** sit in the measured market's own file.
Replaying those is neither re-deriving staleness nor inventing health — it is P6's own report.

### C — audit rows are append-only

`INSERT OR REPLACE` meant a second write at an identity replaced the evidence already there.

| Table | Contract |
|---|---|
| `decisions`, `fills`, `risk_records`, `settlements`, `persistence_log` | **APPEND-ONLY** — plain `INSERT`; a duplicate raises `IntegrityError`, is counted as a sink error, and leaves the original row byte-for-byte unchanged |
| `markets`, `market_metrics` | **FINAL/METADATA** — describe a market rather than record events in it, written once more than created; upsert is the honest shape |

Because a refused duplicate is a sink error, and a sink error fails `no_sink_errors`, such a
market cannot verify `COMPLETE`.

### D — an artifact without identity is not evidence

`lzma` will happily restore a file whose contents are not the market a sidecar names, and a
query answered from it is indistinguishable from a real answer. `open_verified_archive` checks,
in order: the compressed artifact's own hash, then decompresses, then the restored database's
hash against the expected raw hash, then opens read-only (which enforces the schema version),
then confirms the market id and slug inside are the ones claimed. Any failure raises and the
partially-restored file is deleted rather than left for a later caller to find. An archive whose
sidecar records no hash is refused outright.

## Performance

12,000 real captured events from `btc-updown-5m-1787647500`, ordinals 0–11,999, four alternated
triples, each configuration alone in a fresh interpreter. P9 runs in every configuration, so only
the persistence delta is charged to P11.

**Semantic equivalence, checked rather than assumed** — identical in all three modes and all four
pairs:

```text
risk states   {"SAFE": 11971, "HALTED": 28, "RECOVERING": 1}
actions       {"KEEP": 10272, "BLOCKED": 9161, "NOTHING": 4422,
               "PLACE": 73, "REPLACE": 45, "CANCEL": 27}
```

| Metric | off p50 | healthy p50 | stalled p50 | healthy Δ | stalled Δ |
|---|---:|---:|---:|---:|---:|
| decide | 23,417 | 24,093 | 23,929 | +676 ns (+2.89 %) | +512 ns (+2.19 %) |
| full cycle | 50,986 | 52,505 | 51,851 | +1,519 ns (+2.98 %) | +865 ns (+1.70 %) |
| receive→reconcile | 27,247 | 28,084 | 27,470 | +837 ns | +223 ns |

p95 / p99 (ns):

| Metric | off | healthy | stalled |
|---|---|---|---|
| decide | 29,573 / 69,925 | 41,846 / 74,668 | 27,922 / 70,185 |
| full cycle | 77,516 / 107,321 | 97,895 / 139,425 | 77,504 / 104,624 |
| receive→reconcile | 35,863 / 72,613 | 52,077 / 80,806 | 34,639 / 73,153 |

### The P8C gate, unchanged

| Limit | Target | P8C | P11C healthy | Verdict |
|---|---|---:|---:|---|
| Full-cycle p50 overhead | ≤ 5,000 ns | 955 | **+1,519** | **MET** |
| Full-cycle p50 overhead | ≤ 5 % | 2.9 % | **+2.98 %** | **MET** |
| Decide p50 overhead | ≤ 1,000 ns | 454 | **+676** | **MET** |
| Decide p50 overhead | ≤ 3 % | 1.73 % | **+2.89 %** | **MET** |

**Stated plainly: decide p50 is at +2.89 % against a 3 % limit.** That is met, and it is not
comfortable. Two things about the measurement are worth knowing before reading it as a
production figure, and neither is offered as an excuse:

* The replay drives 12,000 events as fast as the interpreter can, while the real baseline market
  produced 114,287 decisions over 300 seconds — roughly 380/s. The writer is saturated here and
  is not in production, so this bounds the overhead **above**.
* The healthy p95 tail (+12,273 ns on decide) is writer GIL contention, and it is absent from the
  stalled run, which does the same publication and no writing. That is where the remaining cost
  lives if it ever needs reducing.

No limit was moved.

## Fresh real market

`btc-updown-5m-1787780700`

| | |
|---|---|
| Decisions persisted | **114,287** |
| Risk records persisted | **114,288**, continuously |
| Fills | 0 — no order has ever been placed |
| Settlement | 1 — resolved **DOWN** |
| Drops / gaps / sink errors | **0 / 0 / 0** |
| Risk accepted / persisted / dropped | 114,288 / 114,288 / **0** |
| Buffer high-water | 377 |
| `telemetry_complete` | **true** |
| Verification | **COMPLETE** — all 27 checks |
| Archive | 597,741,568 → **10,916,604 B** (54.8×) in 45.1 s, restore verified in 1.8 s |
| Verified read-back | **opened, re-verified COMPLETE** |

Audited **through the verified archive path**, not from the runner's counters:

```text
decision_risk_copy_mismatches          0
decisions_naming_an_absent_risk_row    0
decisions_with_no_event_id             0
places_by_risk_state (from RiskRow)    {"SAFE": 678}
risk_states (from RiskRow)             {"SAFE": 114284, "HALTED": 2, "RECOVERING": 1}
risk sequence                          0 … 114,287, exact
storage order                          1 … 228,576, exact, 0 duplicates
actions   KEEP 112,635 · BLOCKED 99,168 · NOTHING 15,415 · PLACE 678 · REPLACE 424 · CANCEL 254
```

**PLACE under a persisted `RiskRow` that was HALTED: 0. RECOVERING: 0.** All 678 under `SAFE`.
The 2 HALTED and 1 RECOVERING records are the startup window before P6 established health.

## The stalled market is retained, and rechecked

P11B's `btc-updown-5m-1787771100` is **not rerun**. The producer/consumer hot path did not change
— only the store's INSERT semantics and the verifier's checks — so spending another real market
to reproduce the same fault would produce the same evidence.

It was re-verified under the *new* verifier rather than assumed still valid:

```text
INCOMPLETE
  risk sequence spans 0..112155 but holds only 64922 of 112156
  516 decision(s) name a risk_sequence that is not stored
  47234 risk record(s) were dropped
  112156 risk records accepted, 64922 persisted
  46718 records were dropped by the bounded buffer
  4 gap(s), 46718 observations lost
```

`decision_risk_copies_agree` passes: the copies that survived agree with the rows that survived.
The market is incomplete because records are missing, not because any surviving record lies.

## Still not done

* **Real own-fill durable record: UNRUN / P14.** The path is complete and exercised by
  constructed captures; no venue fill exists to travel it.
* **Real maker fraction: UNRUN / P14.**
* **Real taker-fill persistence: UNRUN / P14.**
* **Real nonzero own-ledger settlement analytics: UNRUN / P14.**

No mock upgrades any of these. **P11 closes no strategy open item**; O07 remains OPEN, and
incomplete telemetry may not be used by P15 to close one.
