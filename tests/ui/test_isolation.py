"""The UI is a projection and a request, never an authority.

**SUPPORTING UNIT TEST ONLY.** Constructed snapshots and commands, proving software mechanics:
that the UI holds nothing Plane 1 can wait on, that a command changes nothing until the bot
accepts it, and that a release clears exactly one condition. The real-market gate — killing the
process mid-market — is `docs/evidence/P12-UI-CONTROL-PLANE.md`.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
from pathlib import Path

import pytest

from maker5m.market.events import HealthStatus
from maker5m.market.timebase import TimestampNs
from maker5m.risk import RiskConfig, RiskEngine, RiskProvenance, RiskReason, RiskState
from maker5m.risk.trace import HealthFrame, RiskController
from maker5m.ui import (
    COMMAND_SCHEMA_VERSION,
    SNAPSHOT_SCHEMA_VERSION,
    ChannelFullError,
    CommandInbox,
    CommandKind,
    ControlIngress,
    OperatorCommand,
    SnapshotChannel,
    UiSnapshot,
    render_dashboard,
    render_history,
)

HEALTHY = HealthFrame(
    clob_status=HealthStatus.HEALTHY,
    clob_awaiting_snapshot=False,
    spot_status=HealthStatus.HEALTHY,
    order_stream_status=HealthStatus.HEALTHY,
)


def controller() -> RiskController:
    control = RiskController(
        engine=RiskEngine(config=RiskConfig()),
        provenance=RiskProvenance.SUPPORTING_UNIT_TEST,
    )
    for ordinal in range(8):
        control.evaluate(HEALTHY, as_of_ingress_ordinal=ordinal, now_ns=TimestampNs(ordinal))
    return control


def command(kind: CommandKind, command_id: str = "cmd-1") -> OperatorCommand:
    return OperatorCommand(
        schema_version=COMMAND_SCHEMA_VERSION,
        command_id=command_id,
        kind=kind.value,
        issued_at_ns=1_787_000_000_000_000_000,
    )


# -- the UI holds nothing Plane 1 can wait on ---------------------------------------------------


def test_the_ui_package_imports_no_trading_object() -> None:
    """No MarketState, no LedgerState, no LiveOrderTable — not even to read."""
    import maker5m.ui.channel as channel
    import maker5m.ui.model as model
    import maker5m.ui.render as render
    import maker5m.ui.server as server

    forbidden = {"MarketState", "LedgerState", "LiveOrderTable", "StrategyEngine", "Executor"}
    for module in (model, channel, render, server):
        names = set(vars(module))
        assert not (names & forbidden), f"{module.__name__} reaches a trading object"


def test_no_ui_module_holds_a_lock_or_a_blocking_queue() -> None:
    """I19: nothing here is something the trading path could end up waiting on."""
    import maker5m.ui.channel as channel
    import maker5m.ui.control as control
    import maker5m.ui.render as render
    import maker5m.ui.server as server
    import maker5m.ui.snapshot as snapshot

    for module in (channel, control, render, snapshot, server):
        source = inspect.getsource(module)
        for banned in ("threading.Lock", "threading.Condition", "queue.Queue", "acquire("):
            assert banned not in source, f"{module.__name__} uses {banned}"


def test_the_snapshot_is_immutable() -> None:
    snapshot = _snapshot()
    assert dataclasses.is_dataclass(snapshot)
    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.n_up = 99  # type: ignore[misc]


def test_a_command_is_immutable_and_identified() -> None:
    request = command(CommandKind.OPERATOR_HALT)
    with pytest.raises(dataclasses.FrozenInstanceError):
        request.kind = "ANYTHING"  # type: ignore[misc]
    assert request.command_id


def test_an_unknown_command_kind_cannot_be_constructed() -> None:
    with pytest.raises(ValueError, match="not a command this build accepts"):
        OperatorCommand(
            schema_version=COMMAND_SCHEMA_VERSION,
            command_id="x",
            kind="ENABLE_LIVE_TRADING",
            issued_at_ns=1,
        )


def test_the_command_vocabulary_is_two_things() -> None:
    """P12 is visibility and a brake. A command that does not exist cannot be issued."""
    assert {member.value for member in CommandKind} == {
        "OPERATOR_HALT",
        "RELEASE_OPERATOR_HALT",
    }


# -- control is a request, and ordering comes from the market ------------------------------------


def test_a_halt_travels_the_ordered_risk_stream(tmp_path: Path) -> None:
    control = controller()
    ingress = ControlIngress(controller=control)
    before = control.sequence

    outcome = ingress.apply(
        command(CommandKind.OPERATOR_HALT), ingress_ordinal=41, now_ns=TimestampNs(41)
    )
    assert outcome.accepted
    assert outcome.ingress_ordinal == 41
    assert outcome.risk_sequence == before + 1
    assert outcome.risk_state == RiskState.HALTED.value
    assert outcome.allows_place is False

    record = control.trace.records[-1]
    assert RiskReason.OPERATOR_HALT in record.active
    assert record.allows_cancel, "a halt withdraws quotes; it must not trap us in them"


def test_the_browsers_clock_does_not_define_causality() -> None:
    """The command's own timestamp is audit only; the ingress ordinal is the position."""
    control = controller()
    ingress = ControlIngress(controller=control)
    request = OperatorCommand(
        schema_version=COMMAND_SCHEMA_VERSION,
        command_id="cmd-clock",
        kind=CommandKind.OPERATOR_HALT.value,
        issued_at_ns=1,  # implausibly early
    )
    outcome = ingress.apply(request, ingress_ordinal=500, now_ns=TimestampNs(500))
    assert outcome.ingress_ordinal == 500
    assert control.trace.records[-1].signal.as_of_ingress_ordinal == 500
    assert control.trace.records[-1].signal.timestamp == 500


def test_release_clears_only_the_operator_halt() -> None:
    """§16: a resume that swept away a stale feed would be a resume that lied."""
    control = controller()
    ingress = ControlIngress(controller=control)
    ingress.apply(command(CommandKind.OPERATOR_HALT), ingress_ordinal=10, now_ns=TimestampNs(10))

    unhealthy = HealthFrame(
        clob_status=HealthStatus.STALE,
        clob_awaiting_snapshot=False,
        spot_status=HealthStatus.HEALTHY,
    )
    control.evaluate(unhealthy, as_of_ingress_ordinal=11, now_ns=TimestampNs(11))

    outcome = ingress.apply(
        command(CommandKind.RELEASE_OPERATOR_HALT, "cmd-2"),
        ingress_ordinal=12,
        now_ns=TimestampNs(12),
    )
    assert outcome.accepted
    record = control.trace.records[-1]
    assert RiskReason.OPERATOR_HALT not in record.active
    assert RiskReason.CLOB_STALE in record.active, "the stale feed is still stale"
    assert record.state is not RiskState.SAFE
    assert record.allows_place is False


def test_release_cannot_clear_a_latched_reason() -> None:
    """A latch needs positive evidence. An operator's button is not that evidence."""
    control = controller()
    ingress = ControlIngress(controller=control)
    control.operational.position_mismatch = True
    control.evaluate(HEALTHY, as_of_ingress_ordinal=20, now_ns=TimestampNs(20))
    ingress.apply(command(CommandKind.OPERATOR_HALT), ingress_ordinal=21, now_ns=TimestampNs(21))
    control.operational.position_mismatch = False

    ingress.apply(
        command(CommandKind.RELEASE_OPERATOR_HALT, "cmd-2"),
        ingress_ordinal=22,
        now_ns=TimestampNs(22),
    )
    record = control.trace.records[-1]
    assert RiskReason.OPERATOR_HALT not in record.active
    assert RiskReason.POSITION_MISMATCH in record.latched
    assert record.state is RiskState.RECOVERING
    assert not record.allows_place


def test_an_operator_halt_is_not_latched_and_needs_no_reconciliation() -> None:
    """It is the one condition whose evidence is a person deciding."""
    from maker5m.risk import REQUIRES_RECONCILIATION

    assert RiskReason.OPERATOR_HALT not in REQUIRES_RECONCILIATION


# -- the transport is bounded, deduplicating, and never blocks the bot ---------------------------


def test_a_repeated_command_id_is_accepted_once(tmp_path: Path) -> None:
    """A retried submission or a re-posted form is the same command, not a second one."""
    inbox = CommandInbox(tmp_path / "inbox")
    inbox.submit(command(CommandKind.OPERATOR_HALT, "same-id"))
    assert len(inbox.drain()) == 1

    inbox.submit(command(CommandKind.OPERATOR_HALT, "same-id"))
    assert inbox.drain() == []


def test_a_full_inbox_refuses_the_sender_rather_than_growing(tmp_path: Path) -> None:
    """The operator is told. Trading is never told anything."""
    from maker5m.ui.channel import MAX_PENDING_COMMANDS

    inbox = CommandInbox(tmp_path / "inbox")
    for index in range(MAX_PENDING_COMMANDS):
        inbox.submit(command(CommandKind.OPERATOR_HALT, f"cmd-{index}"))
    with pytest.raises(ChannelFullError, match="already waiting"):
        inbox.submit(command(CommandKind.OPERATOR_HALT, "one-too-many"))


def test_draining_an_absent_inbox_is_not_an_error(tmp_path: Path) -> None:
    """The UI may never have run. That is not a condition the bot reacts to."""
    assert CommandInbox(tmp_path / "never-created").drain() == []


@pytest.mark.parametrize(
    "content",
    ["", "not json", "[]", '{"kind": "OPERATOR_HALT"}', '{"schema_version": 99}'],
)
def test_a_malformed_command_file_is_skipped_not_raised(tmp_path: Path, content: str) -> None:
    """A malformed command must not be able to interrupt a market."""
    directory = tmp_path / "inbox"
    directory.mkdir()
    (directory / "00000000000000000001-bad.json").write_text(content)
    inbox = CommandInbox(directory)
    assert inbox.drain() == []
    assert not list(directory.glob("*.json")), "the bad file is consumed, not left to retry"


def test_an_oversized_command_is_refused_unread(tmp_path: Path) -> None:
    directory = tmp_path / "inbox"
    directory.mkdir()
    (directory / "00000000000000000001-big.json").write_text("x" * 20_000)
    assert CommandInbox(directory).drain() == []


# -- the snapshot channel -------------------------------------------------------------------------


def test_a_snapshot_round_trips(tmp_path: Path) -> None:
    channel = SnapshotChannel(tmp_path / "snapshot.json")
    channel.publish(_snapshot())
    loaded = channel.read()
    assert loaded is not None
    assert loaded["slug"] == "btc-updown-5m-1787780700"
    assert loaded["pnl_if_up_without_rebate"] == 4_100_000


def test_a_missing_snapshot_reads_as_no_data_not_as_zero(tmp_path: Path) -> None:
    channel = SnapshotChannel(tmp_path / "absent.json")
    assert channel.read() is None
    page = render_dashboard(None, None)
    assert "NO SNAPSHOT" in page
    assert "no data" in page


def test_a_corrupt_snapshot_reads_as_no_data(tmp_path: Path) -> None:
    path = tmp_path / "snapshot.json"
    path.write_text("{ truncated")
    assert SnapshotChannel(path).read() is None


def test_a_stale_snapshot_is_labelled_stale(tmp_path: Path) -> None:
    channel = SnapshotChannel(tmp_path / "snapshot.json")
    channel.publish(_snapshot())
    page = render_dashboard(channel.read(), 42.0)
    assert "STALE" in page
    assert "not current" in page


def test_publishing_never_raises_into_the_caller(tmp_path: Path) -> None:
    """A full disk is an observability incident, not a trading one."""
    blocked = tmp_path / "blocked"
    blocked.write_text("this is a file, so it cannot also be a directory")
    channel = SnapshotChannel(blocked / "snapshot.json")
    channel.publish(_snapshot())
    assert channel.write_errors == 1
    assert channel.published == 0


# -- rendering formats, and does not compute -----------------------------------------------------


def test_the_dashboard_shows_both_pnl_branches() -> None:
    page = render_dashboard(json.loads(json.dumps(_encoded())), 0.2)
    assert "PnL if UP (no rebate)" in page
    assert "PnL if DOWN (no rebate)" in page
    assert "PnL if UP (est. rebate)" in page
    assert "4.10" in page


def test_absent_values_render_as_unavailable_not_zero() -> None:
    """A genuine zero still renders as 0.00; only *absence* renders as a dash."""
    present = render_dashboard(_encoded(), 0.1)
    assert ">0.00<" in present, "realised rebate really is zero, and says so"

    encoded = _encoded()
    encoded["target_inventory"] = None
    encoded["quantized_centre"] = None
    encoded["favourite"] = None
    absent = render_dashboard(encoded, 0.1)
    assert absent.count('<span class="na">—</span>') > present.count('<span class="na">—</span>'), (
        "the fields that became unknown now read as unknown"
    )


def test_strategy_labels_survive_into_the_page() -> None:
    page = render_dashboard(_encoded(), 0.1)
    for label in ("OPEN", "FITTED", "OPERATIONAL"):
        assert label in page
    assert "O04" in page and "O07" in page


def test_strategy_and_executable_intent_are_shown_separately() -> None:
    page = render_dashboard(_encoded(), 0.1)
    assert "strategy wanted" in page
    assert "execution allowed" in page


def test_the_page_always_states_the_safety_flags() -> None:
    page = render_dashboard(_encoded(), 0.1)
    assert "LIVE TRADING" in page and "DISABLED" in page
    assert "REDEMPTION" in page
    assert "P14 owns live capital" in page


def test_an_incomplete_market_is_shown_and_labelled_ineligible() -> None:
    page = render_history(
        [
            {
                "slug": "btc-updown-5m-1787771100",
                "verification_status": "INCOMPLETE",
                "evidence_eligible": False,
                "decisions": 65438,
                "risk_records": 64922,
                "places_by_risk_state": {"SAFE": 327},
            }
        ]
    )
    assert "btc-updown-5m-1787771100" in page, "not hidden"
    assert "INCOMPLETE" in page
    assert "NOT ELIGIBLE FOR EMPIRICAL STRATEGY EVIDENCE" in page


def test_an_unsupported_archive_is_shown_as_unsupported() -> None:
    page = render_history(
        [{"slug": "x", "verification_status": "UNSUPPORTED", "evidence_eligible": False}]
    )
    assert "UNSUPPORTED" in page
    assert "NOT ELIGIBLE" in page


def _snapshot() -> UiSnapshot:
    from maker5m.ui import ParameterView, SideView

    return UiSnapshot(
        schema_version=SNAPSHOT_SCHEMA_VERSION,
        published_at_ns=1_787_780_700_000_000_000,
        market_id="0xmarket",
        slug="btc-updown-5m-1787780700",
        condition_id="0x" + "ab" * 32,
        phase="QUOTE",
        ingress_ordinal=4242,
        event_timestamp_ns=1_787_780_800_000_000_000,
        elapsed_seconds=100.0,
        remaining_seconds=200.0,
        clob_status="HEALTHY",
        clob_awaiting_snapshot=False,
        spot_status="HEALTHY",
        risk_state="SAFE",
        risk_sequence=4242,
        risk_active=(),
        risk_latched=(),
        allows_place=True,
        allows_cancel=True,
        n_up=20_000_000,
        n_down=13_000_000,
        inventory=7_000_000,
        cost_up=9_800_000,
        cost_down=6_100_000,
        total_cost=15_900_000,
        fees=12_345,
        estimated_rebates=6_789,
        realised_rebates=0,
        pnl_if_up_without_rebate=4_100_000,
        pnl_if_down_without_rebate=-2_900_000,
        pnl_if_up_estimated_rebate=4_106_789,
        pnl_if_down_estimated_rebate=-2_893_211,
        raw_centre_numerator=985_000,
        raw_centre_denominator=2,
        quantized_centre=490_000,
        centre_source="CLOB_MID",
        centre_status="OPEN",
        favourite="UP",
        target_inventory=30_000_000,
        up=SideView(outcome="UP", strategy_price=490_000, strategy_size=15_000_000, action="KEEP"),
        down=SideView(outcome="DOWN", strategy_price=480_000, strategy_size=15_000_000),
        decide_ns=None,
        prepare_ns=None,
        reconcile_ns=None,
        receive_to_reconcile_ns=None,
        resolution_state=None,
        winning_outcome=None,
        authoritative_block=None,
        payout_numerators=(),
        decisions_persisted=4242,
        risk_records_persisted=4242,
        dropped_records=0,
        sink_errors=0,
        telemetry_complete=None,
        live_trading_enabled=False,
        redemption_enabled=False,
        parameters=(
            ParameterView(
                name="grid policy", value="CANONICAL_OFFSET", status="OPEN", open_item="O04"
            ),
            ParameterView(name="endgame tilt", value="30000000", status="FITTED", open_item="O05"),
            ParameterView(name="grid rounding", value="HALF_EVEN", status="OPERATIONAL"),
            ParameterView(name="rebate", value="estimated only", status="OPEN", open_item="O07"),
        ),
    )


def _encoded() -> dict[str, object]:
    from maker5m.ui.channel import _encode

    encoded: dict[str, object] = _encode(_snapshot())
    return encoded
