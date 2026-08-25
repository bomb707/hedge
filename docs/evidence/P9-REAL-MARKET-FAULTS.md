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
| Market slug | `btc-updown-5m-1787674200` |
| Cycles | 112,958 |
| CLOB messages | 110,079 |
| BTC (Binance) messages | 5,267 |
| Reconnects | **1** (the induced one) |
| Malformed messages | 1 (pre-`T0`, during warm-up; counted, no event emitted) |
| Unhandled CLOB messages | 0 |
| Observation drops / gaps | **0 / 0** |
| Orders sent | **0** |
| Raw data | `p9-faults-btc-updown-5m-1787674200.json` |

## How each fault was induced

| Fault | Mechanism | What stayed real |
| --- | --- | --- |
| `btc_stale` | The consumer stops delivering spot payloads to itself for 20 s | The Binance socket stays connected and real BTC trades keep arriving |
| `clob_disconnect` | The producer raises through its own failure path, closing the socket | The reconnect, backoff, resubscription, and book snapshot are all genuine |
| `continuity_uncertain` | `pipeline.on_uncertain(CLOB_BOOK, …)` — the same call a malformed frame makes | The resnapshot that clears it is a real venue book message |
| Seven `signal:` faults | One risk input forced for a 4 s window | Both venue streams keep flowing untouched throughout |

The seven signal faults cover conditions a live venue cannot be asked to produce on demand: a
clock cannot be made to drift, an account cannot be made to disagree, and a write API that has
never been called cannot be made to fail. Their **integration** is exercised against real market
observations with the signal induced locally, which is what
`CONTROLLED_LOCAL_FAULT_ON_REAL_MARKET` means.

## Results — every inducible condition, on a real market

| Fault | Inject ordinal | Halt ordinal | Reaction | Reason | Halted for | SAFE ordinal |
| --- | ---: | ---: | ---: | --- | ---: | ---: |
| `btc_stale` | 28,087 | 30,822 | **4,754 ms** | `SPOT_STALE` | 16,821 ms | 41,265 |
| `clob_disconnect` | 68,519 | 68,521 | **25 ms** | `CLOB_CONTINUITY_UNCERTAIN` | 723 ms | 68,525 |
| `continuity_uncertain` | 96,129 | 96,130 | **0 ms** | `CLOB_CONTINUITY_UNCERTAIN` | 15 ms | 96,133 |
| `clock_drift` | 101,594 | 101,594 | **0 ms** | `CLOCK_DRIFT` | 4,040 ms | 102,688 |
| `order_state_uncertain` | 104,451 | 104,451 | **0 ms** | `ORDER_STATE_UNCERTAIN` | 4,025 ms | 105,365 |
| `position_mismatch` | 106,331 | 106,331 | **0 ms** | `POSITION_MISMATCH` | 4,057 ms | 107,388 |
| `cost_ledger_mismatch` | 108,999 | 108,999 | **0 ms** | `COST_LEDGER_MISMATCH` | 4,032 ms | 109,430 |
| `api_error_rate` | 110,339 | 110,340 | **16 ms** | `API_ERROR_RATE` | 4,015 ms | 110,809 |
| `rate_limit_uncertain` | 111,761 | 111,761 | **0 ms** | `RATE_LIMIT_UNCERTAIN` | 4,023 ms | 112,142 |
| `resolution_ambiguous` | 112,899 | 112,899 | **0 ms** | `RESOLUTION_AMBIGUOUS` | 4,022 ms | 113,258 |

Thirty-three risk transitions in one market, and **every one of them passed through
`RECOVERING`**. There is no direct `HALTED → SAFE` transition anywhere in the run.

**The 4,754 ms reaction to `btc_stale` is the threshold, not a delay.** The configured spot
staleness limit is 5 s (`OPERATIONAL`, owned by P6), so a feed quiet for 4.7 s is not yet stale
by definition; halting sooner would mean halting on ordinary quiet periods.

**The 723 ms `clob_disconnect` halt is the real recovery round trip** — socket close, backoff,
reconnect, resubscribe, fresh authoritative book snapshot. `SAFE` was unreachable for its whole
duration, because `CLOB_CONTINUITY_UNCERTAIN` stays active while `awaiting_snapshot` is set and
only `mark_snapshot` clears it.

**The latching reasons visibly outlived their conditions.** `ORDER_STATE_UNCERTAIN`,
`POSITION_MISMATCH`, and `COST_LEDGER_MISMATCH` each appear in the `latched` column of the
`HALTED → RECOVERING` transition and stay there until `RiskEngine.reconciled` is called
explicitly — which the injector does at release, standing in for an operator who has actually
established which side was wrong. Releasing the signal alone did **not** restore `SAFE`.

### Two conditions were not induced, and are not claimed

| Reason | Real-market status |
| --- | --- |
| `TAKER_FILL` | **UNRUN / DEFERRED TO P14** — no order was sent, so no fill of any kind occurred |
| `MAKER_ONLY_UNCERTAIN` | **UNRUN on a real market** — covered by supporting unit tests only |
| `CLOB_STALE` | Not reached: the CLOB never went quiet for 10 s. The same staleness code path was exercised by `SPOT_STALE` |

## Risk states

| State | Cycles | Share |
| --- | ---: | ---: |
| `SAFE` | 97,697 | 86.5% |
| `HALTED` | 15,253 | 13.5% |
| `RECOVERING` | 11 | 0.010% |

| Halts by reason | Count |
| --- | ---: |
| `SPOT_STALE` | 2 |
| `CLOB_CONTINUITY_UNCERTAIN` | 2 |
| `CLOCK_DRIFT` | 1 |
| `ORDER_STATE_UNCERTAIN` | 1 |
| `POSITION_MISMATCH` | 1 |
| `COST_LEDGER_MISMATCH` | 1 |
| `API_ERROR_RATE` | 1 |
| `RATE_LIMIT_UNCERTAIN` | 1 |
| `RESOLUTION_AMBIGUOUS` | 1 |

Eleven halts, all accounted for: one at `T0` because the BTC feed status is genuinely `UNKNOWN`
before the first spot tick is routed through the pipeline (see the baseline manifest), and ten
induced. **No unexplained halt occurred.**

## New risk was only ever created while SAFE

| Shadow action | `SAFE` | `HALTED` | `RECOVERING` |
| --- | ---: | ---: | ---: |
| PLACE | 245 | **0** | **0** |
| CANCEL | 175 | **2** | **1** |

Zero placements outside `SAFE`, across ten induced faults and 15,253 halted cycles. Three
cancels *did* occur outside `SAFE` — that is the design working: a halt empties the desired
intent, P7's minimal-action reconciler therefore plans `CANCEL` for whatever is resting, and
withdrawing a quote is permitted at all times because it reduces risk.

No SELL, hedge, flatten, merge, split, or convert exists anywhere in the risk package, asserted
structurally over function, class, and attribute names. Balances were held throughout.

## Execution, and the overlay's cost

| Action | Count |
| --- | ---: |
| `NOTHING` | 82,814 |
| `KEEP` | 73,507 |
| `BLOCKED` | 68,923 |
| `PLACE` | 336 |
| `REPLACE` | 187 |
| `CANCEL` | 149 |

`keep_ratio` **0.99545**, in line with the baseline (0.99608) and P8C (0.99259). The elevated
`NOTHING` count is the halts: an emptied intent with nothing resting plans nothing at all.

`RiskEngine.evaluate` on real events: n = 112,961, p50 **14,544 ns**, p90 42,108, p95 44,576,
p99 67,582 — roughly 8% of the P8C `receive_to_reconcile` p50 of 178,857 ns. The `max` of
~380 ms seen across these runs is the process being descheduled, not work: `evaluate` performs a
bounded number of comparisons over primitives with no allocation loop.

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

The corrected run at `btc-updown-5m-1787673300` — three faults, all recovering — is retained as
`p9-faults-SUPERSEDED-btc-updown-5m-1787673300.json`. It is superseded only because the run
documented above covers everything it did and seven conditions more.

## Reproducing

```bash
.venv/bin/python tools/risk_market.py <output-directory> --mode faults
```

Read-only: no credential is read, no authenticated socket is opened, and no order of any size is
sent. The fault schedule is `INJECTED_FAULTS` in `tools/risk_market.py`.
