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
