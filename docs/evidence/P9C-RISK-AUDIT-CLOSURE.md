# P9C risk-audit closure

**Provenance: `CONTROLLED_LOCAL_FAULT_ON_REAL_MARKET`.** Real Polymarket CLOB and real Binance
BTC data throughout; the faults are deliberately induced local failures. No orders, no
credentials, no authenticated socket.

This closes one narrow defect in the **risk replay verifier**. Nothing about risk behaviour
changed. The P9B market findings — BTC staleness, CLOB disconnect, a genuine venue disconnect,
recovery, latching, zero PLACE outside `SAFE` — remain valid and are not withdrawn; only P9B's
*sequence-integrity claim* is superseded.

## The defect

The verifier used the risk sequence to address records without ever proving it.

```python
expected = records[0].risk_sequence if records else 0
```

The expectation was derived from the data, so a trace whose prefix had been lost — `3, 4, 5` —
verified happily as "internally contiguous". The scan also only looked for values that skipped
*ahead*, so `0, 1, 1, 2` (duplicate) and `0, 1, 2, 1` (backwards) both passed. And
`verify_risk_replay` compared `state`, `active`, `latched`, `allows_place`, and `allows_cancel`
against each record while never comparing the sequence it *produced* against the sequence that
was *recorded* — leaving the one number the whole audit is indexed by unverified.

## The contract now

Positional and absolute: **`record[i].risk_sequence == i`**, checked before anything is replayed.
One rule covers every way a permission audit can be incomplete, and the expectation is not
inferred from the file — a trace starting at 5 is a trace whose first five permission decisions
are unaccounted for, and letting the data say where it ought to begin is exactly how that becomes
invisible.

`produced.risk_sequence` is then compared to `recorded.risk_sequence` alongside the verdict
fields, so the index is proved rather than assumed.

Partial replay stays deliberately unsupported. If it is ever wanted it must arrive with an
explicit initial sequence **and** an explicit initial `RiskSnapshot`, because replaying a tail
without the state it inherited proves nothing.

### What that means for a dropped trace

`RiskTrace` is a bounded drop-oldest ring, which is right for the hot path. A trace that has
dropped records therefore **cannot verify**: its first retained sequence is greater than zero.
That is the correct outcome. Trading may continue under the existing safety policy, but the
evidence may not claim deterministic full-risk replay. Nothing renumbers the tail, pretends it
starts at zero, or supplies a reconstructed initial state — a dropped trace is **audit
incomplete**, and says so.

## Malformed-trace tests

**SUPPORTING UNIT TEST ONLY** — constructed traces exercising the verifier, not market evidence.

| Case | Sequence | Result |
| --- | --- | --- |
| valid | `0,1,2,3,…` | **PASS** |
| lost prefix | `3,4,5,…` | FAIL — expected 0, actual 3 |
| missing middle | `0,1,3,4,…` | FAIL — expected 2, actual 3 |
| duplicate | `0,1,1,2,…` | FAIL — expected 2, actual 1 |
| backwards | `0,1,2,1,…` | FAIL — expected 3, actual 1 |
| globally shifted | `100,101,102,…` | FAIL — expected 0, actual 100 |
| tampered index, verdicts intact | one record renumbered | FAIL on `risk_sequence` |
| genuinely sliced trace | real records `[3:]` | FAIL — expected 0, actual 3 |
| overflowed bounded trace | capacity 16, 100 records | FAIL — expected 0, actual 84 |
| complete non-overflowing trace | capacity 256, 100 records | **PASS** |

Two further tests pin what must **not** change: several distinct risk sequences may legally share
one `as_of_ingress_ordinal` (more than one evaluation or signal can occur between market events),
and the out-of-order ingress rule and reconciliation rules are unaffected.

**The tests discriminate.** Restoring the previous verifier makes seven of them fail — lost
prefix, duplicate, backwards, shifted, sliced, overflow, and the produced-versus-recorded
comparison. ("Missing middle" passed under the old verifier too; its forward-gap scan did catch
that one case.)

## Fresh real market

| Field | Value |
| --- | --- |
| Market slug | `btc-updown-5m-1787692200` |
| Provenance | `CONTROLLED_LOCAL_FAULT_ON_REAL_MARKET` |
| Cycles | 159,952 |
| CLOB messages | 155,549 |
| BTC (Binance) messages | 7,548 |
| Reconnects | 1 (the induced one) |
| Malformed | 1 (pre-`T0`, warm-up) |
| Orders sent | **0** |
| `live_trading_enabled` | **false** |
| Raw data | `p9c-faults-btc-updown-5m-1787692200.json` |

### Sequence integrity, stated explicitly

| Metric | Value |
| --- | ---: |
| First risk sequence | **0** |
| Last risk sequence | **159,972** |
| Record count | **159,973** |
| Distinct sequences | 159,973 |
| **Duplicates** | **0** |
| **Gaps** | **0** |
| **Dropped** | **0** |
| Contiguous from zero | **true** |
| Non-evaluation signals | 18 |
| **`verify_risk_replay`** | **PASS** |

The passing replay is itself the proof: it requires `record[i].risk_sequence == i` and compares
the produced sequence to the recorded one, so a trace that started late, skipped, repeated, or
went backwards could not have reached this line.

### Risk behaviour, unchanged

Eleven halts, **thirty-three transitions, every one through `RECOVERING`**. No direct
`HALTED → SAFE` anywhere.

| Cause | Reaction | Halt seq | Halt ordinal | `RECOVERING` seq | `SAFE` seq | Halted | Reason | Latched |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| `T0`, spot `UNKNOWN` | — | 0 | 2 | 161 | 162 | 247 ms | `SPOT_STALE` | — |
| `btc_stale` | 4,938 ms | 51,716 | 52,929 | 61,545 | 61,546 | 15,365 ms | `SPOT_STALE` | — |
| `clob_disconnect` | 37 ms | 98,929 | 101,041 | 98,981 | 98,982 | 741 ms | `CLOB_CONTINUITY_UNCERTAIN` | — |
| `continuity_uncertain` | 0 ms | 142,782 | 145,733 | 142,791 | 142,792 | 82 ms | `CLOB_CONTINUITY_UNCERTAIN` | — |
| `clock_drift` | 0 ms | 147,797 | 150,940 | 148,654 | 148,655 | 4,018 ms | `CLOCK_DRIFT` | — |
| `order_state_uncertain` | 0 ms | 150,115 | 153,270 | 150,986 | 150,987 | 6,090 ms | `ORDER_STATE_UNCERTAIN` | **yes** |
| `position_mismatch` | 0 ms | 152,418 | 155,614 | 153,528 | 153,529 | 6,000 ms | `POSITION_MISMATCH` | **yes** |
| `cost_ledger_mismatch` | 0 ms | 154,536 | 157,747 | 155,275 | 155,276 | 6,000 ms | `COST_LEDGER_MISMATCH` | **yes** |
| `api_error_rate` | 0 ms | 155,995 | 159,209 | 156,369 | 156,370 | 3,987 ms | `API_ERROR_RATE` | — |
| `rate_limit_uncertain` | 0 ms | 157,208 | 160,421 | 157,561 | 157,562 | 3,994 ms | `RATE_LIMIT_UNCERTAIN` | — |
| `resolution_ambiguous` | 0 ms | 158,360 | 161,575 | 158,685 | 158,686 | 4,020 ms | `RESOLUTION_AMBIGUOUS` | — |

The 4,938 ms `btc_stale` reaction is P6's five-second threshold, not a delay — P9 has no timer of
its own. The 741 ms `clob_disconnect` halt is the real reconnect, resubscribe, and fresh
authoritative snapshot.

The three latching reasons show the required three-step in the trace:

```text
seq=150,115  ORDER_RECONCILIATION_RESULT     HALTED      place=False  latched=[ORDER_STATE_UNCERTAIN]
seq=150,986  ORDER_RECONCILIATION_RESULT     RECOVERING  place=False  latched=[ORDER_STATE_UNCERTAIN]
seq=150,987  RECONCILIATION_CONFIRMED        SAFE        place=True   latched=[]
```

The middle row is the point: the condition cleared and **permission did not return**.

| Shadow action | `SAFE` | `HALTED` | `RECOVERING` |
| --- | ---: | ---: | ---: |
| PLACE | 473 | **0** | **0** |
| CANCEL | 361 | 3 | 0 |

Zero placements outside `SAFE` across eleven halts and 14,680 halted cycles. 837 actions, each
attributed to the risk sequence that permitted it. `keep_ratio` 0.99534.

`RiskEngine` evaluation through the full ordered path: n = 159,955, p50 24,953 ns, p95 100,654,
p99 128,661.

## Reproducing

```bash
.venv/bin/python tools/risk_market.py <output-directory> --mode faults
```

Read-only: `LIVE_TRADING_ENABLED` is `False`, no credential is read, no authenticated socket is
opened, and no order of any size is sent.
