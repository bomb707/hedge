# P11E — durable schema contract closure

**Read-side only.** The diff against P11D is `src/maker5m/persistence/verify.py` and its tests —
no runtime, writer, hot path, risk publication or archive semantics. The existing real evidence
is therefore re-verified rather than re-gathered, and no market was spent.

**Capture date:** 2026-08-27 (UTC). `LIVE_TRADING_ENABLED` is `False`; `REDEMPTION_ENABLED` is
`False`. No order, no credential, no chain write.

## Two holes, both absence read as agreement

### A — a missing payload duplicate was exempted

```python
if payload is not None and column != payload:
    inconsistent
```

Every decision has an indexed column for `market_id`, `ingress_ordinal`, `capture_sequence`,
`event_id` and `schema_version`. A payload that has lost its copy of one is a **damaged record**,
not a nullable one — and the guard above waved exactly that through. Missing is now its own
failure, with its own message naming the field.

### B — the schema version was not self-consistent

The effective version came from `decisions.schema_version` alone; the payload's copy was never
read. That was a downgrade bypass:

```text
column  schema_version = 1     ->  treated as V1
payload schema_version = 2         ->  exempt from every V2 rule
```

The V2 rules it bought exemption from are the ones P11D had just made load-bearing: the risk
reference, copy completeness, and the PLACE contract.

**Now:** both representations must be present and equal before any version-specific rule is
applied, and the V1 exemption requires a *proven* V1. A row whose versions disagree has proved
nothing about which contract it is under, so it is held to the current one — the downgrade is
worth nothing.

```text
read column version, read payload version
either missing        -> inconsistent
unequal               -> inconsistent
effective = column version   (only once they agree)
then apply V1/V2 rules
```

A genuine V1 row — both representations saying V1 — keeps its historical meaning. Reinterpreting
it under a contract that did not exist when it was written would make old evidence fail for not
having anticipated a later schema.

## What this is not

This checks a store's **internal consistency**. It is not a cryptographic defence against
somebody rewriting the database, the sidecar and the hashes together, and does not claim to be.
The verified archive SHA — checked by `open_verified_archive` before any query is answered —
remains the artifact-identity layer.

## New checks

| Check | Refuses |
|---|---|
| `decision_schema_version_self_consistent` | column and payload disagreeing about the version |
| `decision_columns_match_payload` | a missing **or** contradicting payload duplicate |

## Discriminating tests

Nine fail against P11D. `SUPPORTING UNIT TEST ONLY` — constructed stores proving refusal paths.

* payload missing `market_id` / `ingress_ordinal` / `capture_sequence` / `schema_version`;
* column V1 / payload V2, and column V2 / payload V1 — neither direction believed over the other;
* a row with disagreeing versions **and** a missing risk reference, which must still fail the V2
  risk requirement, so the downgrade buys nothing;
* a genuine V1 row keeping its exemption;
* a consistent V2 market passing.

## Real evidence, re-verified

### `btc-updown-5m-1787780700` — COMPLETE

Through the verified archive path, sidecar identity checked, then verified for completeness:

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
decision_schema_version_self_consistent True
decision_columns_match_payload          True
places_by_risk_state (from RiskRow)    {"SAFE": 678}
risk_states (from RiskRow)             {"SAFE": 114284, "HALTED": 2, "RECOVERING": 1}
risk_exact_from_zero                   True
storage_exact_from_one                 True
storage_duplicates                     0
verification_failures                  []
```

**678 PLACEs, all under a persisted `SAFE` RiskRow. HALTED: 0. RECOVERING: 0.**

### `btc-updown-5m-1787771100` — INCOMPLETE

Its schema and identity are intact — `decision_schema_version_self_consistent` and
`decision_columns_match_payload` both pass. It fails only for the genuine persistence loss the
controlled stall caused:

```text
risk sequence spans 0..112155 but holds only 64922 of 112156
516 decision(s) name a risk_sequence that is not stored
PLACE recorded against a persisted verdict that forbade it: ...
47234 risk record(s) were dropped
112156 risk records accepted, 64922 persisted
46718 records were dropped by the bounded buffer
4 gap(s), 46718 observations lost
```

As recorded in P11D: those PLACEs did not happen under a halt. The live run was SAFE throughout
and P11B records 327 PLACEs all under SAFE. What the stall lost is the record of the verdicts,
so the audit cannot produce the permission. That is the correct refusal for an incomplete audit.

## Query tool

Unchanged. `verify_store` is the single source of truth and `tools/p11_query.py` already reports
`verification_status`, `telemetry_complete`, `evidence_eligible` and the failure texts, so the
strengthened checks reach the supported P12/P15 read path without any verifier logic being
duplicated into it.

## Unchanged

* **Real own-fill durable record: UNRUN / P14.**
* **Real maker fraction: UNRUN / P14.**
* **Real taker-fill persistence: UNRUN / P14.**
* **Real nonzero own-ledger settlement analytics: UNRUN / P14.**

P11 closes no strategy open item. O07 remains OPEN.
