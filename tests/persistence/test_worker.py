"""The producer/consumer boundary: bounded, non-blocking, and lossy in the open.

**SUPPORTING UNIT TEST ONLY.** These prove refusal and failure paths — a stalled sink, a broken
database, an overflowing buffer — which a real market cannot be asked to produce on demand. The
real-market evidence is a separate gate.

The load tests here are about the *contract*, not about latency. Latency is measured against a
real captured market in `tools/p11_persistence_bench.py`; a threading test on a shared CI-ish
machine could only produce a number nobody should trust.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from maker5m.persistence import (
    STORE_SCHEMA_VERSION,
    MarketIdentity,
    PersistenceWorker,
    TelemetryStore,
    VerificationStatus,
    verify_store,
)
from maker5m.telemetry import ObservationBuffer, TelemetryAnalyzer
from tests.persistence.builders import identity, observation


def worker(tmp_path: Path, *, capacity: int = 1_000, **kwargs: object) -> PersistenceWorker:
    buffer = ObservationBuffer(capacity=capacity)
    store = TelemetryStore(path=tmp_path / "telemetry.sqlite3", batch_size=64)
    return PersistenceWorker(buffer=buffer, store=store, identity=identity(), **kwargs)  # type: ignore[arg-type]


# -- the producer never waits ------------------------------------------------------------------


def test_capture_never_blocks_even_when_the_buffer_is_full() -> None:
    """A full buffer drops the oldest and returns. It does not push back."""
    buffer = ObservationBuffer(capacity=8)
    for seq in range(200):
        buffer.capture(observation(seq))
    assert len(buffer) == 8
    assert buffer.accepted == 200
    assert buffer.dropped == 192


def test_the_producer_holds_no_lock_the_consumer_could_hold() -> None:
    """`deque.append` is the whole hot-side contract; nothing lockable is involved."""
    buffer = ObservationBuffer(capacity=32)
    assert not hasattr(buffer.records, "acquire")
    assert not hasattr(buffer, "lock")
    assert type(buffer.records).__name__ == "deque"


def test_sustained_concurrent_append_and_popleft_loses_nothing(tmp_path: Path) -> None:
    """The CPython deque assumption, exercised rather than asserted.

    `deque.append` and `deque.popleft` are individually atomic on CPython because each finishes
    inside one C call without releasing the GIL. This is documented and relied on by
    `queue.Queue` itself, but it is a CPython property rather than a language guarantee, so it
    is put under sustained two-thread load here instead of being taken on trust.
    """
    unit = worker(tmp_path, capacity=200_000)
    total = 40_000
    stop = threading.Event()

    def consume() -> None:
        # The draining thread opens the store, as the production worker does in `start()`. An
        # earlier version of this test opened it on the main thread and drained here, so every
        # write raised `SQLite objects created in a thread can only be used in that same
        # thread` — and the test still passed, because a refused write was counted as written.
        # 20,480 of the 40,000 "written" decisions were never in the file.
        unit.store.open()
        try:
            while not stop.is_set() or unit.buffer.records:
                if unit.drain_once() == 0:
                    time.sleep(0.001)
        finally:
            unit.store.close()

    thread = threading.Thread(target=consume)
    thread.start()
    try:
        for seq in range(total):
            unit.buffer.capture(observation(seq))
    finally:
        stop.set()
        thread.join(30)

    assert unit.stats.observations_consumed == total
    assert unit.stats.sequence_gaps == 0, "nothing was lost or reordered"
    assert unit.stats.lost_observations == 0
    assert unit.stats.decisions_written == total
    assert unit.stats.write_failures == 0
    assert unit.store.sink_errors == 0, "every one of those rows is actually in the file"


# -- drops are exact and visible ---------------------------------------------------------------


def test_an_overflowing_buffer_produces_an_exact_gap(tmp_path: Path) -> None:
    unit = worker(tmp_path, capacity=16)
    unit.store.open()
    for seq in range(100):
        unit.buffer.capture(observation(seq))
    unit.drain_once()
    unit.store.close()

    assert unit.stats.sequence_gaps == 1
    assert unit.stats.lost_observations == 84
    assert unit.stats.first_gap_at == 0
    assert unit.stats.last_gap_at == 84
    assert unit.stats.decisions_written == 16


def test_a_gap_is_never_bridged_or_interpolated(tmp_path: Path) -> None:
    unit = worker(tmp_path, capacity=1_000)
    unit.store.open()
    unit.buffer.capture(observation(0))
    unit.buffer.capture(observation(5))
    unit.drain_once()
    unit.store.close()
    assert unit.stats.lost_observations == 4
    assert unit.stats.decisions_written == 2, "the missing four are absent, not reconstructed"


# -- sink failure never reaches the producer ---------------------------------------------------


def test_a_database_error_is_counted_and_swallowed(tmp_path: Path) -> None:
    unit = worker(tmp_path)
    unit.store.open()
    assert unit.store._connection is not None
    unit.store._connection.close()  # the disk has gone away mid-market

    for seq in range(10):
        unit.buffer.capture(observation(seq))
    unit.drain_once()  # must not raise

    assert unit.store.sink_errors > 0
    assert unit.buffer.accepted == 10


def test_writing_with_no_connection_at_all_is_an_error_not_a_crash(tmp_path: Path) -> None:
    store = TelemetryStore(path=tmp_path / "never-opened.sqlite3")
    from maker5m.persistence import build_decision_record
    from tests.persistence.builders import identity as ident

    record = build_decision_record(observation(), ident(), persistence_sequence=1)
    store.write_decision(record)
    # Two refusals: the storage-order envelope and the row itself, both with nowhere to go.
    assert store.sink_errors == 2
    assert store.rows_written == 0


def test_a_stalled_consumer_does_not_stop_the_producer(tmp_path: Path) -> None:
    """The §41 claim, in miniature: persistence can fail without stopping trading."""
    stalled = threading.Event()
    stalled.set()
    unit = worker(tmp_path, capacity=64, stall=stalled.is_set)
    unit.start()
    try:
        started = time.perf_counter()
        for seq in range(5_000):
            unit.buffer.capture(observation(seq))
        elapsed = time.perf_counter() - started
        assert elapsed < 5.0, "the producer waited on the consumer"
        assert unit.stats.observations_consumed == 0, "the consumer really was stalled"
        assert unit.buffer.dropped > 0, "the bounded buffer really did overflow"

        stalled.clear()
        deadline = time.perf_counter() + 10
        while unit.stats.observations_consumed == 0 and time.perf_counter() < deadline:
            time.sleep(0.01)
        assert unit.stats.observations_consumed > 0, "the sink did not resume"
    finally:
        unit.stop(timeout=10)


# -- the store itself --------------------------------------------------------------------------


def test_the_store_stamps_and_checks_its_schema_version(tmp_path: Path) -> None:
    path = tmp_path / "telemetry.sqlite3"
    store = TelemetryStore(path=path)
    store.open()
    store.close()

    connection = sqlite3.connect(str(path))
    assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == STORE_SCHEMA_VERSION
    connection.execute(f"PRAGMA user_version={STORE_SCHEMA_VERSION + 1}")
    connection.commit()
    connection.close()

    result = verify_store(path)
    assert result.status is VerificationStatus.UNSUPPORTED
    assert not result.complete


def test_a_truncated_database_fails_the_verifier_closed(tmp_path: Path) -> None:
    path = tmp_path / "telemetry.sqlite3"
    store = TelemetryStore(path=path)
    store.open()
    store.close()
    path.write_bytes(path.read_bytes()[:200])

    result = verify_store(path)
    assert result.status in {VerificationStatus.CORRUPT, VerificationStatus.UNSUPPORTED}
    assert not result.complete


def test_a_missing_database_is_corrupt_not_empty(tmp_path: Path) -> None:
    result = verify_store(tmp_path / "absent.sqlite3")
    assert result.status is VerificationStatus.CORRUPT


def test_markets_are_keyed_separately_so_two_can_be_open_at_once(tmp_path: Path) -> None:
    """A settling market and a trading market must not share state."""
    store = TelemetryStore(path=tmp_path / "two.sqlite3", batch_size=4)
    store.open()
    for slug in ("btc-updown-5m-1787733300", "btc-updown-5m-1787733600"):
        store.register_market(
            market_id=f"0x{slug[-4:]}", slug=slug, condition_id=None, provenance="TEST"
        )
    store.flush()
    assert store._connection is not None
    rows = store._connection.execute("SELECT COUNT(*) FROM markets").fetchone()[0]
    store.close()
    assert rows == 2


@pytest.mark.parametrize("capacity", [1, 7, 64])
def test_any_capacity_keeps_the_drop_arithmetic_exact(capacity: int) -> None:
    buffer = ObservationBuffer(capacity=capacity)
    for seq in range(500):
        buffer.capture(observation(seq))
    assert buffer.accepted == 500
    assert buffer.dropped == 500 - capacity
    assert buffer.accepted - buffer.dropped - buffer.drained == len(buffer)


def test_the_analyzer_sees_exactly_the_same_stream(tmp_path: Path) -> None:
    """One capture, fanned out — P8's measurement and P11's records cannot disagree."""
    analyzer = TelemetryAnalyzer()
    unit = worker(tmp_path, capacity=1_000, analyzer=analyzer)
    unit.store.open()
    for seq in range(50):
        unit.buffer.capture(observation(seq))
    unit.drain_once()
    unit.store.close()
    assert analyzer.processed == 50
    assert analyzer.gaps == 0
    assert unit.stats.decisions_written == 50


def test_an_identity_is_required_before_records_can_be_attributed() -> None:
    ident = MarketIdentity(
        market_id="0xabc", slug="btc-updown-5m-1", condition_id=None, provenance="TEST"
    )
    assert ident.market_id and ident.slug
    assert ident.condition_id is None


def test_a_second_concurrent_drainer_is_turned_away_not_interleaved(tmp_path: Path) -> None:
    """Found by accident, kept on purpose.

    Two threads popping the same deque interleave, so each reads the other's observations as
    forward jumps and reports gaps that never happened. A load test with a stray second drainer
    reported 12 gaps and 23 lost observations while losing nothing at all. Phantom gaps would
    mark a whole market's telemetry incomplete for nothing and hide a real gap in the noise, so
    the second caller is refused rather than allowed to corrupt the accounting.

    Asserted by holding the drain lock directly rather than by racing threads: the invariant is
    "a second caller consumes nothing", and a race would test the scheduler instead.
    """
    unit = worker(tmp_path, capacity=1_000)
    unit.store.open()
    for seq in range(50):
        unit.buffer.capture(observation(seq))

    assert unit._draining.acquire(blocking=False)
    try:
        assert unit.drain_once() == 0, "a second drainer must take nothing"
        assert unit.stats.observations_consumed == 0
        assert len(unit.buffer) == 50, "and must leave the observations for the first"
    finally:
        unit._draining.release()

    assert unit.drain_once() == 50
    unit.store.close()
    assert unit.stats.sequence_gaps == 0
    assert unit.stats.lost_observations == 0


# -- "written" means the row is in the file ----------------------------------------------------


def _operator_command(command_id: str) -> Any:
    from maker5m.ui import COMMAND_SCHEMA_VERSION, OperatorCommand

    return OperatorCommand(
        schema_version=COMMAND_SCHEMA_VERSION,
        command_id=command_id,
        kind="OPERATOR_HALT",
        issued_at_ns=1,
    )


def _outcome(command_id: str) -> Any:
    from maker5m.ui import CommandOutcome

    return (
        CommandOutcome(
            command_id=command_id,
            kind="OPERATOR_HALT",
            accepted=True,
            ingress_ordinal=10,
            risk_sequence=0,
            risk_state="HALTED",
            allows_place=False,
            detail="operator halt raised",
        ),
        True,
    )


def test_a_refused_control_row_is_not_counted_or_published(tmp_path: Path) -> None:
    """§9. `write_control_audit` absorbs the IntegrityError; that is not the same as storing it.

    The second row carries a command id the unique index already holds. The write path counts a
    sink error and returns without the row. Before this contract existed, the worker incremented
    `control_records_written` and called `on_control_record` anyway, so the manifest claimed a
    row the file did not have and the dashboard listed it in the operator's history.
    """
    from maker5m.persistence import BoundedChannel

    channel = BoundedChannel(capacity=8)
    unit = worker(tmp_path, control_audit=channel)
    published: list[Any] = []
    unit.on_control_record = published.append
    unit.store.open()

    channel.publish((_operator_command("cmd-1"), _outcome("cmd-1")))
    unit.drain_side_channels()
    assert unit.stats.control_records_written == 1
    assert len(published) == 1
    assert unit.stats.write_failures == 0

    before = unit.store.sink_errors
    channel.publish((_operator_command("cmd-1"), _outcome("cmd-1")))
    unit.drain_side_channels()
    unit.store.close()

    assert unit.stats.control_records_written == 1, "the refused row is not written"
    assert len(published) == 1, "and it is not published as persisted"
    assert unit.stats.write_failures == 1
    assert unit.store.sink_errors > before
    assert unit.store.duplicate_writes >= 1

    stored = sqlite3.connect(tmp_path / "telemetry.sqlite3")
    try:
        assert next(iter(stored.execute("SELECT COUNT(*) FROM control_audit")))[0] == 1
    finally:
        stored.close()


def test_the_operator_snapshot_never_lists_a_refused_control_row(tmp_path: Path) -> None:
    """§9. The read model is fed by the callback, so the refusal has to reach it as silence."""
    from maker5m.persistence import BoundedChannel
    from maker5m.strategy import default_config
    from maker5m.ui import SnapshotPublisher

    channel = BoundedChannel(capacity=8)
    unit = worker(tmp_path, control_audit=channel)
    publisher = SnapshotPublisher(identity=identity(), config=default_config(), bridge=None)
    unit.on_control_record = lambda row: publisher.deliver(
        "control_persisted", {"command_id": row.command_id, "kind": row.kind}
    )
    unit.store.open()
    for _ in range(2):
        channel.publish((_operator_command("cmd-1"), _outcome("cmd-1")))
        unit.drain_side_channels()
    unit.store.close()

    publisher._drain_inbox()
    assert publisher.accepted_commands == [{"command_id": "cmd-1", "kind": "OPERATOR_HALT"}]


def _risk_record(sequence: int) -> Any:
    """One real P9 RiskRecord, produced by the risk controller rather than hand-built."""
    from maker5m.market.events import HealthStatus
    from maker5m.market.timebase import TimestampNs
    from maker5m.risk import RiskConfig, RiskEngine, RiskProvenance
    from maker5m.risk.trace import HealthFrame, RiskController

    control = RiskController(
        engine=RiskEngine(config=RiskConfig()), provenance=RiskProvenance.SUPPORTING_UNIT_TEST
    )
    frame = HealthFrame(
        clob_status=HealthStatus.HEALTHY,
        clob_awaiting_snapshot=False,
        spot_status=HealthStatus.HEALTHY,
    )
    for ordinal in range(sequence + 1):
        record = control.evaluate(
            frame, as_of_ingress_ordinal=ordinal, now_ns=TimestampNs(ordinal + 1)
        )
    return record


def test_a_refused_risk_row_is_not_counted_or_published(tmp_path: Path) -> None:
    """§9, again for RiskRow: the same false-success path, the same contract."""
    from maker5m.persistence import BoundedChannel

    channel = BoundedChannel(capacity=8)
    unit = worker(tmp_path, risk=channel)
    published: list[Any] = []
    unit.on_risk_record = published.append
    unit.store.open()

    channel.publish(_risk_record(0))
    unit.drain_side_channels()
    assert unit.stats.risk_written == 1
    assert len(published) == 1

    # A second verdict at a risk_sequence the file already holds. The unique index refuses it.
    before = unit.store.sink_errors
    channel.publish(_risk_record(0))
    unit.drain_side_channels()
    unit.store.close()

    assert unit.stats.risk_written == 1
    assert len(published) == 1
    assert unit.stats.write_failures == 1
    assert unit.store.sink_errors > before

    stored = sqlite3.connect(tmp_path / "telemetry.sqlite3")
    try:
        assert next(iter(stored.execute("SELECT COUNT(*) FROM risk_records")))[0] == 1
    finally:
        stored.close()
