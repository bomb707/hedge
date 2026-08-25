# P9B controlled fault injection on a real market

**Provenance: `CONTROLLED_LOCAL_FAULT_ON_REAL_MARKET`.**

Real Polymarket CLOB and real Binance BTC data throughout. The **faults are deliberately induced
local failures** — our own adapter paused, our own socket dropped, our own risk signals emitted.
None is an observed venue incident, except one which is identified as such below. No orders, no
credentials, no authenticated socket.

Fresh evidence from the **corrected** code.
[`P9-REAL-MARKET-FAULTS.md`](P9-REAL-MARKET-FAULTS.md) is retained and marked superseded for
architectural acceptance.

## Provenance

| Field | Value |
| --- | --- |
| Market slug | `btc-updown-5m-1787679300` |
| Cycles | 162,644 |
| CLOB messages | 163,240 |
| BTC (Binance) messages | 3,073 |
| Reconnects | **2** — one induced, one genuine (see below) |
| Malformed | 1 (pre-`T0`, during warm-up) |
| Observation drops | 0 |
| Orders sent | **0** |
| Raw data | `p9b-faults-btc-updown-5m-1787679300.json` |

> **Risk-sequence verifier: SUPERSEDED FOR AUDIT-INTEGRITY ACCEPTANCE.**
>
> Everything this run *observed* stands: the BTC-stale halt, the induced CLOB disconnect, the
> genuine venue disconnect, the recoveries, the latching behaviour, and zero PLACE outside
> `SAFE` are all valid real-market evidence and none of it is withdrawn.
>
> What is superseded is the **verifier's** claim about sequence integrity. It derived its
> expectation from `records[0]`, so a trace whose prefix had been lost would have verified as
> internally contiguous, and it accepted duplicate and backwards sequences. It also never
> compared the sequence it produced against the sequence recorded. The trace below is in fact
> complete and contiguous from zero — that is checkable from the raw manifest — but the
> verifier of the day could not have proved it.
>
> Closed on `fix/p9-risk-sequence-integrity`, with a fresh real market in
> [`P9C-RISK-AUDIT-CLOSURE.md`](P9C-RISK-AUDIT-CLOSURE.md).

## The risk audit stream

| Metric | Value |
| --- | ---: |
| Risk schema version | 1 |
| Risk records written | **162,666** |
| Non-evaluation signals | **18** |
| Records dropped | **0** |
| Risk sequence range | 0 … 162,665 |
| **Sequence gaps** | **0** |
| **Replay verified** | **yes** |
| Shadow actions attributed to a risk sequence | 1,507 |

`verify_risk_replay` re-derived all 162,666 verdicts from their recorded signals and health
frames and matched every state, active set, latched set, `allows_place`, and `allows_cancel`.
The verification runs inside the tool before the manifest is written, so an unreplayable trace
could not have become evidence.

## Every halt, with its position in both streams

| Cause | Reaction | Halt seq | Halt ordinal | `RECOVERING` seq | `SAFE` seq | Halted | Reason | Latched |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| `T0`, spot `UNKNOWN` | — | 0 | 2 | 3 | 4 | 19 ms | `SPOT_STALE` | — |
| `btc_stale` | 4,083 ms | 24,857 | 25,366 | 34,267 | 34,268 | 16,365 ms | `SPOT_STALE` | — |
| `clob_disconnect` | 24 ms | 61,862 | 63,148 | 61,864 | 61,865 | 1,097 ms | `CLOB_CONTINUITY_UNCERTAIN` | — |
| **genuine venue disconnect** | — | 84,498 | 86,397 | 84,501 | 84,502 | 1,071 ms | `CLOB_CONTINUITY_UNCERTAIN` | — |
| `continuity_uncertain` | 0 ms | 99,983 | 102,197 | 99,986 | 99,987 | 59 ms | `CLOB_CONTINUITY_UNCERTAIN` | — |
| `clock_drift` | 0 ms | 111,105 | 113,547 | 112,982 | 112,983 | 4,008 ms | `CLOCK_DRIFT` | — |
| `order_state_uncertain` | 0 ms | 120,352 | 123,074 | 124,970 | 124,971 | 5,999 ms | `ORDER_STATE_UNCERTAIN` | **yes** |
| `position_mismatch` | 0 ms | 128,786 | 131,757 | 131,555 | 131,556 | 6,001 ms | `POSITION_MISMATCH` | **yes** |
| `cost_ledger_mismatch` | 0 ms | 137,119 | 140,555 | 141,956 | 141,957 | 5,993 ms | `COST_LEDGER_MISMATCH` | **yes** |
| `api_error_rate` | 0 ms | 147,791 | 151,900 | 150,778 | 150,779 | 3,907 ms | `API_ERROR_RATE` | — |
| `rate_limit_uncertain` | 0 ms | 155,892 | 160,518 | 158,116 | 158,117 | 4,001 ms | `RATE_LIMIT_UNCERTAIN` | — |
| `resolution_ambiguous` | 0 ms | 159,993 | 165,049 | 160,661 | 160,662 | 4,033 ms | `RESOLUTION_AMBIGUOUS` | — |

Twelve halts, thirty-six transitions, and **every one passed through `RECOVERING`**. There is no
direct `HALTED → SAFE` anywhere in the run.

**The 4,083 ms reaction to `btc_stale` is P6's threshold, not a delay.** P9 no longer has a timer
of its own: the consumer stops delivering spot payloads, P6's `StalenessMonitor` alone notices
the 5-second silence, `StreamHealth.mark_stale()` emits `STALE`, and P9 halts on reading it.

**The 1,097 ms `clob_disconnect` halt is the real recovery round trip** — socket close, backoff,
reconnect, resubscribe, fresh authoritative snapshot. `SAFE` was unreachable throughout, because
`CLOB_CONTINUITY_UNCERTAIN` stays active while `awaiting_snapshot` is set.

### One halt was a real venue incident, and is labelled as one

The halt at risk sequence **84,498** matches no scheduled fault, and the feed counters record
**two** reconnects where only one was induced. A genuine Polymarket websocket disconnect occurred
mid-market and was handled by exactly the same path: halt, real reconnect, real resubscription,
fresh snapshot, `RECOVERING`, `SAFE` — 1,071 ms, within a few percent of the induced one.

It is called out rather than folded into the induced count, because a manifest that blurred the
two would be claiming a venue incident it had caused itself, or hiding one it had not.

## Latching reasons outlive their conditions — visible in the stream

The eighteen non-evaluation signals show the required three-step for each latching reason. Taken
verbatim from the trace:

```text
seq=120,352  ORDER_RECONCILIATION_RESULT     HALTED      place=False  latched=[ORDER_STATE_UNCERTAIN]
seq=124,970  ORDER_RECONCILIATION_RESULT     RECOVERING  place=False  latched=[ORDER_STATE_UNCERTAIN]
seq=124,971  RECONCILIATION_CONFIRMED        SAFE        place=True   latched=[]

seq=128,786  POSITION_RECONCILIATION_RESULT  HALTED      place=False  latched=[POSITION_MISMATCH]
seq=131,555  POSITION_RECONCILIATION_RESULT  RECOVERING  place=False  latched=[POSITION_MISMATCH]
seq=131,556  RECONCILIATION_CONFIRMED        SAFE        place=True   latched=[]

seq=137,119  COST_RECONCILIATION_RESULT      HALTED      place=False  latched=[COST_LEDGER_MISMATCH]
seq=141,956  COST_RECONCILIATION_RESULT      RECOVERING  place=False  latched=[COST_LEDGER_MISMATCH]
seq=141,957  RECONCILIATION_CONFIRMED        SAFE        place=True   latched=[]
```

The middle row of each group is the point: **the condition cleared and permission did not
return.** The state went to `RECOVERING` with the reason still latched, and only the explicit
`RECONCILIATION_CONFIRMED` signal — standing in for an operator who has established which side
was wrong — restored `SAFE`. Every step has a risk sequence and an ingress ordinal.

Nothing mutated permission outside this path. `RiskController` is the single owner, and a
structural test asserts no code outside it calls `engine.reconciled`.

## New risk was only ever created while SAFE

| Shadow action | `SAFE` | `HALTED` | `RECOVERING` |
| --- | ---: | ---: | ---: |
| PLACE | 805 | **0** | **0** |
| CANCEL | 693 | **9** | 0 |

Zero placements outside `SAFE`, across twelve halts and 29,401 halted cycles. Nine cancels *did*
occur while halted — that is the design working: a halt empties the desired intent, P7's
minimal-action reconciler plans `CANCEL` for whatever is resting, and withdrawing a quote is
permitted at all times because it reduces risk.

Each of the 1,507 actions carries the risk sequence that permitted it, so "why was this PLACE
permitted?" resolves to a specific record with its signal, health frame, active reasons, and
verdict.

No SELL, hedge, flatten, merge, split, or convert exists anywhere in the risk package, asserted
structurally over function, class, and attribute names. Balances were held throughout.

| Risk state | Cycles |
| --- | ---: |
| `SAFE` | 133,253 |
| `HALTED` | 29,401 |
| `RECOVERING` | 12 |

`keep_ratio` 0.99177.

## A defect this run's predecessor found

The **first** P9B fault run halted on `API_ERROR_RATE` for **0 ms** — one risk sequence. The
injector forced the condition on, and the very next evaluation saw the real `ApiErrorMonitor`
report no failures and emitted a clearing signal. Two sources were writing the same condition and
contradicting each other.

Fixed in the injector, not the engine: while an API fault is scheduled the injector owns that
condition and the monitor-derived update is suppressed. The corrected run holds the halt for
3,907 ms, as intended. The failure is worth recording because the ordered trace is what made it
visible at all — two `API_ERROR_STATE_UPDATE` records one sequence apart, with opposite flags.

## Risk latency

| Path | n | p50 | p95 | p99 |
| --- | ---: | ---: | ---: | ---: |
| `controller.evaluate` (whole ordered path) | 162,648 | 27,472 ns | 98,873 | 125,698 |
| Non-evaluation signal application | 18 | 41,274 ns | — | 77,540 |

Measured in isolation on an idle machine the complete ordered path costs **4,728 ns**; the live
p50 reflects a machine also running a full market capture. Nothing on the path encodes JSON,
writes a file, touches a database, or formats a log line — the trace is a bounded in-memory ring
and P11 owns durable persistence.

## Reproducing

```bash
.venv/bin/python tools/risk_market.py <output-directory> --mode faults
```

Read-only: no credential is read, no authenticated socket is opened, and no order of any size is
sent. The fault schedule is `INJECTED_FAULTS` in `tools/risk_market.py`.
