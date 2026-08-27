"""Two markets in one process, and the ways that could go wrong.

**SUPPORTING UNIT TEST ONLY.** State isolation, command routing, prearm handoff, cleanup and
eligibility are software mechanics and can be proved here. Nothing in this file says anything
about a market: queue quality, stale rate, classification distributions and fill opportunity come
from real captures, in the corpus, and from nowhere else.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from maker5m.bot import MarketSession, PaperConfig, PrearmRecord, Supervisor, UiPlane
from maker5m.bot.supervisor import MARKET_SECONDS
from maker5m.market.timebase import NANOS_PER_SECOND, TimestampNs
from maker5m.numeric import parse_price, parse_share
from maker5m.ui import COMMAND_SCHEMA_VERSION, CommandKind, OperatorCommand

T0_A = 1_787_811_600
T0_B = T0_A + MARKET_SECONDS


def paper(tmp_path: Path) -> PaperConfig:
    return PaperConfig(
        evidence_dir=tmp_path / "markets",
        corpus_path=tmp_path / "corpus.jsonl",
        ui_dir=tmp_path / "ui",
        buffer_capacity=1_024,
    )


def discovered(t0_seconds: int, suffix: str) -> Any:
    """A market definition in the shape discovery returns. No network, no venue."""
    from maker5m.feeds.discovery import DiscoveredMarket
    from maker5m.feeds.venue import VenueMarketRules
    from maker5m.market import CANONICAL_PHASE_CONFIG
    from maker5m.market.state import MarketDefinition

    slug = f"btc-updown-5m-{t0_seconds}"
    definition = MarketDefinition(
        slug=slug,
        market_id=f"0xmarket-{suffix}",
        up_token_id=f"111-{suffix}",
        down_token_id=f"222-{suffix}",
        t0=TimestampNs(t0_seconds * NANOS_PER_SECOND),
        phase_config=CANONICAL_PHASE_CONFIG,
        tick=parse_price("0.01"),
    )
    return DiscoveredMarket(
        definition=definition,
        venue_rules=VenueMarketRules(parse_price("0.01"), parse_share("5"), source="test"),
        condition_id=f"0xcondition-{suffix}",
        crypto_config={},
        strike_available=False,
        raw_gamma={},
        raw_clob={},
    )


def session(tmp_path: Path, ui: UiPlane, t0_seconds: int, suffix: str) -> MarketSession:
    market = discovered(t0_seconds, suffix)
    t0_ns = t0_seconds * NANOS_PER_SECOND
    return MarketSession(
        market=market,
        config=paper(tmp_path),
        prearm=PrearmRecord(
            slug=market.definition.slug,
            t0_ns=t0_ns,
            started_ns=t0_ns - 70 * NANOS_PER_SECOND,
            ready_ns=t0_ns - 60 * NANOS_PER_SECOND,
            ok=True,
        ),
        ui=ui,
    )


def command(kind: CommandKind, command_id: str) -> OperatorCommand:
    return OperatorCommand(
        schema_version=COMMAND_SCHEMA_VERSION,
        command_id=command_id,
        kind=kind.value,
        issued_at_ns=TimestampNs(1),
    )


# -- identity isolation ------------------------------------------------------------------------


def test_two_sessions_share_no_mutable_trading_state(tmp_path: Path) -> None:
    ui = UiPlane(directory=tmp_path / "ui")
    first = session(tmp_path, ui, T0_A, "a")
    second = session(tmp_path, ui, T0_B, "b")

    assert first.identity.market_id != second.identity.market_id
    for name in (
        "controller",
        "analyzer",
        "metrics",
        "buffer",
        "risk_channel",
        "audit_channel",
        "fill_channel",
        "worker",
        "publisher",
        "control_ingress",
        "quality",
    ):
        assert getattr(first, name) is not getattr(second, name), name
    assert first.database != second.database
    assert first.journal_path != second.journal_path


def test_halting_one_market_does_not_halt_the_other(tmp_path: Path) -> None:
    """The risk authority is per market. An operator halt is not a process-wide mode."""
    ui = UiPlane(directory=tmp_path / "ui")
    first = session(tmp_path, ui, T0_A, "a")
    second = session(tmp_path, ui, T0_B, "b")
    _evaluate_healthy(first)
    _evaluate_healthy(second)

    first.control_ingress.apply(
        command(CommandKind.OPERATOR_HALT, "halt-1"), ingress_ordinal=10, now_ns=TimestampNs(11)
    )
    assert first.controller.state.value == "HALTED"
    assert second.controller.state.value != "HALTED"


def test_a_command_reaches_only_the_active_market(tmp_path: Path) -> None:
    """Both sessions are past their T0 for a few seconds at every handoff."""
    ui = UiPlane(directory=tmp_path / "ui")
    first = session(tmp_path, ui, T0_A, "a")
    second = session(tmp_path, ui, T0_B, "b")
    _evaluate_healthy(first)
    _evaluate_healthy(second)
    ui.active_slug = second.slug
    ui.channel.push(command(CommandKind.OPERATOR_HALT, "halt-1"))

    taken_by_old = ui.drain_commands(
        first.slug, first.control_ingress, ingress_ordinal=5, now_ns=TimestampNs(6)
    )
    assert taken_by_old == 0, "the closing market must not take it"
    assert len(ui.channel) == 1, "and must leave it for the one that is trading"

    taken_by_new = ui.drain_commands(
        second.slug, second.control_ingress, ingress_ordinal=7, now_ns=TimestampNs(8)
    )
    assert taken_by_new == 1
    assert first.controller.state.value != "HALTED"
    assert second.controller.state.value == "HALTED"


def test_a_persisted_command_records_which_market_accepted_it(tmp_path: Path) -> None:
    ui = UiPlane(directory=tmp_path / "ui")
    unit = session(tmp_path, ui, T0_A, "a")
    row = _control_row(unit)
    unit._on_control_persisted(row)
    assert unit.commands[-1]["slug"] == unit.slug
    assert unit.commands[-1]["market_id"] == unit.identity.market_id


def test_a_settlement_for_one_market_cannot_move_another(tmp_path: Path) -> None:
    """Settlement arrives after the next market has started. It is not a global fact."""
    ui = UiPlane(directory=tmp_path / "ui")
    first = session(tmp_path, ui, T0_A, "a")
    second = session(tmp_path, ui, T0_B, "b")
    first.settlement = object()
    assert second.settlement is None
    first.incidents.append("settlement watch failed")
    assert second.incidents == []


# -- lifecycle ---------------------------------------------------------------------------------


def test_release_drops_what_a_closed_market_was_holding(tmp_path: Path) -> None:
    """Two hundred markets in one process. None of them may still be holding a MarketState."""
    ui = UiPlane(directory=tmp_path / "ui")
    unit = session(tmp_path, ui, T0_A, "a")
    unit.risk_channel.publish(object())
    unit.audit_channel.publish(object())
    unit.control_events.publish(("sink", (1.0, True)))
    unit.hot_path_ns.extend([1, 2, 3])

    unit.release()

    assert unit.runs == []
    assert unit.capture is None
    assert len(unit.buffer) == 0
    assert len(unit.risk_channel) == 0
    assert len(unit.audit_channel) == 0
    assert unit.worker.on_decision_record is None
    assert unit.worker.on_risk_record is None
    assert unit.worker.on_control_record is None
    assert unit.analyzer.on_quote is None


def test_the_supervisor_never_recollects_a_completed_market(tmp_path: Path) -> None:
    config = paper(tmp_path)
    supervisor = Supervisor(config=config)
    supervisor.corpus.append(
        {"slug": "btc-updown-5m-1", "verification_status": "COMPLETE", "evidence_eligible": True}
    )
    supervisor.corpus.append(
        {
            "slug": "btc-updown-5m-2",
            "verification_status": "INCOMPLETE",
            "evidence_eligible": False,
        }
    )
    assert supervisor.corpus.completed_slugs() == {"btc-updown-5m-1"}


def test_a_failed_market_is_kept_and_counts_toward_nothing(tmp_path: Path) -> None:
    config = paper(tmp_path)
    supervisor = Supervisor(config=config)
    ui = UiPlane(directory=tmp_path / "ui")
    unit = session(tmp_path, ui, T0_A, "a")
    unit.incidents.append("capture failed: ConnectionResetError")

    entry = supervisor._entry(unit, {"verification_status": "INCOMPLETE", "replay": {}})
    assert entry["evidence_eligible"] is False
    assert entry["incidents"] == ["capture failed: ConnectionResetError"]
    assert supervisor.corpus.append(entry) is True
    assert supervisor.corpus.stats().complete == 0
    assert supervisor.corpus.entries()[0]["slug"] == unit.slug


def test_a_replay_mismatch_makes_a_complete_market_ineligible(tmp_path: Path) -> None:
    """§16: a stream that does not reproduce is not evidence, whatever the store says."""
    supervisor = Supervisor(config=paper(tmp_path))
    ui = UiPlane(directory=tmp_path / "ui")
    unit = session(tmp_path, ui, T0_A, "a")
    unit.worker.stats.decisions_written = 120_000
    unit.risk_states["SAFE"] = 120_000

    good = supervisor._entry(
        unit,
        {
            "verification_status": "COMPLETE",
            "replay": {"status": "EXACT", "byte_roundtrip_identical": True},
        },
    )
    assert good["evidence_eligible"] is True

    mismatched = supervisor._entry(
        unit,
        {
            "verification_status": "COMPLETE",
            "replay": {"status": "MISMATCH", "error": "orders differ at step 41"},
        },
    )
    assert mismatched["evidence_eligible"] is False
    assert mismatched["verification_status"] == "COMPLETE"


def test_a_market_that_produced_almost_nothing_is_flagged_operationally(tmp_path: Path) -> None:
    """OPERATIONAL, and labelled as such: a broken collector, not a strategy judgement."""
    supervisor = Supervisor(config=paper(tmp_path))
    ui = UiPlane(directory=tmp_path / "ui")
    unit = session(tmp_path, ui, T0_A, "a")
    unit.worker.stats.decisions_written = 12

    entry = supervisor._entry(
        unit,
        {
            "verification_status": "COMPLETE",
            "replay": {"status": "EXACT", "byte_roundtrip_identical": True},
        },
    )
    assert entry["evidence_eligible"] is False
    assert any("OPERATIONAL" in fault for fault in entry["operational_faults"])


def test_prearm_that_missed_t0_is_recorded_as_an_operational_fault(tmp_path: Path) -> None:
    supervisor = Supervisor(config=paper(tmp_path))
    ui = UiPlane(directory=tmp_path / "ui")
    unit = session(tmp_path, ui, T0_A, "a")
    unit.worker.stats.decisions_written = 120_000
    unit.prearm = PrearmRecord(
        slug=unit.slug,
        t0_ns=unit.t0_ns,
        started_ns=unit.t0_ns,
        ready_ns=unit.t0_ns + 10 * NANOS_PER_SECOND,
        ok=True,
    )
    entry = supervisor._entry(
        unit,
        {
            "verification_status": "COMPLETE",
            "replay": {"status": "EXACT", "byte_roundtrip_identical": True},
        },
    )
    assert entry["prearm"]["ready_before_t0"] is False
    assert entry["evidence_eligible"] is False


def test_the_supervisor_records_a_prearm_failure_without_stopping(tmp_path: Path) -> None:
    supervisor = Supervisor(config=paper(tmp_path))
    supervisor._record_prearm_failure(
        PrearmRecord(
            slug="btc-updown-5m-9",
            t0_ns=T0_A * NANOS_PER_SECOND,
            started_ns=1,
            ready_ns=2,
            ok=False,
            error="DiscoveryError: gamma returned 0 events",
        )
    )
    entry = supervisor.corpus.entries()[0]
    assert entry["verification_status"] == "NOT_STARTED"
    assert entry["evidence_eligible"] is False
    assert "DiscoveryError" in entry["incidents"][0]


# -- safety ------------------------------------------------------------------------------------


def _code_only(module: Any) -> str:
    """The module's code with every string literal removed, docstrings included.

    Scanned this way because the guard below is looking for a live-trading *path*, and the
    modules deliberately explain in prose that no such path exists. A guard that a truthful
    docstring can trip is a guard people learn to work around; the answer is to scan what runs,
    not to loosen what is forbidden.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        for attribute in ("body", "orelse", "finalbody"):
            block = getattr(node, attribute, None)
            if isinstance(block, list):
                setattr(
                    node,
                    attribute,
                    [
                        item
                        for item in block
                        if not (
                            isinstance(item, ast.Expr)
                            and isinstance(item.value, ast.Constant)
                            and isinstance(item.value.value, str)
                        )
                    ]
                    or [ast.Pass()],
                )
    return ast.unparse(tree)


def test_no_live_write_path_is_reachable_from_the_bot_package() -> None:
    """§3: there is no flag, no environment variable, and no transport that could send one."""
    import maker5m.bot.cold as cold_module
    import maker5m.bot.config as config_module
    import maker5m.bot.corpus as corpus_module
    import maker5m.bot.runner as runner_module
    import maker5m.bot.session as session_module
    import maker5m.bot.settle as settle_module
    import maker5m.bot.supervisor as supervisor_module

    forbidden = (
        "--live",
        "--trade",
        "--send-orders",
        "--redeem",
        "LIVE_TRADING_ENABLED = True",
        "REDEMPTION_ENABLED = True",
        "os.environ",
        "getenv",
        "private_key",
        "signer",
        "redeem_positions",
    )
    for module in (
        config_module,
        cold_module,
        corpus_module,
        runner_module,
        session_module,
        settle_module,
        supervisor_module,
    ):
        source = _code_only(module)
        for banned in forbidden:
            assert banned not in source, f"{module.__name__} contains {banned!r}"


def test_the_session_executor_records_rather_than_sends(tmp_path: Path) -> None:
    from maker5m.execution import RecordingTransport
    from maker5m.feeds import BookTracker, IngressMerger, MarketDataPipeline
    from maker5m.market import MarketState
    from maker5m.strategy import StrategyEngine

    ui = UiPlane(directory=tmp_path / "ui")
    unit = session(tmp_path, ui, T0_A, "a")
    definition = unit.market.definition
    merger = IngressMerger(
        engine=StrategyEngine(unit.strategy_config),
        state=MarketState.initial(definition),
        clock=lambda: definition.t0,
        market_id=definition.market_id,
    )
    unit._attach(
        MarketDataPipeline(
            merger=merger,
            books=BookTracker(definition.up_token_id, definition.down_token_id),
        )
    )
    transport = unit.runs[0].executor.adapter.transport
    assert isinstance(transport, RecordingTransport)
    assert not hasattr(transport, "send_order")


def test_the_safety_flags_are_false_and_recorded() -> None:
    from maker5m.bot import config_identity
    from maker5m.safety import LIVE_TRADING_ENABLED
    from maker5m.settlement import REDEMPTION_ENABLED

    assert LIVE_TRADING_ENABLED is False
    assert REDEMPTION_ENABLED is False
    identity = config_identity(
        PaperConfig(evidence_dir=Path("a"), corpus_path=Path("b"), ui_dir=Path("c"))
    )
    assert identity["live_trading_enabled"] is False
    assert identity["redemption_enabled"] is False
    assert identity["config_sha256"]


def test_two_configurations_do_not_share_an_identity() -> None:
    from maker5m.bot import config_identity

    base = PaperConfig(evidence_dir=Path("a"), corpus_path=Path("b"), ui_dir=Path("c"))
    other = PaperConfig(
        evidence_dir=Path("a"), corpus_path=Path("b"), ui_dir=Path("c"), base_lot=25
    )
    assert config_identity(base)["config_sha256"] != config_identity(other)["config_sha256"]


# -- helpers -----------------------------------------------------------------------------------


def _evaluate_healthy(unit: MarketSession) -> None:
    """Give a session's risk controller a healthy history, without a pipeline."""
    from maker5m.market.events import HealthStatus
    from maker5m.risk.trace import HealthFrame

    frame = HealthFrame(
        clob_status=HealthStatus.HEALTHY,
        clob_awaiting_snapshot=False,
        spot_status=HealthStatus.HEALTHY,
    )
    for ordinal in range(6):
        unit.controller.evaluate(
            frame, as_of_ingress_ordinal=ordinal, now_ns=TimestampNs(ordinal + 1)
        )


def _control_row(unit: MarketSession) -> Any:
    from maker5m.persistence import ControlAuditRow

    return ControlAuditRow(
        schema_version=1,
        persistence_sequence=1,
        market_id=unit.identity.market_id,
        command_id="halt-1",
        kind="OPERATOR_HALT",
        issued_at_ns=1,
        source="operator-ui",
        accepted=True,
        ingress_ordinal=10,
        risk_sequence=0,
        risk_state="HALTED",
        allows_place=False,
        signal_flag=True,
    )


# -- what a closed market lets go of, and what it must not ------------------------------------


class _Counters:
    def summary(self) -> dict[str, int]:
        return {"clob_messages": 486_028, "spot_messages": 26_404}


class _Capture:
    counters = _Counters()

    def __init__(self) -> None:
        self.journal = object()


def test_dropping_the_recorded_steps_keeps_the_feed_counts(tmp_path: Path) -> None:
    """The steps are two hundred megabytes; the counters are two integers.

    An earlier version dropped the capture result whole, which would have recorded zero CLOB
    and zero BTC messages for every market in the corpus — a number nobody would have
    questioned, because zero is what an unpopulated counter looks like.
    """
    ui = UiPlane(directory=tmp_path / "ui")
    unit = session(tmp_path, ui, T0_A, "a")
    unit.capture = _Capture()

    unit._drop_recorded_steps()

    assert unit.capture is None
    assert unit.feed_counters == {"clob_messages": 486_028, "spot_messages": 26_404}

    supervisor = Supervisor(config=paper(tmp_path))
    entry = supervisor._entry(unit, {"verification_status": "COMPLETE", "replay": {}})
    assert entry["feed_counters"]["clob_messages"] == 486_028


def test_the_hot_path_cost_is_recorded_per_market(tmp_path: Path) -> None:
    """The session's own addition to a cycle, measured on the market rather than replayed."""
    ui = UiPlane(directory=tmp_path / "ui")
    unit = session(tmp_path, ui, T0_A, "a")
    unit.hot_path_ns.extend([100, 200, 300, 400])
    supervisor = Supervisor(config=paper(tmp_path))
    entry = supervisor._entry(unit, {"verification_status": "COMPLETE", "replay": {}})
    tiers = entry["worker"]
    assert entry["hot_path_observe_ns"]["n"] == 4
    assert entry["hot_path_observe_ns"]["max"] == 400
    assert isinstance(tiers, dict)
