# P11D — reference completeness closure

**Read-side only.** No runtime, writer, hot path, risk publication or archive semantics changed;
the diff against P11C is `src/maker5m/persistence/verify.py` plus tests and the query tool. The
existing real evidence is therefore re-verified rather than re-gathered, and no market was spent.

**Capture date:** 2026-08-27 (UTC). `LIVE_TRADING_ENABLED` is `False`; `REDEMPTION_ENABLED` is
`False`. No order, no credential, no chain write.

## The defect

```python
referenced = decision["risk_sequence"]
if referenced is None:
    continue
```

A V2 decision with no risk reference skipped everything that followed: the `RiskRow` join, all
three copy comparisons, and the PLACE contract. The check that existed to stop a decision
misrepresenting its verdict did not apply to a decision that declined to name one at all — which
is the *worse* audit fact of the two, and the one that was being waved through.

## The contract now

For every **V2** decision in a market claiming COMPLETE:

| Requirement | Check |
|---|---|
| `risk_sequence` present | `decision_risk_reference_present` |
| `risk_state`, `risk_allows_place`, `risk_allows_cancel` present | `decision_risk_copy_complete` |
| the sequence resolves to a stored `RiskRow` | `decision_risk_references_resolve` |
| each copied field **equals** that row | `decision_risk_copies_agree` |
| a PLACE's row exists, allows placement, and is `SAFE` | `no_place_without_permission` |
| the verdict was not taken later than the cycle | `risk_verdict_not_from_the_future` |
| `event_id` is non-empty | `decisions_carry_a_real_event_id` |
| indexed columns equal the payload they duplicate | `decision_columns_match_payload` |

The `if copied is not None` guard on the comparison is gone. **`None` and `False` are different
audit facts** — a row recording that placement was forbidden and a row recording nothing at all
are not the same claim — and neither is a pass. V1 rows are exempt: they predate these fields and
still mean what they meant when written.

A PLACE with no identifiable verdict, or naming a row that is not stored, now fails the
permission check rather than slipping past it.

## Discriminating tests

Eleven fail against P11C. `SUPPORTING UNIT TEST ONLY` — constructed stores, proving refusal paths.

* a PLACE whose four risk fields are nulled while every `RiskRow` stays intact;
* each of `risk_state`, `risk_allows_place`, `risk_allows_cancel` nulled on its own;
* a missing reference on a decision that placed nothing — the link is required for every
  decision, not only the ones that placed;
* `None` not masquerading as `False`;
* a blanked `event_id`, in both the column and the payload;
* each of `event_id`, `ingress_ordinal`, `capture_sequence` contradicting the payload.

## Real evidence, re-verified

### P11C baseline — `btc-updown-5m-1787780700`

Read through the verified archive path, sidecar identity checked, then verified for completeness:

```text
verification_status                    COMPLETE
telemetry_complete                     True
evidence_eligible                      True
decisions                              114,287
decisions_missing_risk_reference       0
decisions_with_incomplete_risk_copy    0
decisions_naming_an_absent_risk_row    0
decision_risk_copy_mismatches          0
decisions_with_no_event_id             0
places_by_risk_state (from RiskRow)    {"SAFE": 678}
risk_states (from RiskRow)             {"SAFE": 114284, "HALTED": 2, "RECOVERING": 1}
risk_exact_from_zero                   True
storage_exact_from_one                 True
storage_duplicates                     0
failures                               []
```

**678 PLACEs, all under a persisted `SAFE` RiskRow. HALTED: 0. RECOVERING: 0.**

### P11B stalled market — `btc-updown-5m-1787771100`

Still INCOMPLETE, and now for one reason more than before:

```text
risk sequence spans 0..112155 but holds only 64922 of 112156
516 decision(s) name a risk_sequence that is not stored
PLACE recorded against a persisted verdict that forbade it:
    ingress 71771 placed under risk_sequence 69918, which is not stored; ...
47234 risk record(s) were dropped
112156 risk records accepted, 64922 persisted
46718 records were dropped by the bounded buffer
4 gap(s), 46718 observations lost
```

**Read that failure precisely.** Those PLACEs did not happen under a halt — the live run's risk
was `SAFE` throughout, and the P11B evidence records 327 PLACEs all under SAFE. What the stalled
market lost is the *record* of the verdicts that governed them. The verifier refuses because the
audit cannot produce the permission, not because permission was absent. That is the correct
reading of an incomplete audit and the reason this market is not evidence.

## Identity is not completeness

`tools/p11_query.py` now reports both, apart:

* `open_verified_archive` proves the artifact **is** the market its sidecar names;
* `verify_store` then answers whether that market is **whole**.

`evidence_eligible` is true only for COMPLETE telemetry. P12 may display an incomplete market;
P15 must not close an open item from one, and the output says so in words rather than leaving it
to be inferred from a status code.

## Unchanged

* **Real own-fill durable record: UNRUN / P14.**
* **Real maker fraction: UNRUN / P14.**
* **Real taker-fill persistence: UNRUN / P14.**
* **Real nonzero own-ledger settlement analytics: UNRUN / P14.**

P11 closes no strategy open item. O07 remains OPEN.
