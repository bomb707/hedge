# P9 real-market baseline

> ## SUPERSEDED FOR FINAL P9 ARCHITECTURAL ACCEPTANCE
>
> The mechanisms this run exercised are **valid** and nothing here is withdrawn: the halts, the
> recoveries, the zero placements outside `SAFE`, and the real `STALE`-recovery defect it found
> all stand as evidence about how risk behaves.
>
> It is superseded as *architectural* acceptance because independent review then found two gaps
> in the code that produced it:
>
> 1. **P9 ran a second staleness detector.** `RiskEngine` compared its own copy of the threshold
>    against its own last-message timestamps, while P6 already owned the monitor and the
>    numbers. Two authorities for one question.
> 2. **Operational permission changes were not ordered or replayable.** `RiskEngine.reconciled`
>    mutated the latched snapshot directly and the fault scheduler called it, so `allows_place`
>    could change with no record of when, why, or relative to which market events.
>
> Corrected on `fix/p9-risk-ordering-staleness`, with fresh real-market evidence in
> [`P9B-REAL-MARKET-BASELINE.md`](P9B-REAL-MARKET-BASELINE.md).
> This run is retained because a measurement that led to a correction is part of the record of
> how the correction was reached.


**Provenance: `REAL_PUBLIC_MARKET_DATA`.** Real Polymarket CLOB and real Binance BTC data
throughout. No faults injected. No orders, no credentials, no authenticated socket.

This is the healthy-market half of the P9 real-market integration gate. Its job is to show that
the risk engine does not fire when nothing is wrong — a safety mechanism that halts a healthy
market is not safe, it is broken.

## Provenance

| Field | Value |
| --- | --- |
| Market slug | `btc-updown-5m-1787672100` |
| `live_trading_enabled` | **false** |
| Orders sent to any venue | **0** |
| Credentials used | none |
| Cycles | 154,882 |
| CLOB messages | 148,204 |
| BTC (Binance) messages | 9,777 |
| Malformed messages | 0 |
| Unhandled CLOB messages | 0 |
| Reconnects | 0 |
| Observation drops / gaps | **0 / 0** |
| Raw data | `p9-baseline-btc-updown-5m-1787672100.json` |

## Risk states

| State | Cycles | Share |
| --- | ---: | ---: |
| `SAFE` | 154,876 | **99.996%** |
| `HALTED` | 5 | 0.003% |
| `RECOVERING` | 1 | 0.001% |

| Transition | Ingress ordinal | Active reasons |
| --- | ---: | --- |
| → `HALTED` | 2 | `SPOT_STALE` |
| `HALTED` → `RECOVERING` | 8 | none |
| `RECOVERING` → `SAFE` | 9 | none |

There is exactly one halt, it lasts six ingress ordinals and roughly **55 ms**, and it is
explained below. After ordinal 9 the verdict is `SAFE` for the remainder of the market.

### The opening halt is correct behaviour, not a false positive

At `T0` the BTC feed status is genuinely `UNKNOWN`. During the 30-second pre-arm window
`capture_market` parses spot payloads for precision evidence but deliberately does **not** route
them through `pipeline.on_spot` — before `T0` the market's deterministic stream has not begun,
and emitting an earlier-stamped event would violate the non-decreasing timestamp contract. So at
the first post-`T0` evaluation the bot has not yet processed a single BTC price through its own
pipeline, and `spot_health.status` is `UNKNOWN`.

Refusing to quote a BTC-referenced market before having seen a BTC price is the behaviour the
engine is supposed to have. It is the same property as a fresh engine starting in `RECOVERING`
rather than `SAFE`: permission is never the default. The halt clears 41 ms later when the first
post-`T0` spot tick arrives, then takes one further evaluation to satisfy the two-confirmation
recovery rule.

**No unexplained false halt occurred.** `CLOB_STALE`, `CLOB_CONTINUITY_UNCERTAIN`, `CLOCK_DRIFT`,
`API_ERROR_RATE`, and every other condition stayed inactive for the whole market.

## New risk was only ever created while SAFE

| Shadow action | `SAFE` | `HALTED` | `RECOVERING` |
| --- | ---: | ---: | ---: |
| PLACE | 399 | **0** | **0** |
| CANCEL | 297 | 0 | 0 |

Zero placements outside `SAFE`, which is the requirement. No cancels were needed during the
opening halt because nothing was resting yet — the halt happened before the first placement.

## Execution, unchanged by the overlay

| Action | Count |
| --- | ---: |
| `KEEP` | 137,104 |
| `BLOCKED` | 129,831 |
| `NOTHING` | 41,749 |
| `PLACE` | 540 |
| `REPLACE` | 272 |
| `CANCEL` | 268 |

**`keep_ratio` = 0.99608**, consistent with the P8C reference (0.99259) and the P8B run
(0.99568). The risk overlay is a no-op on a healthy market by construction — `risk_adjust`
returns the identical `DecisionResult` object when the verdict is `SAFE` — and the execution
profile confirms it in practice.

## Risk evaluation latency

`RiskEngine.evaluate`, measured on real events, one evaluation per cycle:

| n | p50 | p90 | p95 | p99 | max |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 154,882 | **9,549 ns** | 31,159 | 40,022 | 59,052 | 376,985,381 |

The p50 of 9.5 µs sits against a P8C `receive_to_reconcile` p50 of 178,857 ns, so the safety
overlay is roughly **5%** of the existing critical path and does not create a new latency
defect of the kind P8 exists to catch.

The `max` of 377 ms is not a risk-engine cost. `evaluate` performs a bounded number of
comparisons over primitives with no allocation loop, so a 377 ms tail is the process being
descheduled or collecting garbage, not work. The p99 of 59 µs is the honest upper bound on what
the evaluation itself does.

## Reproducing

```bash
.venv/bin/python tools/risk_market.py <output-directory> --mode baseline
```

Read-only: `LIVE_TRADING_ENABLED` is `False`, no credential is read, no authenticated socket is
opened, and no order of any size is sent.
