# Evidence index

| Document | Phase | Status |
| --- | --- | --- |
| `p6-capture-*.manifest.json` | P6 | valid |
| [`P8-MEASUREMENT.md`](P8-MEASUREMENT.md) | P8, first run | **queue metrics SUPERSEDED** |
| [`P8B-MEASUREMENT.md`](P8B-MEASUREMENT.md) | P8, correction 1 | **valid correctness evidence; performance gate failed** |
| [`P8C-PERFORMANCE-CLOSURE.md`](P8C-PERFORMANCE-CLOSURE.md) | P8, correction 2 | **final performance closure** |
| [`P9-REAL-MARKET-BASELINE.md`](P9-REAL-MARKET-BASELINE.md) | P9 | valid mechanisms; **superseded for architectural acceptance** |
| [`P9-REAL-MARKET-FAULTS.md`](P9-REAL-MARKET-FAULTS.md) | P9 | valid mechanisms; **superseded for architectural acceptance** |
| [`P9B-REAL-MARKET-BASELINE.md`](P9B-REAL-MARKET-BASELINE.md) | P9 corrected | `REAL_PUBLIC_MARKET_DATA` |
| [`P9B-REAL-MARKET-FAULTS.md`](P9B-REAL-MARKET-FAULTS.md) | P9 corrected | valid market findings; **verifier claim superseded** |
| [`P9C-RISK-AUDIT-CLOSURE.md`](P9C-RISK-AUDIT-CLOSURE.md) | P9 final | `CONTROLLED_LOCAL_FAULT_ON_REAL_MARKET` |
| [`P10A-O11-RESOLUTION-RESEARCH.md`](P10A-O11-RESOLUTION-RESEARCH.md) | P10A | `REAL_PUBLIC_MARKET_DATA` — closes O11 |

Superseded documents are retained and labelled rather than deleted. A measurement that turned
out to be wrong is part of the record of how the right one was reached, and removing it would
leave the history reading as though the first answer had been correct.

Files named `*-SUPERSEDED-*` are runs kept for the same reason; each is explained in the
manifest that replaced it.

## Provenance labels

From P9 forward every market-facing manifest states its provenance explicitly
(`ARCHITECTURE_SSOT.md` §4.4):

* `REAL_PUBLIC_MARKET_DATA` — real venue and real BTC data, no interference.
* `CONTROLLED_LOCAL_FAULT_ON_REAL_MARKET` — real market data throughout, with a deliberately
  induced *local* failure. Never described as a venue incident.
* `UNRUN / DEFERRED` — the condition cannot yet be demonstrated against real venue or account
  behaviour. No mock is substituted.

---

# P6 capture evidence

Machine-readable evidence for the P6 acceptance gate. Each manifest describes one **real,
read-only** capture of a full `btc-updown-5m-*` market: counts by event type, observed decimal
precision, clock-health samples, pre-arm slack, and the P5 verification result.

## The journals themselves are not in Git

A single 5-minute market produces ~100–130k decision steps. Because every step records the
**complete** `DecisionResult` — which is deliberate, since a decision can be wrong in its
centre or eligibility while the emitted order looks identical — the canonical journal runs to
roughly 1.5 kB per step, or 150–200 MB per market. That is not a healthy thing to put in Git
history, so the journals are stored outside the repository and identified here by SHA256.

`STATUS.md` records the exact path, byte size, digest, and the command that reproduces them.

## Reproducing

```bash
.venv/bin/python tools/capture_market.py <output-directory>
```

Read-only: no credential, no key, no signing, no order endpoint. The run waits for the next
suitable `T0`, so it takes roughly nine minutes.
