"""Reading persisted telemetry back, and deciding whether to believe it.

The verifier's job is to be *unwilling*. It reports COMPLETE only when everything it can check
agrees; anything else — a market that never closed, a hole in a sequence, a count that does not
match its manifest, a file whose hash has moved — is INCOMPLETE or CORRUPT, and it never repairs
either. A telemetry store that quietly fixed itself would be a store whose contents mean nothing.

This matters beyond tidiness. P15 will use persisted markets to close open strategy items, and a
market with a hole in it cannot be allowed to look like one without. "Trading went fine" is not
evidence that the record of it is whole.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from maker5m.persistence.schema import (
    DECISION_SCHEMA_VERSION,
    MANIFEST_SCHEMA_VERSION,
    SUPPORTED_DECISION_SCHEMA_VERSIONS,
    Manifest,
)
from maker5m.persistence.store import SchemaVersionError, database_digest, open_for_read

__all__ = ["VerificationResult", "VerificationStatus", "read_manifest", "verify_store"]


class VerificationStatus(Enum):
    COMPLETE = "COMPLETE"
    """Every check passed. This market may be used as evidence."""

    INCOMPLETE = "INCOMPLETE"
    """Readable, and known to be missing something. Never silently upgraded."""

    CORRUPT = "CORRUPT"
    """Unreadable, or readable and self-contradictory."""

    UNSUPPORTED = "UNSUPPORTED"
    """A schema this build does not define. Refused rather than guessed at."""


@dataclass(frozen=True, slots=True)
class VerificationResult:
    status: VerificationStatus
    market_id: str | None
    slug: str | None
    checks: dict[str, bool] = field(default_factory=dict)
    failures: tuple[str, ...] = ()
    decision_rows: int = 0
    fill_rows: int = 0
    risk_rows: int = 0
    settlement_rows: int = 0
    manifest: Manifest | None = None

    @property
    def complete(self) -> bool:
        return self.status is VerificationStatus.COMPLETE

    def summary(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "market_id": self.market_id,
            "slug": self.slug,
            "checks": dict(sorted(self.checks.items())),
            "failures": list(self.failures),
            "decision_rows": self.decision_rows,
            "fill_rows": self.fill_rows,
            "risk_rows": self.risk_rows,
            "settlement_rows": self.settlement_rows,
        }


def read_manifest(connection: sqlite3.Connection, market_id: str) -> Manifest | None:
    row = connection.execute(
        "SELECT manifest_json FROM markets WHERE market_id = ?", (market_id,)
    ).fetchone()
    if row is None or row[0] is None:
        return None
    data: dict[str, Any] = json.loads(row[0])
    if int(data.get("schema_version", -1)) != MANIFEST_SCHEMA_VERSION:
        raise SchemaVersionError(f"manifest schema {data.get('schema_version')} is not supported")
    fields = {
        key: value
        for key, value in data.items()
        if key in {f.name for f in Manifest.__dataclass_fields__.values()}
    }
    fields["notes"] = tuple(fields.get("notes") or ())
    return Manifest(**fields)


def _has_control_audit(connection: sqlite3.Connection) -> bool:
    """Whether this store has the table at all.

    V2 stores — every accepted P11 archive — predate operator control, so the absence of the
    table is a fact about when they were written rather than a missing row.
    """
    row = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='control_audit'"
    ).fetchone()
    return row is not None


def _exact_int(value: object) -> bool:
    """An honest integer, and not a bool.

    `isinstance(x, int)` is true for `True` and `False`, so a payload storing `true` where a
    version or an ordinal belongs would compare equal to `1` and pass. A durable record's types
    are part of what it says, and `True` does not say `1`.
    """
    return type(value) is int


def _exact_from(values: list[int], first: int) -> bool:
    """Whether the sequence is exactly ``first, first+1, ... `` with nothing missing.

    Anchored to a known start, deliberately. An earlier version accepted a run beginning at any
    number, which meant a bounded channel that dropped its oldest entries produced a suffix —
    ``5000, 5001, 5002, ...`` — that looked perfectly contiguous. That is the same class of
    defect P9C already closed for the risk trace, and it is not being reopened: a lost prefix is
    the easiest loss to miss and the most expensive to discover later.
    """
    if not values:
        return True
    return values == list(range(first, first + len(values)))


def verify_store(
    path: Path, *, market_id: str | None = None, expected_sha256: str | None = None
) -> VerificationResult:
    """Check one persisted market end to end.

    ``expected_sha256`` comes from the sidecar manifest, not from inside the database, because a
    file cannot contain its own hash: writing the digest into the store would change the store
    and invalidate the digest in the same act. The sidecar is written once the database is
    closed and nothing is still touching it.

    Passing ``None`` skips the hash check and says so in ``checks``; it does not quietly pass.
    """
    try:
        connection = open_for_read(path)
    except SchemaVersionError as error:
        return VerificationResult(
            status=VerificationStatus.UNSUPPORTED,
            market_id=market_id,
            slug=None,
            failures=(str(error),),
        )
    except (FileNotFoundError, sqlite3.DatabaseError) as error:
        return VerificationResult(
            status=VerificationStatus.CORRUPT,
            market_id=market_id,
            slug=None,
            failures=(f"{type(error).__name__}: {error}",),
        )

    checks: dict[str, bool] = {}
    failures: list[str] = []
    try:
        markets = connection.execute(
            "SELECT market_id, slug, closed FROM markets"
            + ("" if market_id is None else " WHERE market_id = ?"),
            () if market_id is None else (market_id,),
        ).fetchall()
        if len(markets) != 1:
            return VerificationResult(
                status=VerificationStatus.CORRUPT,
                market_id=market_id,
                slug=None,
                failures=(f"expected exactly one market in the store, found {len(markets)}",),
            )
        found_id, slug, closed = str(markets[0][0]), str(markets[0][1]), bool(markets[0][2])

        decisions = connection.execute(
            "SELECT persistence_sequence, capture_sequence, market_id, ingress_ordinal"
            " FROM decisions WHERE market_id = ? ORDER BY persistence_sequence",
            (found_id,),
        ).fetchall()
        fills = connection.execute(
            "SELECT COUNT(*) FROM fills WHERE market_id = ?", (found_id,)
        ).fetchone()[0]
        risks = connection.execute(
            "SELECT risk_sequence FROM risk_records WHERE market_id = ? ORDER BY risk_sequence",
            (found_id,),
        ).fetchall()
        settlements = connection.execute(
            "SELECT COUNT(*) FROM settlements WHERE market_id = ?", (found_id,)
        ).fetchone()[0]
        log_sequences = [
            int(row[0])
            for row in connection.execute(
                "SELECT persistence_sequence FROM persistence_log WHERE market_id = ?"
                " ORDER BY persistence_sequence",
                (found_id,),
            )
        ]
        log_total = len(log_sequences)
        control_rows = (
            [
                json.loads(row[0])
                for row in connection.execute(
                    "SELECT payload FROM control_audit WHERE market_id = ?", (found_id,)
                )
            ]
            if _has_control_audit(connection)
            else []
        )
        control_risk_rows = {
            int(row[0]): json.loads(row[1])
            for row in connection.execute(
                "SELECT risk_sequence, payload FROM risk_records WHERE market_id = ?"
                " AND json_extract(payload, '$.signal_kind') = 'OPERATOR_CONTROL'",
                (found_id,),
            )
        }
        # The reference a decision makes to the verdict that governed it, and whether it placed.
        # Pulled out of the payload because that is where V2 keeps them; the columns carry only
        # what is indexed.
        # The indexed columns and the payload are two representations of one record. Both are
        # read so they can be compared: trusting either silently would mean a store that
        # contradicts itself still verifies.
        decision_rows = []
        for row in connection.execute(
            "SELECT payload, market_id, ingress_ordinal, capture_sequence, event_id, schema_version"
            " FROM decisions WHERE market_id = ?",
            (found_id,),
        ):
            record = json.loads(row[0])
            column_version = int(row[5])
            payload_version = record.get("schema_version")
            # The effective version is only knowable once both representations agree, and
            # agreement is not enough on its own — see `supported` below. Deciding it from the
            # column alone was a downgrade bypass: a row whose column said 1 while its payload
            # said 2 collected V1's exemptions from every V2 rule.
            schema_agrees = _exact_int(payload_version) and payload_version == column_version
            decision_rows.append(
                {
                    "schema_version": column_version,
                    "payload_schema_version": payload_version,
                    "schema_agrees": schema_agrees,
                    "schema_supported": schema_agrees
                    and column_version in SUPPORTED_DECISION_SCHEMA_VERSIONS,
                    "effective_version": (
                        column_version
                        if schema_agrees and column_version in SUPPORTED_DECISION_SCHEMA_VERSIONS
                        else None
                    ),
                    "risk_sequence": record.get("risk_sequence"),
                    "place": record.get("up", {}).get("action") == "PLACE"
                    or record.get("down", {}).get("action") == "PLACE",
                    "risk_state": record.get("risk_state"),
                    "allows_place": record.get("risk_allows_place"),
                    "allows_cancel": record.get("risk_allows_cancel"),
                    "ingress_ordinal": record.get("ingress_ordinal"),
                    "event_id": record.get("event_id"),
                    "column_market_id": str(row[1]),
                    "column_ingress_ordinal": int(row[2]),
                    "column_capture_sequence": int(row[3]),
                    "column_event_id": str(row[4]),
                    "payload_capture_sequence": record.get("capture_sequence"),
                    "payload_market_id": record.get("market_id"),
                }
            )

        # The authoritative durable verdict, keyed by the thing decisions point at. A decision
        # carries a *copy* of the verdict it ran under; the RiskRow is the record P9 wrote.
        risk_by_sequence = {}
        for raw_seq, raw_ordinal, raw_payload in connection.execute(
            "SELECT risk_sequence, as_of_ingress_ordinal, payload FROM risk_records"
            " WHERE market_id = ?",
            (found_id,),
        ):
            payload = json.loads(raw_payload)
            risk_by_sequence[int(raw_seq)] = {
                "state": payload.get("state"),
                "allows_place": payload.get("allows_place"),
                "allows_cancel": payload.get("allows_cancel"),
                "as_of_ingress_ordinal": int(raw_ordinal),
            }
        ingress_ordinals = [int(row[3]) for row in decisions]

        manifest = read_manifest(connection, found_id)
    except sqlite3.DatabaseError as error:
        return VerificationResult(
            status=VerificationStatus.CORRUPT,
            market_id=market_id,
            slug=None,
            failures=(f"{type(error).__name__}: {error}",),
        )
    except SchemaVersionError as error:
        return VerificationResult(
            status=VerificationStatus.UNSUPPORTED,
            market_id=market_id,
            slug=None,
            failures=(str(error),),
        )
    finally:
        connection.close()

    checks["market_closed"] = closed
    if not closed:
        failures.append("market was never closed; it may be a crash or a run still in progress")

    checks["one_market_identity"] = all(str(row[2]) == found_id for row in decisions)
    if not checks["one_market_identity"]:
        failures.append("decision rows carry a different market id than the market row")

    checks["global_persistence_order_exact"] = _exact_from(log_sequences, 1)
    if not checks["global_persistence_order_exact"]:
        failures.append(
            "the combined storage order is not 1..N; a record was lost, duplicated across "
            "tables, or written out of order"
        )
    checks["persistence_log_covers_every_row"] = log_total == (
        len(decisions) + int(fills) + len(risks) + int(settlements)
    )
    if not checks["persistence_log_covers_every_row"]:
        failures.append(
            f"{log_total} storage-order entries for "
            f"{len(decisions) + int(fills) + len(risks) + int(settlements)} stored records"
        )

    capture = [int(row[1]) for row in decisions]
    checks["capture_sequence_ordered"] = capture == sorted(capture)
    if not checks["capture_sequence_ordered"]:
        failures.append("capture order is not monotonic; the stream was reordered")

    risk_sequences = [int(row[0]) for row in risks]
    checks["risk_sequence_exact_from_zero"] = _exact_from(risk_sequences, 0)
    if not checks["risk_sequence_exact_from_zero"]:
        first = risk_sequences[0] if risk_sequences else None
        last = risk_sequences[-1] if risk_sequences else None
        expected = (last - first + 1) if first is not None and last is not None else 0
        fault = (
            f"begins at {first} rather than 0"
            if first not in (0, None)
            else f"spans 0..{last} but holds only {len(risk_sequences)} of {expected}"
        )
        failures.append(
            f"risk sequence {fault}; the P9 audit trail is not verifiable from this file"
        )

    # Every decision names the verdict that governed it. A PLACE is the one action that creates
    # new risk, so the verdict it points at has to have permitted it — and has to exist.
    # Join every decision to the risk record it names, and check the copy against it. Trusting
    # the copy would make the invariant circular: a record that misrepresented the verdict it ran
    # under would be checked against its own misrepresentation and pass.
    #
    # A *missing* reference is not a decision that needs no checking — it is a decision whose
    # governing verdict cannot be identified at all, which is worse than one whose copy is wrong.
    # P9 governs every P11 cycle, so a V2 record without its reference is incomplete telemetry,
    # and skipping it (as this once did) let such a record evade every check below including the
    # PLACE contract.
    dangling = 0
    missing_reference = 0
    missing_copy: list[str] = []
    mismatches: list[str] = []
    unpermitted: list[str] = []
    out_of_order = 0
    for row in decision_rows:
        effective = row["effective_version"]
        if effective == 1:
            # V1 means what it meant when it was written and predates these fields. Matched
            # exactly, not by `effective < DECISION_SCHEMA_VERSION`: "older than current" is not
            # a definition of a contract, and it would hand the V1 exemption to a record stamped
            # 0 or -1 that names no schema at all.
            continue

        referenced = row["risk_sequence"]
        absent = [
            name
            for name, value in (
                ("risk_sequence", referenced),
                ("risk_state", row["risk_state"]),
                ("risk_allows_place", row["allows_place"]),
                ("risk_allows_cancel", row["allows_cancel"]),
            )
            if value is None
        ]
        if absent:
            if referenced is None:
                missing_reference += 1
            missing_copy.append(f"ingress {row['ingress_ordinal']}: missing {', '.join(absent)}")
            if row["place"]:
                unpermitted.append(
                    f"ingress {row['ingress_ordinal']} placed with no identifiable verdict"
                )
            continue

        verdict = risk_by_sequence.get(int(referenced))
        if verdict is None:
            dangling += 1
            if row["place"]:
                unpermitted.append(
                    f"ingress {row['ingress_ordinal']} placed under risk_sequence {referenced}, "
                    "which is not stored"
                )
            continue

        for field_name, copied, authoritative in (
            ("risk_state", row["risk_state"], verdict["state"]),
            ("allows_place", row["allows_place"], verdict["allows_place"]),
            ("allows_cancel", row["allows_cancel"], verdict["allows_cancel"]),
        ):
            # Compared directly, and without a `copied is not None` guard. An earlier draft used
            # `bool(copied) != bool(...)`, which silently exempted every state mismatch since
            # SAFE and HALTED are both truthy; a `is not None` guard exempted absence entirely.
            # None and False are different audit facts and neither is a pass.
            if copied != authoritative:
                mismatches.append(
                    f"ingress {row['ingress_ordinal']}: decision says {field_name}={copied!r}, "
                    f"risk_sequence {referenced} says {authoritative!r}"
                )

        # A verdict cannot have been taken after the cycle it governed.
        if verdict["as_of_ingress_ordinal"] > int(row["ingress_ordinal"] or 0):
            out_of_order += 1

        # Permission is read from the RiskRow, never from the decision's copy of it.
        if row["place"] and not (verdict["allows_place"] and verdict["state"] == "SAFE"):
            unpermitted.append(
                f"ingress {row['ingress_ordinal']} placed under risk_sequence {referenced} "
                f"(state={verdict['state']}, allows_place={verdict['allows_place']})"
            )

    checks["decision_risk_reference_present"] = missing_reference == 0
    if missing_reference:
        failures.append(
            f"{missing_reference} decision(s) name no governing risk_sequence at all; the "
            "verdict each ran under cannot be identified"
        )

    checks["decision_risk_copy_complete"] = not missing_copy
    if missing_copy:
        failures.append(
            f"{len(missing_copy)} decision(s) are missing their governing verdict: "
            + "; ".join(missing_copy[:5])
        )

    checks["decision_risk_references_resolve"] = dangling == 0
    if dangling:
        failures.append(f"{dangling} decision(s) name a risk_sequence that is not stored")

    checks["decision_risk_copies_agree"] = not mismatches
    if mismatches:
        failures.append(
            f"{len(mismatches)} decision/risk verdict mismatch(es): " + "; ".join(mismatches[:5])
        )

    # Identity. P11 claims to persist P2's real event id, so a blank one is a claim it did not
    # keep — and the indexed columns duplicate the payload, so a disagreement means the record
    # contradicts itself and neither half can be believed over the other.
    blank_event_ids = 0
    inconsistent: list[str] = []
    schema_disagreements: list[str] = []
    unsupported_versions: list[str] = []
    for row in decision_rows:
        where = f"ingress {row['column_ingress_ordinal']}"

        if row["schema_agrees"] and not row["schema_supported"]:
            # Internally consistent about a schema this build has never defined. That is not a
            # damaged record — it is a record we cannot read, which is a different answer.
            unsupported_versions.append(f"{where}: schema_version={row['schema_version']!r}")

        if not row["schema_agrees"]:
            schema_disagreements.append(
                f"{where}: column schema_version={row['schema_version']!r}, "
                f"payload schema_version={row['payload_schema_version']!r}"
            )

        if (
            row["effective_version"] is not None
            and row["effective_version"] >= (DECISION_SCHEMA_VERSION)
            and not row["event_id"]
        ):
            blank_event_ids += 1

        for name, column, payload, wanted in (
            ("schema_version", row["schema_version"], row["payload_schema_version"], int),
            ("event_id", row["column_event_id"], row["event_id"], str),
            ("market_id", row["column_market_id"], row["payload_market_id"], str),
            ("ingress_ordinal", row["column_ingress_ordinal"], row["ingress_ordinal"], int),
            (
                "capture_sequence",
                row["column_capture_sequence"],
                row["payload_capture_sequence"],
                int,
            ),
        ):
            # Absence is not agreement. An indexed column exists for every one of these, so a
            # payload that has lost its copy is a damaged record, not a nullable one — and an
            # earlier `payload is not None` guard exempted exactly that.
            if payload is None:
                inconsistent.append(f"{where}: payload has no {name}, column has {column!r}")
                continue
            # Nor is the *type* incidental. `1 == True` and `1 == 1.0` are both true in Python,
            # so a payload storing a bool or a float where an integer belongs would agree with
            # its column while saying something else. What the record says includes how it says
            # it.
            if (wanted is int and not _exact_int(payload)) or (
                wanted is str and type(payload) is not str
            ):
                inconsistent.append(
                    f"{where}: payload {name} is {type(payload).__name__} "
                    f"({payload!r}), not {wanted.__name__}"
                )
            elif column != payload:
                inconsistent.append(
                    f"{where}: column {name}={column!r}, payload {name}={payload!r}"
                )

    checks["decisions_carry_a_real_event_id"] = blank_event_ids == 0
    if blank_event_ids:
        failures.append(
            f"{blank_event_ids} decision(s) carry no event id; P2 assigns one to every event, "
            "so a blank one is an identity that was lost rather than one that never existed"
        )

    # Operator control: every accepted command must name a risk row that exists and agrees with
    # it, and every OPERATOR_CONTROL risk row must have a command that claims it. An orphan in
    # either direction means the durable record cannot say who changed the bot's permissions.
    control_problems: list[str] = []
    claimed: set[int] = set()
    seen_ids: set[str] = set()
    for row in control_rows:
        command_id = str(row.get("command_id"))
        if command_id in seen_ids:
            control_problems.append(f"command {command_id} audited more than once")
        seen_ids.add(command_id)
        if not row.get("accepted"):
            continue
        sequence = row.get("risk_sequence")
        verdict = control_risk_rows.get(int(sequence)) if sequence is not None else None
        if verdict is None:
            control_problems.append(
                f"command {command_id} names risk_sequence {sequence}, which is not a stored "
                "OPERATOR_CONTROL record"
            )
            continue
        claimed.add(int(sequence))
        if verdict.get("as_of_ingress_ordinal") != row.get("ingress_ordinal"):
            control_problems.append(
                f"command {command_id}: audit says ingress {row.get('ingress_ordinal')}, risk "
                f"row says {verdict.get('as_of_ingress_ordinal')}"
            )
        if verdict.get("signal_reason") != "OPERATOR_HALT":
            control_problems.append(
                f"command {command_id}: risk row reason is {verdict.get('signal_reason')!r}"
            )
        if bool(verdict.get("signal_flag")) != bool(row.get("signal_flag")):
            control_problems.append(
                f"command {command_id}: audit flag {row.get('signal_flag')!r} against risk row "
                f"flag {verdict.get('signal_flag')!r}"
            )
        if verdict.get("state") != row.get("risk_state"):
            control_problems.append(
                f"command {command_id}: audit state {row.get('risk_state')!r}, risk row "
                f"{verdict.get('state')!r}"
            )
        if bool(verdict.get("allows_place")) != bool(row.get("allows_place")):
            control_problems.append(f"command {command_id}: allows_place disagrees")

    for sequence in sorted(set(control_risk_rows) - claimed):
        control_problems.append(
            f"OPERATOR_CONTROL risk_sequence {sequence} has no operator command claiming it"
        )

    checks["control_audit_cross_links"] = not control_problems
    if control_problems:
        failures.append(
            f"{len(control_problems)} operator-control audit problem(s): "
            + "; ".join(control_problems[:5])
        )

    checks["decision_schema_version_supported"] = not unsupported_versions
    if unsupported_versions:
        failures.append(
            f"{len(unsupported_versions)} decision(s) declare a schema this build does not "
            f"define (known: {sorted(SUPPORTED_DECISION_SCHEMA_VERSIONS)}): "
            + "; ".join(unsupported_versions[:5])
        )

    checks["decision_schema_version_self_consistent"] = not schema_disagreements
    if schema_disagreements:
        failures.append(
            f"{len(schema_disagreements)} decision(s) whose two schema versions disagree, so "
            "which contract applies to them cannot be established: "
            + "; ".join(schema_disagreements[:5])
        )

    checks["decision_columns_match_payload"] = not inconsistent
    if inconsistent:
        failures.append(
            f"{len(inconsistent)} decision(s) whose indexed columns contradict their payload: "
            + "; ".join(inconsistent[:5])
        )

    checks["risk_verdict_not_from_the_future"] = out_of_order == 0
    if out_of_order:
        failures.append(
            f"{out_of_order} decision(s) reference a verdict taken at a later ingress ordinal"
        )

    checks["no_place_without_permission"] = not unpermitted
    if unpermitted:
        failures.append(
            "PLACE recorded against a persisted verdict that forbade it: "
            + "; ".join(unpermitted[:5])
        )

    checks["manifest_present"] = manifest is not None
    if manifest is None:
        failures.append("no manifest; nothing states what this market should contain")
        return VerificationResult(
            status=VerificationStatus.INCOMPLETE,
            market_id=found_id,
            slug=slug,
            checks=checks,
            failures=tuple(failures),
            decision_rows=len(decisions),
            fill_rows=int(fills),
            risk_rows=len(risks),
            settlement_rows=int(settlements),
        )

    checks["decision_count_matches_manifest"] = manifest.decision_count == len(decisions)
    if not checks["decision_count_matches_manifest"]:
        failures.append(
            f"manifest claims {manifest.decision_count} decisions, store holds {len(decisions)}"
        )
    checks["risk_count_matches_manifest"] = manifest.risk_count == len(risks)
    if not checks["risk_count_matches_manifest"]:
        failures.append(
            f"manifest claims {manifest.risk_count} risk records, store holds {len(risks)}"
        )
    checks["fill_count_matches_manifest"] = manifest.fill_count == int(fills)
    if not checks["fill_count_matches_manifest"]:
        failures.append(f"manifest claims {manifest.fill_count} fills, store holds {fills}")
    checks["settlement_count_matches_manifest"] = manifest.settlement_count == int(settlements)
    if not checks["settlement_count_matches_manifest"]:
        failures.append(
            f"manifest claims {manifest.settlement_count} settlements, store holds {settlements}"
        )

    stored_sequences = log_sequences or [int(row[0]) for row in decisions]
    checks["manifest_first_sequence_matches"] = manifest.first_persistence_sequence == (
        stored_sequences[0] if stored_sequences else None
    )
    if not checks["manifest_first_sequence_matches"]:
        failures.append(
            f"manifest names first storage sequence {manifest.first_persistence_sequence}, "
            f"store begins at {stored_sequences[0] if stored_sequences else None}"
        )
    checks["manifest_last_sequence_matches"] = manifest.last_persistence_sequence == (
        stored_sequences[-1] if stored_sequences else None
    )
    if not checks["manifest_last_sequence_matches"]:
        failures.append(
            f"manifest names last storage sequence {manifest.last_persistence_sequence}, "
            f"store ends at {stored_sequences[-1] if stored_sequences else None}"
        )
    checks["manifest_first_ingress_matches"] = manifest.first_ingress_ordinal == (
        min(ingress_ordinals) if ingress_ordinals else None
    )
    checks["manifest_last_ingress_matches"] = manifest.last_ingress_ordinal == (
        max(ingress_ordinals) if ingress_ordinals else None
    )
    for name in ("manifest_first_ingress_matches", "manifest_last_ingress_matches"):
        if not checks[name]:
            failures.append(f"{name.replace('_', ' ')}: the manifest bound is not what is stored")

    checks["risk_drop_accounting_clean"] = manifest.risk_records_dropped == 0
    if not checks["risk_drop_accounting_clean"]:
        failures.append(f"{manifest.risk_records_dropped} risk record(s) were dropped")
    checks["risk_accepted_equals_persisted"] = (
        manifest.risk_records_accepted == manifest.risk_records_persisted
    )
    if not checks["risk_accepted_equals_persisted"]:
        failures.append(
            f"{manifest.risk_records_accepted} risk records accepted, "
            f"{manifest.risk_records_persisted} persisted"
        )
    checks["fill_drop_accounting_clean"] = manifest.fill_captures_dropped == 0
    if not checks["fill_drop_accounting_clean"]:
        failures.append(f"{manifest.fill_captures_dropped} fill capture(s) were dropped")

    checks["no_drops"] = manifest.dropped_records == 0
    if not checks["no_drops"]:
        failures.append(f"{manifest.dropped_records} records were dropped by the bounded buffer")
    checks["no_sequence_gaps"] = manifest.sequence_gaps == 0
    if not checks["no_sequence_gaps"]:
        failures.append(
            f"{manifest.sequence_gaps} gap(s), {manifest.lost_observations} observations lost"
        )
    checks["no_sink_errors"] = manifest.sink_errors == 0
    if not checks["no_sink_errors"]:
        failures.append(f"{manifest.sink_errors} sink error(s) while writing")

    if expected_sha256 is not None:
        _, digest = database_digest(path)
        checks["database_hash_matches"] = digest == expected_sha256
        if not checks["database_hash_matches"]:
            failures.append("database hash does not match the sidecar; the file has changed")
    else:
        checks["database_hash_checked"] = False

    checks["manifest_reports_complete"] = manifest.telemetry_complete

    # An unsupported schema outranks incompleteness: "this build cannot read these records" is a
    # more fundamental answer than "some records are missing", and reporting the second would
    # imply the first had been judged.
    if unsupported_versions:
        status = VerificationStatus.UNSUPPORTED
    elif not failures and manifest.telemetry_complete:
        status = VerificationStatus.COMPLETE
    else:
        status = VerificationStatus.INCOMPLETE
    return VerificationResult(
        status=status,
        market_id=found_id,
        slug=slug,
        checks=checks,
        failures=tuple(failures),
        decision_rows=len(decisions),
        fill_rows=int(fills),
        risk_rows=len(risks),
        settlement_rows=int(settlements),
        manifest=manifest,
    )
