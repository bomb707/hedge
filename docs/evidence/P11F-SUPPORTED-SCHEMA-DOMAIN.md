# P11F — supported schema domain closure

**Read-side only.** The diff against P11E is `verify.py`, one added constant in `schema.py`, and
tests. No runtime, writer, hot path, risk publication or archive semantics changed, so the
existing real evidence is re-verified rather than re-gathered and no market was spent.

**Capture date:** 2026-08-27 (UTC). `LIVE_TRADING_ENABLED` is `False`; `REDEMPTION_ENABLED` is
`False`. No order, no credential, no chain write.

## The defect

P11E proved the column and the payload said the *same* thing about the schema version. It never
asked whether the thing they said named a contract.

```python
if effective is not None and effective < DECISION_SCHEMA_VERSION:
    continue  # V1 exemption
```

"Older than current" is not a definition of a contract. A record stamped `0` or `-1` in both
places satisfied it and collected V1's exemption from every V2 rule — the risk reference, copy
completeness, and the PLACE contract. A record stamped `3` was validated as though it were the
current schema.

Separately, `isinstance(payload_version, int)` accepted `True`, because `bool` subclasses `int`
and `True == 1`. The same equality trap holds for `1 == 1.0`.

## The domain

```python
SUPPORTED_DECISION_SCHEMA_VERSIONS = frozenset({1, DECISION_SCHEMA_VERSION})  # {1, 2}
```

Enumerated, not a range. The rule in order:

```text
payload version is not an exact int   -> the record contradicts itself   -> INCOMPLETE
column version != payload version     -> the record contradicts itself   -> INCOMPLETE
version not in the supported set      -> we cannot read it               -> UNSUPPORTED
version == 1                          -> historical V1 semantics
version == 2                          -> current V2 semantics
```

There is no `version < current -> legacy` and no `version > current -> validate as current`.

### The chosen distinction

| Case | Result | Why |
|---|---|---|
| exact ints, equal, known version | read under that contract | it names something we defined |
| exact ints, equal, unknown version (`0`, `-1`, `3`, `999`) | **UNSUPPORTED** | a record we cannot read, which is not the same as a damaged one |
| not an exact int, or the two disagree | **INCOMPLETE** | a record that contradicts itself |

`UNSUPPORTED` outranks `INCOMPLETE`. "This build cannot read these records" is a more fundamental
answer than "some records are missing", and reporting the second would imply the first had been
judged.

## Types are part of what a record says

`1 == True` and `1 == 1.0` are both true in Python, so a payload storing a bool or a float where
an integer belongs agreed with its column while saying something else. Exact types are now
required before comparison, for the version and for every duplicated identity field:

| Field | Required type |
|---|---|
| `schema_version` | exact `int` |
| `ingress_ordinal` | exact `int` |
| `capture_sequence` | exact `int` |
| `market_id` | `str` |
| `event_id` | `str` |

This remains read-side integrity checking. It is not a cryptographic defence against rewriting
the database, the sidecar and the hashes together; the verified archive SHA is that layer.

## Discriminating tests

Fifteen fail against P11E. `SUPPORTING UNIT TEST ONLY`.

* versions `0`, `-1`, `3`, `999` agreeing in both places → **UNSUPPORTED**, with
  `decision_schema_version_self_consistent` still passing because the two really do agree;
* `True`, `2.0`, `"2"` as versions → refused on type;
* exact V1 → historical contract preserved, including its exemption from the V2 risk fields;
* exact V2 → current contract preserved;
* an unsupported version on an already-incomplete market → UNSUPPORTED, not INCOMPLETE;
* `ingress_ordinal`/`capture_sequence` as `True` or `1.0`, `market_id`/`event_id` as `int` →
  each refused on type.

## Real evidence, re-verified

### `btc-updown-5m-1787780700` — COMPLETE

```text
verification_status                     COMPLETE
telemetry_complete                      True
evidence_eligible                       True
decisions                               114,287
decisions_missing_risk_reference        0
decisions_with_incomplete_risk_copy     0
decisions_naming_an_absent_risk_row     0
decision_risk_copy_mismatches           0
decisions_with_no_event_id              0
decision_schema_version_supported       True
decision_schema_version_self_consistent True
decision_columns_match_payload          True
places_by_risk_state (from RiskRow)     {"SAFE": 678}
risk_states (from RiskRow)              {"SAFE": 114284, "HALTED": 2, "RECOVERING": 1}
verification_failures                   []
```

**678 PLACEs, all under a persisted `SAFE` RiskRow. HALTED: 0. RECOVERING: 0.**

### `btc-updown-5m-1787771100` — INCOMPLETE

Schema supported, self-consistent, and column/payload agreeing — all three pass. It fails only
for the genuine persistence loss the controlled stall caused: 47,234 risk records and 46,718
observations dropped across 4 gaps, and 516 decisions naming risk rows that are gone. As recorded
in P11D, those PLACEs did not happen under a halt; what was lost is the record of the verdicts,
so the audit cannot produce the permission.

## Unchanged

* **Real own-fill durable record: UNRUN / P14.**
* **Real maker fraction: UNRUN / P14.**
* **Real taker-fill persistence: UNRUN / P14.**
* **Real nonzero own-ledger settlement analytics: UNRUN / P14.**

P11 closes no strategy open item. O07 remains OPEN.
