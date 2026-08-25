# P9 controlled fault injection on a real market

**Provenance: `CONTROLLED_LOCAL_FAULT_ON_REAL_MARKET`.**

The market data is real and uninterrupted throughout: real Polymarket CLOB, real Binance BTC,
real reconnect, real resubscription, real authoritative snapshot. The **faults are deliberately
induced local failures** — our own adapter paused, our own socket dropped, our own continuity
path forced into a resnapshot. None of them is an observed venue incident and none is described
as one. No synthetic market stream, generated book, or fabricated history was used.

No orders. No credentials. No authenticated socket. `LIVE_TRADING_ENABLED` is `False`.

## Provenance

| Field | Value |
| --- | --- |
| Market slug | `btc-updown-5m-1787673300` |
| Cycles | 136,928 |
| CLOB messages | 134,725 |
| BTC (Binance) messages | 4,824 |
| Reconnects | **1** (the induced one) |
| Malformed messages | 1 (pre-`T0`, during warm-up; counted, no event emitted) |
| Unhandled CLOB messages | 0 |
| Observation drops / gaps | **0 / 0** |
| Orders sent | **0** |
| Raw data | `p9-faults-btc-updown-5m-1787673300.json` |

## How each fault was induced

| Fault | Mechanism | What stayed real |
| --- | --- | --- |
| `btc_stale` | The consumer stops delivering spot payloads to itself for 20 s | The Binance socket stays connected and real BTC trades keep arriving |
| `clob_disconnect` | The producer raises through its own failure path, closing the socket | The reconnect, backoff, resubscription, and book snapshot are all genuine |
| `continuity_uncertain` | `pipeline.on_uncertain(CLOB_BOOK, …)` — the same call a malformed frame makes | The resnapshot that clears it is a real venue book message |

## Results

| Fault | Inject ordinal | Halt ordinal | Reaction | Reason | Halted for | SAFE ordinal |
| --- | ---: | ---: | ---: | --- | ---: | ---: |
| `btc_stale` | 27,143 | 30,263 | **4,806 ms** | `SPOT_STALE` | 15,840 ms | 41,775 |
| `clob_disconnect` | 77,730 | 77,732 | **39 ms** | `CLOB_CONTINUITY_UNCERTAIN` | 1,050 ms | 77,738 |
| `continuity_uncertain` | 118,224 | 118,225 | **0 ms** | `CLOB_CONTINUITY_UNCERTAIN` | 58 ms | 118,228 |

Every halt passed through `RECOVERING` on its way back to `SAFE`. There were no direct
`HALTED → SAFE` transitions.

**The 4,806 ms reaction to `btc_stale` is the threshold, not a delay.** The configured spot
staleness limit is 5 s (`OPERATIONAL`, owned by P6), so a feed that has been quiet for 4.8 s is
not yet stale by definition. Halting sooner would mean halting on ordinary quiet periods.

**The 1,050 ms `clob_disconnect` halt is the real recovery round trip** — socket close, backoff,
reconnect, resubscribe, and a fresh authoritative book snapshot. `SAFE` was unreachable for its
whole duration, because `CLOB_CONTINUITY_UNCERTAIN` stays active while
`clob_health.awaiting_snapshot` is set and only `mark_snapshot` clears it.

`continuity_uncertain` recovered in 58 ms, *before* its scheduled one-second release window
closed. The release marker at ordinal 118,631 is bookkeeping for the injector; the recovery had
already happened on the next real book message.

## Risk states

| State | Cycles | Share |
| --- | ---: | ---: |
| `SAFE` | 125,293 | 91.5% |
| `HALTED` | 11,634 | 8.5% |
| `RECOVERING` | 4 | 0.003% |

| Halts by reason | Count |
| --- | ---: |
| `SPOT_STALE` | 2 |
| `CLOB_CONTINUITY_UNCERTAIN` | 2 |

Four halts, all accounted for: one at `T0` because the BTC feed status is genuinely `UNKNOWN`
before the first spot tick is routed through the pipeline (see the baseline manifest), and three
induced. **No unexplained halt occurred.**

## New risk was only ever created while SAFE

| Shadow action | `SAFE` | `HALTED` | `RECOVERING` |
| --- | ---: | ---: | ---: |
| PLACE | 353 | **0** | **0** |
| CANCEL | 272 | **2** | **1** |

Zero placements outside `SAFE`, which is the requirement. Three cancels *did* occur outside
`SAFE` — that is the design working: a halt empties the desired intent, P7's minimal-action
reconciler therefore plans `CANCEL` for whatever is resting, and withdrawing a quote is
permitted at all times because it reduces risk.

No SELL, hedge, flatten, merge, split, or convert exists anywhere in the risk package, asserted
structurally over function, class, and attribute names. Balances were held throughout.

## Execution, and the overlay's cost

| Action | Count |
| --- | ---: |
| `KEEP` | 111,999 |
| `BLOCKED` | 102,414 |
| `NOTHING` | 58,447 |
| `PLACE` | 498 |
| `REPLACE` | 283 |
| `CANCEL` | 215 |

`keep_ratio` **0.99557**, in line with the baseline (0.99608) and P8C (0.99259).

`RiskEngine.evaluate` on real events: n = 136,931, p50 **12,712 ns**, p90 39,651, p95 42,112,
p99 65,654. The `max` of 380 ms is the process being descheduled, not work — `evaluate` performs
a bounded number of comparisons over primitives with no allocation loop.

## A real defect this run found

The **first** fault run halted correctly on `btc_stale` and then never recovered. It stayed
`HALTED` for 132,717 of 160,917 cycles, which also masked the two later faults entirely.

`StreamHealth` had no path out of `STALE`. `mark_message` updated the timestamp and nothing
else, and `mark_snapshot` was only reachable while `awaiting_snapshot` was set — which `STALE`
does not set. One quiet BTC feed would have halted the bot for the remainder of a market.

Fixed in `1584dee`: a stream marked `STALE` returns to `HEALTHY` when a message arrives, because
`STALE` means "has said nothing for too long" and a message is the direct refutation of that.
`DISCONNECTED` and `SEQUENCE_GAP` are deliberately not cleared this way — they set
`awaiting_snapshot`, and a single message after a continuity break says nothing about the
messages that were missed.

**No synthetic data was involved in finding this.** It was invisible to a green unit-test suite
and appeared the moment a real adapter was paused during a real market, which is the case for
the evidence policy in `ARCHITECTURE_SSOT.md` §4.4.

## Reproducing

```bash
.venv/bin/python tools/risk_market.py <output-directory> --mode faults
```

Read-only: no credential is read, no authenticated socket is opened, and no order of any size is
sent. The fault schedule is `INJECTED_FAULTS` in `tools/risk_market.py`.
