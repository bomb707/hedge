# P9B real-market baseline

**Provenance: `REAL_PUBLIC_MARKET_DATA`.** Real Polymarket CLOB and real Binance BTC data
throughout, no faults injected, no orders, no credentials.

Fresh evidence from the **corrected** code. The earlier
[`P9-REAL-MARKET-BASELINE.md`](P9-REAL-MARKET-BASELINE.md) is retained and marked superseded for
architectural acceptance: the mechanisms it exercised were valid, but the code that produced it
ran a second staleness detector and let operational permission change without an ordered record.

## Provenance

| Field | Value |
| --- | --- |
| Market slug | `btc-updown-5m-1787678100` |
| `live_trading_enabled` | **false** |
| Orders sent to any venue | **0** |
| Credentials used | none |
| Cycles | 153,082 |
| CLOB messages | 152,738 |
| BTC (Binance) messages | 3,196 |
| Malformed / unhandled / reconnects | 0 / 0 / 0 |
| Observation drops | 0 |
| Raw data | `p9b-baseline-btc-updown-5m-1787678100.json` |

## The risk audit stream

| Metric | Value |
| --- | ---: |
| Risk schema version | 1 |
| Risk records written | **153,082** |
| Records retained (bounded trace) | 153,082 |
| Records dropped | **0** |
| Risk sequence range | 0 … 153,081 |
| **Sequence gaps** | **0** |
| **Replay verified** | **yes** |
| States reproduced | `HALTED`, `RECOVERING`, `SAFE` |

Every risk record was re-derived from its recorded signal and health frame by
`verify_risk_replay` and matched the recorded state, active set, latched set, `allows_place`,
and `allows_cancel`. The verification runs *inside* the tool before the manifest is written, so
an unreplayable trace could not have become evidence.

## Risk states

| State | Cycles | Share |
| --- | ---: | ---: |
| `SAFE` | 152,914 | **99.89%** |
| `HALTED` | 167 | 0.11% |
| `RECOVERING` | 1 | 0.001% |

| Transition | Risk sequence | Ingress ordinal | Active reasons |
| --- | ---: | ---: | --- |
| → `HALTED` | 0 | 2 | `SPOT_STALE` |
| `HALTED` → `RECOVERING` | 167 | 172 | none |
| `RECOVERING` → `SAFE` | 168 | 173 | none |

One halt, and it is the explained one. At `T0` the BTC feed status is genuinely `UNKNOWN`:
during pre-arm `capture_market` parses spot payloads for precision evidence but deliberately does
not route them through `pipeline.on_spot`, because before `T0` the market's deterministic stream
has not begun. So the bot has not yet seen a BTC price through its own pipeline, and refusing to
quote a BTC-referenced market before seeing a BTC price is the behaviour the engine is supposed
to have.

**No unexplained halt occurred.** `CLOB_STALE`, `CLOB_CONTINUITY_UNCERTAIN`, `CLOCK_DRIFT`,
`API_ERROR_RATE`, and every other condition stayed inactive for the whole market.

## New risk was only ever created while SAFE

| Shadow action | `SAFE` | `HALTED` | `RECOVERING` |
| --- | ---: | ---: | ---: |
| PLACE | 679 | **0** | **0** |
| CANCEL | 538 | 0 | 0 |

**1,217 shadow actions, each attributed to the exact risk sequence that permitted it.** A sample
from the trace:

```json
{"action": "PLACE",  "ingress_ordinal": 1321, "risk_sequence": 1296, "risk_state": "SAFE"}
{"action": "CANCEL", "ingress_ordinal": 1964, "risk_sequence": 1917, "risk_state": "SAFE"}
```

That is what makes "why was this PLACE permitted?" answerable: the risk sequence names a record,
and the record carries the signal, the health frame, the active reasons, and the verdict. No
hidden mutable boolean has to be reconstructed.

## Staleness has exactly one owner

The manifest records it explicitly:

```json
"risk_config": {
  "feed_staleness_owner": "P6 (maker5m.feeds.health) - P9 holds no threshold",
  "clock_drift_limit_ns": 250000000,
  "api_error_window_ns": 30000000000,
  "api_error_threshold": 5,
  "recovery_confirmations": 2,
  "status": "OPERATIONAL"
}
```

`RiskConfig` carries no `clob_stale_after` and no `spot_stale_after`, and `RiskInputs` carries no
`last_message_at`. P6 holds the monitor, the thresholds, and the `mark_stale` transition; P9
reads the resulting `HealthStatus`.

## Risk latency

`RiskEngine` evaluation through the full ordered path — signal construction, state update,
verdict, record, and trace append:

| n | p50 | p90 | p95 | p99 |
| ---: | ---: | ---: | ---: | ---: |
| 153,082 | **18,299 ns** | 69,851 | 83,729 | 118,136 |

The comparison against P9's 9,549 ns is **not like-for-like**: that timer bracketed
`engine.evaluate(inputs)` only, with `RiskInputs` construction outside it. This one brackets the
whole ordered contract. Measured in isolation on an idle machine the complete path costs
**4,728 ns**, down from 6,390 ns before `RiskInputs`, `RiskSnapshot`, and `RiskDecision` became
`NamedTuple`s — the same frozen-dataclass cost P8 found twice.

Nothing on this path encodes JSON, writes a file, touches a database, or formats a log line; the
trace is a bounded in-memory ring and P11 owns durable persistence.

## Reproducing

```bash
.venv/bin/python tools/risk_market.py <output-directory> --mode baseline
```

Read-only: `LIVE_TRADING_ENABLED` is `False`, no credential is read, no authenticated socket is
opened, and no order of any size is sent.
