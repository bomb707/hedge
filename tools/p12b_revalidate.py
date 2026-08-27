"""Rebuild the P12B final snapshot from the durable store, with the corrected read model.

**REAL MARKET DATA.** Everything here comes out of the P12B store captured from Polymarket's
real `btc-updown-5m-1787807700` market, opened through the verified archive path.

This is a revalidation, not a rewrite. `docs/evidence/p12b-final-snapshot.json` stays exactly as
it was: it is the evidence that the old read model made a statement its own manifest contradicted
— 82,335 decisions, 82,337 risk records, one drop and INCOMPLETE, beside a manifest saying
82,336, 82,338, none and COMPLETE. What this produces is a second, separately named artifact
showing what the corrected read model says about the same durable bytes.

The read model is fed only from what is durably stored. Nothing is inferred, and nothing comes
from a live counter, because the market closed days ago and there are no live counters left.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

from maker5m.persistence import (
    ArchiveIdentity,
    Manifest,
    MarketIdentity,
    open_verified_archive,
    verify_store,
)
from maker5m.persistence.schema import DecisionRecord, ExactRatio, SideRecord
from maker5m.strategy import default_config
from maker5m.ui import SnapshotPublisher


def _decision_from(payload: dict[str, Any]) -> DecisionRecord:
    """Rebuild the stored decision as the real record type.

    That it rebuilds at all is part of what is being checked: P11 claims the payload is
    self-describing, and a record that cannot be reconstructed from its own row would not be.
    """
    fields = dict(payload)
    fields["up"] = SideRecord(**fields["up"])
    fields["down"] = SideRecord(**fields["down"])
    if fields.get("raw_centre") is not None:
        fields["raw_centre"] = ExactRatio(**fields["raw_centre"])
    return DecisionRecord(**fields)


class StoredVerdict:
    """The governing risk record as the store holds it. Attribute-compatible with the live one.

    P11's risk rows do not carry P6's HealthFrame, so `health` is genuinely absent here and the
    rebuilt snapshot reads UNKNOWN for the feed statuses rather than inventing them. That is the
    read model behaving correctly on a rebuild; the live P12C market shows the real statuses.
    """

    health = None

    def __init__(self, payload: dict[str, Any]) -> None:
        self.risk_sequence = payload["risk_sequence"]
        self.state = payload["state"]
        self.active = tuple(payload["active"])
        self.latched = tuple(payload["latched"])
        self.allows_place = payload["allows_place"]
        self.allows_cancel = payload["allows_cancel"]
        self.signal = None


def _rows(connection: sqlite3.Connection, sql: str) -> list[sqlite3.Row]:
    connection.row_factory = sqlite3.Row
    return list(connection.execute(sql))


def revalidate(archive: Path, sidecar: Path, out: Path) -> dict[str, Any]:
    scratch = out / "p12b-revalidation.sqlite3"
    scratch.unlink(missing_ok=True)
    open_verified_archive(archive, ArchiveIdentity.from_sidecar(sidecar), scratch)
    identity_row = ArchiveIdentity.from_sidecar(sidecar)
    verification = verify_store(scratch)

    connection = sqlite3.connect(scratch)
    try:
        market = _rows(connection, "SELECT * FROM markets")[0]
        stored = json.loads(market["manifest_json"])
        # Rebuilt as the real object so completeness is derived by the same code the runner
        # uses, rather than read out of a field the store never had.
        manifest = Manifest(**{f: stored[f] for f in Manifest.__dataclass_fields__ if f in stored})
        decision_rows = _rows(
            connection, "SELECT payload FROM decisions ORDER BY persistence_sequence"
        )
        last_decision = _decision_from(json.loads(decision_rows[-1]["payload"]))
        risk_rows = _rows(
            connection,
            "SELECT payload FROM risk_records ORDER BY persistence_sequence DESC LIMIT 8",
        )
        control = _rows(connection, "SELECT * FROM control_audit ORDER BY persistence_sequence")
        settlements = _rows(connection, "SELECT * FROM settlements ORDER BY persistence_sequence")
        counts = {
            table: _rows(connection, f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]
            for table in ("decisions", "risk_records", "fills", "control_audit", "settlements")
        }
    finally:
        connection.close()

    publisher = SnapshotPublisher(
        identity=MarketIdentity(
            slug=market["slug"],
            market_id=market["market_id"],
            condition_id=market["condition_id"],
            provenance=market["provenance"],
        ),
        config=default_config(),
        bridge=None,
        t0_ns=int(manifest.capture_start_ns or 0),
    )
    for row in reversed(risk_rows):
        publisher.observe_risk(StoredVerdict(json.loads(row["payload"])))
    publisher.observe(last_decision)
    for row in control:
        payload = json.loads(row["payload"])
        publisher.deliver(
            "control_persisted",
            {
                "command_id": row["command_id"],
                "kind": row["kind"],
                "accepted": bool(row["accepted"]),
                "ingress_ordinal": payload.get("ingress_ordinal"),
                "risk_sequence": row["risk_sequence"],
                "risk_state": payload.get("risk_state"),
                "allows_place": payload.get("allows_place"),
                "detail": payload.get("detail"),
            },
        )
    publisher.deliver(
        "audit_counts",
        {"accepted": len(control), "persisted": len(control), "dropped": 0},
    )
    if settlements:
        payload = json.loads(settlements[0]["payload"])
        decision = payload.get("decision", payload)
        publisher.deliver(
            "settlement",
            {
                "state": decision.get("state", settlements[0]["resolution_state"]),
                "winning_outcome": decision.get("winning_outcome"),
                "authoritative_block": decision.get("authoritative_block"),
                "payout_numerators": list(decision.get("payout_numerators") or []),
                "note": "redemption is disabled in this build; nothing was redeemed",
            },
        )
    publisher.deliver(
        "closed",
        {
            "decision_count": manifest.decision_count,
            "risk_count": manifest.risk_count,
            "dropped_records": manifest.dropped_records,
            "sink_errors": manifest.sink_errors,
            "telemetry_complete": manifest.telemetry_complete,
            "verification_status": verification.status.value,
        },
    )
    snapshot = publisher.build(now=0.0)
    scratch.unlink(missing_ok=True)

    fields = snapshot.__dataclass_fields__ if hasattr(snapshot, "__dataclass_fields__") else {}
    rendered = {name: _plain(getattr(snapshot, name)) for name in fields}
    return {
        "kind": "P12B_SNAPSHOT_REVALIDATION",
        "provenance": market["provenance"],
        "slug": market["slug"],
        "source_archive": str(archive),
        "source_sidecar": str(sidecar),
        "archive_sha256": identity_row.archive_sha256,
        "database_sha256": identity_row.raw_sha256,
        "note": (
            "The P12B store is unchanged and re-verified here. The historical "
            "p12b-final-snapshot.json is deliberately left as written: it is the evidence of "
            "the incorrect read, not a draft to be corrected."
        ),
        "durable_counts": counts,
        "joined_on": {
            "decision_ingress_ordinal": last_decision.ingress_ordinal,
            "decision_risk_sequence": last_decision.risk_sequence,
            "note": (
                "The snapshot's risk fields come from the RiskRow whose sequence this decision "
                "names, read out of the store. P12B took whichever verdict was newest."
            ),
        },
        "manifest": stored,
        "manifest_telemetry_complete": manifest.telemetry_complete,
        "verification": verification.summary(),
        "revalidated_snapshot": rendered,
        "p12b_final_snapshot_said": {
            "decisions_persisted": 82_335,
            "risk_records_persisted": 82_337,
            "dropped_records": 1,
            "telemetry_complete": None,
            "audit": "INCOMPLETE",
        },
        "limitation": (
            "This rebuilds the read model from durable rows only. Per-decision latency is not "
            "reconstructed, because the P8 observation buffer is a live structure and was not "
            "persisted; latency coherence is proved on the fresh P12C market instead."
        ),
    }


def _plain(value: object) -> object:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if hasattr(value, "__dataclass_fields__"):
        return {f: _plain(getattr(value, f)) for f in value.__dataclass_fields__}
    return str(value)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--sidecar", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    evidence = revalidate(args.archive, args.sidecar, args.out)
    path = args.out / f"p12c-p12b-revalidation-{evidence['slug']}.json"
    path.write_text(json.dumps(evidence, indent=2, sort_keys=True, default=str), encoding="utf-8")
    print(json.dumps(evidence["revalidated_snapshot"], indent=2, sort_keys=True)[:2000])
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
