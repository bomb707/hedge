"""Durable-audit integrity: what a persisted market must be unable to claim.

**SUPPORTING UNIT TEST ONLY.** Every store here is constructed. These prove refusal paths — a
lost risk prefix, a duplicated storage sequence, a tampered manifest, a PLACE under a verdict
that forbade it — which a real market cannot be asked to produce on demand.

Each of them describes a way a market could look complete while missing something, which is the
only failure mode that actually matters for a durability phase: a market that obviously broke
gets noticed anyway.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from maker5m.market.timebase import TimestampNs
from maker5m.persistence import (
    MANIFEST_SCHEMA_VERSION,
    STORE_SCHEMA_VERSION,
    Manifest,
    SchemaVersionError,
    TelemetryStore,
    VerificationStatus,
    build_decision_record,
    build_fill_record,
    verify_store,
)
from maker5m.persistence.store import _payload
from tests.persistence.builders import fill_capture, identity, observation

DECISIONS = 6
RISKS = 4


def build_market(
    tmp_path: Path,
    *,
    risk_first: int = 0,
    risk_count: int = RISKS,
    fills: int = 0,
    place_at: int | None = None,
    place_allowed: bool = True,
    manifest_overrides: dict[str, Any] | None = None,
) -> Path:
    """A small, complete, self-consistent market — then broken one way by the caller."""
    from maker5m.execution.reconciler import ReconcileAction

    path = tmp_path / "market.sqlite3"
    ident = identity()
    store = TelemetryStore(path=path, batch_size=4)
    store.open()
    store.register_market(
        market_id=ident.market_id,
        slug=ident.slug,
        condition_id=ident.condition_id,
        provenance=ident.provenance,
    )

    sequence = 0
    for index in range(risk_count):
        sequence += 1
        store.write_risk(_risk_row(ident.market_id, risk_first + index, sequence))

    for index in range(DECISIONS):
        sequence += 1
        placing = place_at is not None and index == place_at
        record = build_decision_record(
            observation(
                index,
                ordinal=index,
                risk=(risk_first, "SAFE", place_allowed, True),
                action=ReconcileAction.PLACE if placing else ReconcileAction.KEEP,
            ),
            ident,
            persistence_sequence=sequence,
        )
        store.write_decision(record)

    for _ in range(fills):
        sequence += 1
        store.write_fill(build_fill_record(fill_capture(), ident, persistence_sequence=sequence))

    values: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "slug": ident.slug,
        "market_id": ident.market_id,
        "condition_id": ident.condition_id,
        "capture_start_ns": 0,
        "capture_end_ns": 1,
        "source_revision": "test",
        "decision_count": DECISIONS,
        "fill_count": fills,
        "risk_count": risk_count,
        "settlement_count": 0,
        "first_ingress_ordinal": 0,
        "last_ingress_ordinal": DECISIONS - 1,
        "first_persistence_sequence": 1,
        "last_persistence_sequence": sequence,
        "accepted_records": DECISIONS,
        "persisted_records": DECISIONS,
        "dropped_records": 0,
        "sequence_gaps": 0,
        "lost_observations": 0,
        "sink_errors": 0,
        "first_gap_at": None,
        "last_gap_at": None,
        "buffer_capacity": 100,
        "buffer_high_water": 3,
        "database_bytes": None,
        "database_sha256": None,
        "provenance": ident.provenance,
        "live_trading_enabled": False,
        "redemption_enabled": False,
        "closed": True,
        "risk_records_accepted": risk_count,
        "risk_records_persisted": risk_count,
        "risk_records_dropped": 0,
        "fill_captures_accepted": fills,
        "fill_captures_persisted": fills,
        "fill_captures_dropped": 0,
    }
    values.update(manifest_overrides or {})
    store.write_manifest(Manifest(**values))
    store.close()
    return path


def _risk_row(market_id: str, risk_sequence: int, persistence_sequence: int) -> Any:
    from maker5m.persistence import RiskRow

    return RiskRow(
        schema_version=1,
        persistence_sequence=persistence_sequence,
        market_id=market_id,
        risk_sequence=risk_sequence,
        as_of_ingress_ordinal=risk_sequence,
        signal_kind="RISK_EVALUATION",
        signal_reason=None,
        signal_flag=False,
        signal_timestamp_ns=TimestampNs(risk_sequence),
        signal_value_ns=None,
        state="SAFE",
        active=(),
        latched=(),
        allows_place=True,
        allows_cancel=True,
        provenance="SYNTHETIC_SUPPORTING_TEST_ONLY",
        risk_schema_version=1,
    )


# -- the control ------------------------------------------------------------------------------


def test_a_well_formed_market_verifies_complete(tmp_path: Path) -> None:
    result = verify_store(build_market(tmp_path))
    assert result.status is VerificationStatus.COMPLETE, result.failures
    assert result.complete


# -- risk sequence: the P9C contract, not a weaker P11 reading --------------------------------


def test_a_lost_risk_prefix_is_refused(tmp_path: Path) -> None:
    """The exact defect: a bounded channel drops its oldest, and 5,6,7 looks contiguous."""
    result = verify_store(build_market(tmp_path, risk_first=5))
    assert result.status is VerificationStatus.INCOMPLETE
    assert not result.checks["risk_sequence_exact_from_zero"]
    assert any("begins at 5 rather than 0" in failure for failure in result.failures)


def test_a_risk_middle_gap_is_refused(tmp_path: Path) -> None:
    path = build_market(tmp_path)
    _mutate(path, "UPDATE risk_records SET risk_sequence = 9 WHERE risk_sequence = 2")
    result = verify_store(path)
    assert not result.checks["risk_sequence_exact_from_zero"]
    assert result.status is VerificationStatus.INCOMPLETE


def test_a_duplicate_risk_sequence_cannot_be_stored_at_all(tmp_path: Path) -> None:
    """Refused by the schema, not merely by the verifier.

    `risk_records` carries a UNIQUE index on (market_id, risk_sequence), so a duplicate cannot
    reach the file to be caught later. Asserted here because it is a stronger guarantee than
    detection and would be easy to lose in a future migration without noticing.
    """
    path = build_market(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        _mutate(path, "UPDATE risk_records SET risk_sequence = 1 WHERE risk_sequence = 2")


def test_a_backwards_risk_sequence_is_refused(tmp_path: Path) -> None:
    path = build_market(tmp_path)
    _mutate(path, "UPDATE risk_records SET risk_sequence = -1 WHERE risk_sequence = 0")
    assert not verify_store(path).checks["risk_sequence_exact_from_zero"]


def test_dropped_risk_records_make_the_market_incomplete(tmp_path: Path) -> None:
    """Even when every retained record is perfectly contiguous."""
    path = build_market(tmp_path, manifest_overrides={"risk_records_dropped": 3})
    result = verify_store(path)
    assert result.status is VerificationStatus.INCOMPLETE
    assert not result.checks["risk_drop_accounting_clean"]


# -- global storage order ---------------------------------------------------------------------


def test_a_lost_global_prefix_is_refused(tmp_path: Path) -> None:
    path = build_market(tmp_path)
    _mutate(path, "DELETE FROM persistence_log WHERE persistence_sequence = 1")
    result = verify_store(path)
    assert result.status is VerificationStatus.INCOMPLETE
    assert not result.checks["global_persistence_order_exact"]


def test_a_duplicate_across_two_record_types_is_refused(tmp_path: Path) -> None:
    """A decision and a risk row cannot share a storage sequence."""
    path = build_market(tmp_path)
    _mutate(path, "DELETE FROM persistence_log WHERE persistence_sequence = 6")
    result = verify_store(path)
    assert not result.checks["global_persistence_order_exact"]
    assert not result.checks["persistence_log_covers_every_row"]


def test_a_middle_hole_in_storage_order_is_refused(tmp_path: Path) -> None:
    path = build_market(tmp_path)
    _mutate(path, "DELETE FROM persistence_log WHERE persistence_sequence = 4")
    assert not verify_store(path).checks["global_persistence_order_exact"]


# -- manifest bounds are checked, not merely stored --------------------------------------------


def test_a_tampered_first_sequence_bound_is_caught(tmp_path: Path) -> None:
    path = build_market(tmp_path, manifest_overrides={"first_persistence_sequence": 2})
    result = verify_store(path)
    assert result.status is VerificationStatus.INCOMPLETE
    assert not result.checks["manifest_first_sequence_matches"]


def test_a_tampered_last_sequence_bound_is_caught(tmp_path: Path) -> None:
    path = build_market(tmp_path, manifest_overrides={"last_persistence_sequence": 99})
    assert not verify_store(path).checks["manifest_last_sequence_matches"]


def test_a_tampered_ingress_bound_is_caught(tmp_path: Path) -> None:
    path = build_market(tmp_path, manifest_overrides={"last_ingress_ordinal": 999})
    assert not verify_store(path).checks["manifest_last_ingress_matches"]


# -- decision <-> risk cross references --------------------------------------------------------


def test_a_decision_naming_an_absent_risk_sequence_is_refused(tmp_path: Path) -> None:
    path = build_market(tmp_path)
    _mutate(
        path,
        "UPDATE decisions SET payload = replace(payload,"
        " '\"risk_sequence\":0', '\"risk_sequence\":404')",
    )
    result = verify_store(path)
    assert result.status is VerificationStatus.INCOMPLETE
    assert not result.checks["decision_risk_references_resolve"]


def test_a_place_under_a_verdict_that_forbade_it_is_refused(tmp_path: Path) -> None:
    """The load-bearing cross-record invariant: a PLACE creates new risk."""
    path = build_market(tmp_path, place_at=2, place_allowed=False)
    result = verify_store(path)
    assert result.status is VerificationStatus.INCOMPLETE
    assert not result.checks["no_place_without_permission"]
    assert any("PLACE recorded" in failure for failure in result.failures)


def test_a_place_under_a_permitting_verdict_is_fine(tmp_path: Path) -> None:
    result = verify_store(build_market(tmp_path, place_at=2, place_allowed=True))
    assert result.status is VerificationStatus.COMPLETE, result.failures


def test_cancelling_while_halted_remains_allowed(tmp_path: Path) -> None:
    """A halt withdraws quotes; it must not trap us in them."""
    from maker5m.execution.reconciler import ReconcileAction

    record = build_decision_record(
        observation(0, risk=(0, "HALTED", False, True), action=ReconcileAction.CANCEL),
        identity(),
        persistence_sequence=1,
    )
    assert record.up.action == "CANCEL"
    assert record.risk_allows_place is False
    assert record.risk_allows_cancel is True


# -- writer schema fails closed -----------------------------------------------------------------


def test_opening_an_unknown_schema_for_writing_is_refused_without_mutation(
    tmp_path: Path,
) -> None:
    path = build_market(tmp_path)
    before = path.read_bytes()
    _mutate(path, f"PRAGMA user_version={STORE_SCHEMA_VERSION + 7}")
    stamped = path.read_bytes()

    store = TelemetryStore(path=path)
    with pytest.raises(SchemaVersionError, match="refusing to open"):
        store.open()

    assert path.read_bytes() == stamped, "the refused open changed the file"
    assert before != stamped, "the test's own tampering did take effect"


def test_a_fresh_store_stamps_the_current_schema(tmp_path: Path) -> None:
    path = tmp_path / "fresh.sqlite3"
    store = TelemetryStore(path=path)
    store.open()
    store.close()
    connection = sqlite3.connect(str(path))
    assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == STORE_SCHEMA_VERSION
    connection.close()


# -- event identity -------------------------------------------------------------------------------


def test_the_persisted_event_id_is_the_real_one(tmp_path: Path) -> None:
    """§19: not the slug, not the ordinal, not the capture sequence."""
    record = build_decision_record(
        observation(7, event_id="clob-004242"), identity(), persistence_sequence=1
    )
    assert record.event_id == "clob-004242"
    assert identity().slug not in record.event_id


def test_changing_capture_or_storage_sequence_does_not_change_the_event_id() -> None:
    first = build_decision_record(
        observation(1, event_id="spot-000009"), identity(), persistence_sequence=1
    )
    second = build_decision_record(
        observation(99, event_id="spot-000009"), identity(), persistence_sequence=500
    )
    assert first.event_id == second.event_id == "spot-000009"
    assert first.capture_sequence != second.capture_sequence
    assert first.persistence_sequence != second.persistence_sequence


def test_an_event_without_an_id_is_recorded_as_absent_not_invented() -> None:
    record = build_decision_record(observation(0, event_id=""), identity(), persistence_sequence=1)
    assert record.event_id == ""


def test_a_stored_event_id_survives_the_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "ids.sqlite3"
    store = TelemetryStore(path=path, batch_size=1)
    store.open()
    ident = identity()
    store.register_market(
        market_id=ident.market_id,
        slug=ident.slug,
        condition_id=ident.condition_id,
        provenance=ident.provenance,
    )
    store.write_decision(
        build_decision_record(observation(0, event_id="clob-000777"), ident, persistence_sequence=1)
    )
    store.close()

    connection = sqlite3.connect(str(path))
    stored = connection.execute("SELECT event_id, payload FROM decisions").fetchone()
    connection.close()
    assert stored[0] == "clob-000777"
    assert json.loads(stored[1])["event_id"] == "clob-000777"


# -- raw strategy intent survives risk withdrawal ---------------------------------------------


def test_a_record_can_tell_a_refused_quote_from_an_absent_one() -> None:
    """§5: telemetry must answer both 'what did the strategy want' and 'what was allowed'."""
    from maker5m.execution.reconciler import ReconcileAction
    from maker5m.numeric.units import PriceUnits, ShareUnits

    wanted = build_decision_record(
        observation(
            0,
            risk=(3, "HALTED", False, True),
            action=ReconcileAction.CANCEL,
            strategy_intent=(
                PriceUnits(490_000),
                ShareUnits(15_000_000),
                PriceUnits(480_000),
                ShareUnits(15_000_000),
            ),
        ),
        identity(),
        persistence_sequence=1,
    )
    assert wanted.strategy_up_price == 490_000
    assert wanted.strategy_down_price == 480_000
    assert wanted.risk_state == "HALTED"
    assert wanted.risk_allows_place is False

    declined = build_decision_record(
        observation(1, risk=(3, "SAFE", True, True), strategy_intent=(None, None, None, None)),
        identity(),
        persistence_sequence=2,
    )
    assert declined.strategy_up_price is None
    assert declined.risk_allows_place is True
    assert declined.risk_withdrew_intent is False


# -- fills ---------------------------------------------------------------------------------------


def test_one_fill_capture_persists_exactly_one_fill_record(tmp_path: Path) -> None:
    path = build_market(tmp_path, fills=1)
    result = verify_store(path)
    assert result.fill_rows == 1
    assert result.status is VerificationStatus.COMPLETE, result.failures


def test_a_fill_record_never_reapplies_the_fill() -> None:
    from maker5m.accounting.ledger import LedgerState

    capture = fill_capture()
    record = build_fill_record(capture, identity(), persistence_sequence=1)
    assert record.total_cost_before == LedgerState().total_cost
    assert record.total_cost_after == capture.after.total_cost
    assert record.n_up_after == capture.after.n_up
    # The capture's own states are untouched by having been recorded.
    assert capture.before.total_cost == 0
    assert capture.after.total_cost == 4_900_000


def test_dropped_fill_captures_make_the_market_incomplete(tmp_path: Path) -> None:
    path = build_market(tmp_path, manifest_overrides={"fill_captures_dropped": 2})
    result = verify_store(path)
    assert result.status is VerificationStatus.INCOMPLETE
    assert not result.checks["fill_drop_accounting_clean"]


def _mutate(path: Path, statement: str) -> None:
    connection = sqlite3.connect(str(path))
    connection.execute(statement)
    connection.commit()
    connection.close()


def test_payload_encoding_round_trips_every_field() -> None:
    record = build_decision_record(observation(), identity(), persistence_sequence=1)
    decoded = json.loads(_payload(record))
    assert decoded["event_id"] == record.event_id
    assert decoded["raw_centre"] is None or "numerator" in decoded["raw_centre"]
    assert decoded["up"]["action"] == record.up.action
    assert decoded["schema_version"] == record.schema_version
