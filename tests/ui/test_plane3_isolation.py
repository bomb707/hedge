"""The ingress path performs no filesystem work, proved by making the filesystem raise.

**SUPPORTING UNIT TEST ONLY.** These are software-mechanics tests, and deliberately not source
scans: the original defect was not in `maker5m.ui` at all — every module there was clean — it was
in the integration runner, which called those clean functions from inside the ingress consumer.
A scan of the UI package would have passed while the bot was doing `listdir` in `on_tick`.

So the filesystem itself is replaced with functions that raise immediately, and the actual
hot-side control poll is run against it. If anything on that path touches a file, the test fails
with the induced error rather than with an assertion — which is the point.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import pytest

from maker5m.market.events import HealthStatus
from maker5m.market.timebase import TimestampNs
from maker5m.risk import RiskConfig, RiskEngine, RiskProvenance, RiskState
from maker5m.risk.trace import HealthFrame, RiskController
from maker5m.ui import (
    COMMAND_SCHEMA_VERSION,
    CommandBridge,
    CommandInbox,
    CommandKind,
    ControlIngress,
    HotCommandChannel,
    OperatorCommand,
    SnapshotChannel,
)

HEALTHY = HealthFrame(
    clob_status=HealthStatus.HEALTHY,
    clob_awaiting_snapshot=False,
    spot_status=HealthStatus.HEALTHY,
)


class ExplodingFilesystem:
    """Every filesystem entry point the UI path could reach, replaced with a raise."""

    def __init__(self) -> None:
        self.touched: list[str] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(name: str) -> Any:
            def raiser(*args: Any, **kwargs: Any) -> Any:
                self.touched.append(name)
                raise OSError(f"the filesystem is unavailable ({name})")

            return raiser

        for target, name in (
            (Path, "glob"),
            (Path, "iterdir"),
            (Path, "read_text"),
            (Path, "write_text"),
            (Path, "unlink"),
            (Path, "mkdir"),
            (Path, "stat"),
            (Path, "replace"),
            (Path, "open"),
            (Path, "exists"),
        ):
            monkeypatch.setattr(target, name, boom(f"Path.{name}"))
        monkeypatch.setattr(os, "listdir", boom("os.listdir"))
        monkeypatch.setattr(os, "replace", boom("os.replace"))
        monkeypatch.setattr(tempfile, "mkstemp", boom("tempfile.mkstemp"))


def command(kind: CommandKind, command_id: str) -> OperatorCommand:
    return OperatorCommand(
        schema_version=COMMAND_SCHEMA_VERSION,
        command_id=command_id,
        kind=kind.value,
        issued_at_ns=1,
    )


def controller() -> RiskController:
    control = RiskController(
        engine=RiskEngine(config=RiskConfig()), provenance=RiskProvenance.SUPPORTING_UNIT_TEST
    )
    for ordinal in range(6):
        control.evaluate(HEALTHY, as_of_ingress_ordinal=ordinal, now_ns=TimestampNs(ordinal))
    return control


def test_the_hot_control_poll_runs_with_no_filesystem_at_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact shape of the ingress owner's UI work, against a filesystem that raises.

    Against the previous P12 this fails immediately: `on_tick` called `inbox.drain()`, which
    globs, and `publisher.maybe_publish()`, which writes.
    """
    channel = HotCommandChannel()
    channel.push(command(CommandKind.OPERATOR_HALT, "halt-1"))
    control = controller()
    ingress = ControlIngress(controller=control)

    exploding = ExplodingFilesystem()
    exploding.install(monkeypatch)

    # This is everything the ingress owner does for the UI, verbatim.
    accepted = [
        ingress.apply(item, ingress_ordinal=10, now_ns=TimestampNs(10))
        for item in channel.pop_all()
    ]

    assert exploding.touched == [], f"the hot path touched {exploding.touched}"
    assert len(accepted) == 1
    assert accepted[0].accepted
    assert control.trace.records[-1].state is RiskState.HALTED


def test_risk_evaluation_continues_with_no_filesystem(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The decision/risk path itself, with the filesystem removed underneath it."""
    control = controller()
    exploding = ExplodingFilesystem()
    exploding.install(monkeypatch)

    for ordinal in range(6, 30):
        record = control.evaluate(
            HEALTHY, as_of_ingress_ordinal=ordinal, now_ns=TimestampNs(ordinal)
        )
    assert record.state is RiskState.SAFE
    assert exploding.touched == []


def test_popping_an_empty_hot_channel_is_not_a_syscall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = HotCommandChannel()
    exploding = ExplodingFilesystem()
    exploding.install(monkeypatch)
    assert channel.pop_all() == []
    assert exploding.touched == []


# -- the bridge absorbs everything the filesystem can do --------------------------------------


def test_a_stalled_bridge_leaves_the_hot_side_untouched(tmp_path: Path) -> None:
    """§22: the bridge does nothing at all, and nothing downstream waits for it."""
    inbox = CommandInbox(tmp_path / "inbox")
    channel = HotCommandChannel()
    bridge = CommandBridge(inbox=inbox, channel=channel, stall=lambda: True)
    inbox.submit(command(CommandKind.OPERATOR_HALT, "halt-1"))

    bridge.start()
    try:
        import time

        time.sleep(0.4)
        assert len(channel) == 0, "a stalled bridge delivers nothing"
        assert channel.pop_all() == []
        # The command is not lost — it is still on disk, waiting.
        assert len(list((tmp_path / "inbox").glob("*.json"))) == 1
    finally:
        bridge.stop(timeout=2)


def test_a_full_hot_channel_defers_rather_than_dropping(tmp_path: Path) -> None:
    """A safety command must never be discarded because the consumer was briefly behind."""
    inbox = CommandInbox(tmp_path / "inbox")
    channel = HotCommandChannel(capacity=2)
    bridge = CommandBridge(inbox=inbox, channel=channel)
    for index in range(5):
        inbox.submit(command(CommandKind.OPERATOR_HALT, f"cmd-{index}"))

    bridge.poll_once()
    assert len(channel) == 2
    assert bridge.stats.deferred == 1
    assert len(list((tmp_path / "inbox").glob("*.json"))) == 3, "the rest are still on disk"

    channel.pop_all()
    bridge.poll_once()
    assert len(channel) == 2


@pytest.mark.parametrize("failing", ["glob", "read_text", "unlink", "stat"])
def test_a_filesystem_failure_stays_inside_the_bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    """§23: none of these may reach the trading loop, and each is recorded."""
    inbox = CommandInbox(tmp_path / "inbox")
    inbox.submit(command(CommandKind.OPERATOR_HALT, "halt-1"))
    channel = HotCommandChannel()
    bridge = CommandBridge(inbox=inbox, channel=channel)

    def raiser(*args: Any, **kwargs: Any) -> Any:
        raise OSError(f"induced {failing} failure")

    monkeypatch.setattr(Path, failing, raiser)
    bridge.poll_once()  # must not raise

    assert bridge.stats.unreadable >= 1 or bridge.stats.errors
    assert channel.pop_all() == [] or bridge.stats.errors


def test_a_malformed_command_is_consumed_and_counted(tmp_path: Path) -> None:
    directory = tmp_path / "inbox"
    directory.mkdir()
    (directory / "00000000000000000001-bad.json").write_text("not json")
    channel = HotCommandChannel()
    bridge = CommandBridge(inbox=CommandInbox(directory), channel=channel)

    bridge.poll_once()
    assert channel.pop_all() == []
    assert bridge.stats.unreadable == 1
    assert not list(directory.glob("*.json")), "not retried forever"


def test_an_oversized_command_is_refused_by_the_bridge(tmp_path: Path) -> None:
    directory = tmp_path / "inbox"
    directory.mkdir()
    (directory / "00000000000000000001-big.json").write_text("x" * 20_000)
    channel = HotCommandChannel()
    bridge = CommandBridge(inbox=CommandInbox(directory), channel=channel)
    bridge.poll_once()
    assert channel.pop_all() == []
    assert bridge.stats.unreadable == 1


def test_the_bridge_reports_whether_the_channel_is_available(tmp_path: Path) -> None:
    """An operator whose halt button does nothing must be told, not left guessing."""
    bridge = CommandBridge(inbox=CommandInbox(tmp_path / "inbox"), channel=HotCommandChannel())
    assert bridge.stats.summary()["available"] is False
    bridge.start()
    try:
        assert bridge.stats.summary()["alive"] is True
    finally:
        bridge.stop(timeout=2)
    assert bridge.stats.summary()["alive"] is False


def test_snapshot_writing_happens_on_the_bridge(tmp_path: Path) -> None:
    """`offer_snapshot` assigns; the bridge writes. The offering thread pays no disk cost."""
    from tests.ui.test_isolation import _snapshot

    path = tmp_path / "snapshot.json"
    bridge = CommandBridge(
        inbox=CommandInbox(tmp_path / "inbox"),
        channel=HotCommandChannel(),
        snapshot=SnapshotChannel(path),
    )
    bridge.offer_snapshot(_snapshot())
    assert not path.exists(), "offering is not writing"
    assert bridge.publish_pending()
    assert path.exists()
    assert bridge.stats.snapshots_published == 1


# -- idempotency belongs to the authority ------------------------------------------------------


def test_the_authority_applies_a_command_id_once(tmp_path: Path) -> None:
    """§24: called directly, twice, bypassing the transport entirely."""
    control = controller()
    ingress = ControlIngress(controller=control)
    before = control.sequence

    first = ingress.apply(
        command(CommandKind.OPERATOR_HALT, "same"), ingress_ordinal=10, now_ns=TimestampNs(10)
    )
    second = ingress.apply(
        command(CommandKind.OPERATOR_HALT, "same"), ingress_ordinal=11, now_ns=TimestampNs(11)
    )

    assert first.accepted and not first.duplicate
    assert not second.accepted and second.duplicate
    assert second.risk_sequence == first.risk_sequence
    assert control.sequence == before + 1, "exactly one RiskRecord"
    assert ingress.duplicates == 1


def test_a_rebuilt_transport_cannot_replay_a_command(tmp_path: Path) -> None:
    """A restarted bridge has no memory. The authority does, and that is where it belongs."""
    control = controller()
    ingress = ControlIngress(controller=control)
    request = command(CommandKind.OPERATOR_HALT, "replayed")

    inbox = CommandInbox(tmp_path / "inbox")
    channel = HotCommandChannel()
    CommandBridge(inbox=inbox, channel=channel)
    inbox.submit(request)
    CommandBridge(inbox=inbox, channel=channel).poll_once()
    for item in channel.pop_all():
        ingress.apply(item, ingress_ordinal=10, now_ns=TimestampNs(10))
    after_first = control.sequence

    # A brand-new inbox and a brand-new bridge: transport dedupe knows nothing about the first.
    fresh_inbox = CommandInbox(tmp_path / "inbox2")
    fresh_inbox.submit(request)
    CommandBridge(inbox=fresh_inbox, channel=channel).poll_once()
    outcomes = [
        ingress.apply(item, ingress_ordinal=11, now_ns=TimestampNs(11))
        for item in channel.pop_all()
    ]

    assert outcomes and outcomes[0].duplicate
    assert control.sequence == after_first, "no second risk-state mutation"


def test_a_duplicate_publishes_no_second_risk_record() -> None:
    published: list[object] = []
    control = controller()
    ingress = ControlIngress(controller=control, publish=published.append)
    for _ in range(3):
        ingress.apply(
            command(CommandKind.OPERATOR_HALT, "one"), ingress_ordinal=10, now_ns=TimestampNs(10)
        )
    assert len(published) == 1
