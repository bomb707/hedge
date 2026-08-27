"""Two markets in one process, and the ways that could go wrong.

**SUPPORTING UNIT TEST ONLY.** State isolation, command routing, prearm handoff, cleanup and
eligibility are software mechanics and can be proved here. Nothing in this file says anything
about a market: queue quality, stale rate, classification distributions and fill opportunity come
from real captures, in the corpus, and from nowhere else.
"""

from __future__ import annotations

import asyncio
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
    """A collection configuration with the broken-collector floors scaled to a unit test.

    The floors are OPERATIONAL and exist to notice a dead feed; a ten-decision fixture is not a
    market and is not pretending to be one. The *rules* under test are the same.
    """
    from maker5m.bot import OperationalThresholds

    return PaperConfig(
        evidence_dir=tmp_path / "markets",
        corpus_path=tmp_path / "corpus.jsonl",
        ui_dir=tmp_path / "ui",
        buffer_capacity=1_024,
        thresholds=OperationalThresholds(min_decisions=5, min_clob_messages=2, min_spot_messages=2),
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


def collected(
    tmp_path: Path,
    ui: UiPlane,
    *,
    decisions: int = 10,
    classifications: int | None = None,
    actions: int | None = None,
    clob: int = 5_000,
    spot: int = 300,
    warm: bool = True,
    latency: bool = True,
) -> MarketSession:
    """A session in the state a clean real market leaves behind.

    Everything a corpus entry is judged on is set explicitly here, so a test can break exactly
    one of them and watch eligibility fail for that reason and no other.
    """
    from maker5m.telemetry.analyzer import QuoteEvent
    from maker5m.telemetry.classifier import ExecutionQuality, QualityReason, QuoteClassification

    unit = session(tmp_path, ui, T0_A, "a")
    unit.worker.stats.decisions_written = decisions
    unit.risk_states["SAFE"] = decisions
    unit.feed_counters = {"clob_messages": clob, "spot_messages": spot}

    total = 2 * decisions if classifications is None else classifications
    for index in range(total):
        unit.quality.observe(
            QuoteEvent(
                index,
                "BookUpdate",
                unit.t0_ns + index,
                "QUOTE",
                "UP" if index % 2 == 0 else "DOWN",
                QuoteClassification(
                    quality=ExecutionQuality.AT_FRONT, reason=QualityReason.QUOTING
                ),
            )
        )
    unit.analyzer.counters.actions = {"KEEP": 2 * decisions if actions is None else actions}
    if latency:
        # A real artifact, written by the real writer: the market's own live samples are what
        # eligibility now turns on, so a fixture that faked one would be testing nothing.
        for index in range(20):
            unit.analyzer.latency.clob_receive_to_decide.add(100_000 + index)
            unit.analyzer.latency.spot_receive_to_decide.add(90_000 + index)
            unit.analyzer.latency.clob_receive_to_reconcile.add(140_000 + index)
            unit.analyzer.latency.spot_receive_to_reconcile.add(130_000 + index)
            unit.analyzer.latency.decide_duration.add(24_000 + index)
        asyncio.run(unit.write_latency_artifact(build_identity(unit.config)))
    if warm:
        ready_at_t0(unit, clob_since=20, spot_since=10)
    return unit


def ready_at_t0(
    unit: MarketSession,
    *,
    clob_since: float | None,
    spot_since: float | None,
) -> None:
    """Put a session in the warm state P6 would have reported at its T0.

    Both halves, because they are different facts: the first-seen milestones are diagnostics,
    and the boundary snapshot is what eligibility is judged on.
    """
    clob = None if clob_since is None else int(unit.t0_ns - clob_since * NANOS_PER_SECOND)
    spot = None if spot_since is None else int(unit.t0_ns - spot_since * NANOS_PER_SECOND)
    if clob is not None:
        unit._note_warm("clob_book_ready_ns", TimestampNs(clob))
    if spot is not None:
        unit._note_warm("spot_first_valid_ns", TimestampNs(spot))
    unit._note_prearm(
        {
            "at_ns": unit.t0_ns,
            "clob_ready": clob is not None,
            "spot_ready": spot is not None,
            "clob_ready_since_ns": clob,
            "spot_ready_since_ns": spot,
        }
    )


def build_identity(config: PaperConfig) -> dict[str, Any]:
    """The build identity a clean acceptance run would stamp on everything it writes."""
    from maker5m.bot import config_identity

    return {
        "source_revision": "revision",
        "source_tree_sha": "tree",
        "config_sha256": str(config_identity(config)["config_sha256"]),
        "epoch": config.epoch,
        "run_mode": "ACCEPTANCE_CLEAN",
    }


def acceptance(config: PaperConfig) -> Supervisor:
    """A supervisor that believes it is running from clean source.

    Stated rather than inherited: a unit test must not pass or fail depending on whether the
    repository happens to have uncommitted edits in it while the suite runs, and the rule under
    test — dirty source is never acceptance evidence — has its own tests.
    """
    supervisor = Supervisor(config=config)
    supervisor.identity = {
        **supervisor.identity,
        "working_tree_clean": True,
        "source_revision": "revision",
        "source_tree_sha": "tree",
    }
    supervisor.run_mode = "ACCEPTANCE_CLEAN"
    return supervisor


def clean_cold() -> dict[str, Any]:
    return {
        "verification_status": "COMPLETE",
        "replay": {"status": "EXACT", "byte_roundtrip_identical": True},
    }


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
    supervisor = acceptance(paper(tmp_path))
    ui = UiPlane(directory=tmp_path / "ui")
    unit = collected(tmp_path, ui)

    good = supervisor._entry(unit, clean_cold())
    assert good["evidence_eligible"] is True, good["operational_faults"]

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
    supervisor = acceptance(paper(tmp_path))
    ui = UiPlane(directory=tmp_path / "ui")
    unit = collected(tmp_path, ui, decisions=2)

    entry = supervisor._entry(unit, clean_cold())
    assert entry["evidence_eligible"] is False
    assert any("OPERATIONAL" in fault for fault in entry["operational_faults"])


# -- §5, §7: the exhaustive denominators -------------------------------------------------------


def test_a_clean_market_classifies_both_sides_of_every_decision(tmp_path: Path) -> None:
    supervisor = acceptance(paper(tmp_path))
    ui = UiPlane(directory=tmp_path / "ui")
    entry = supervisor._entry(collected(tmp_path, ui, decisions=10), clean_cold())
    assert entry["classification"]["expected_classifications"] == 20
    assert entry["classification"]["actual_classifications"] == 20
    assert entry["classification"]["classification_complete"] is True
    assert entry["action_total"] == 20
    assert entry["evidence_eligible"] is True


def test_a_short_classification_count_makes_the_market_ineligible(tmp_path: Path) -> None:
    """The P13 corpus counted 30,734 classifications for 143,740 decisions and called it a rate."""
    supervisor = acceptance(paper(tmp_path))
    ui = UiPlane(directory=tmp_path / "ui")
    unit = collected(tmp_path, ui, decisions=10, classifications=7)
    entry = supervisor._entry(unit, clean_cold())
    assert entry["classification"]["classification_complete"] is False
    assert entry["evidence_eligible"] is False
    assert any("side classifications" in fault for fault in entry["operational_faults"])


def test_a_short_action_count_makes_the_market_ineligible(tmp_path: Path) -> None:
    supervisor = acceptance(paper(tmp_path))
    ui = UiPlane(directory=tmp_path / "ui")
    unit = collected(tmp_path, ui, decisions=10, actions=13)
    entry = supervisor._entry(unit, clean_cold())
    assert entry["evidence_eligible"] is False
    assert any("side actions" in fault for fault in entry["operational_faults"])


def test_the_action_counts_are_reported_by_kind(tmp_path: Path) -> None:
    supervisor = acceptance(paper(tmp_path))
    ui = UiPlane(directory=tmp_path / "ui")
    unit = collected(tmp_path, ui, decisions=10)
    unit.analyzer.counters.actions = {"KEEP": 14, "PLACE": 4, "BLOCKED": 2}
    entry = supervisor._entry(unit, clean_cold())
    assert entry["action_counts"] == {"BLOCKED": 2, "KEEP": 14, "PLACE": 4}
    assert entry["action_total"] == 20


# -- §9-11: discovery readiness is not feed readiness ------------------------------------------


def test_discovery_and_feed_readiness_are_separate_facts(tmp_path: Path) -> None:
    """P13's first corpus reported a 74.9 s "prearm lead" that was `discover_market` returning."""
    ui = UiPlane(directory=tmp_path / "ui")
    unit = collected(tmp_path, ui)
    prearm = unit.prearm_summary()

    assert prearm["discovery_ready_ns"] < prearm["clob_book_ready_ns"]
    assert prearm["discovery_lead_seconds"] == 60.0
    assert prearm["clob_lead_seconds"] == 20.0
    assert prearm["spot_lead_seconds"] == 10.0
    assert prearm["feed_ready_lead_seconds"] == 10.0, "the later of the two feeds decides"
    assert prearm["feed_ready_before_t0"] is True
    assert prearm["clob_ready_at_t0"] is True
    assert prearm["spot_ready_at_t0"] is True


def test_a_market_with_no_book_before_t0_is_not_warm(tmp_path: Path) -> None:
    supervisor = acceptance(paper(tmp_path))
    ui = UiPlane(directory=tmp_path / "ui")
    unit = collected(tmp_path, ui, warm=False)
    ready_at_t0(unit, clob_since=None, spot_since=10)

    prearm = unit.prearm_summary()
    assert prearm["feed_ready_ns"] is None
    assert prearm["feed_ready_before_t0"] is False
    entry = supervisor._entry(unit, clean_cold())
    assert entry["evidence_eligible"] is False
    assert any("not warm before T0" in fault for fault in entry["operational_faults"])


def test_a_market_with_no_spot_before_t0_is_not_warm(tmp_path: Path) -> None:
    supervisor = acceptance(paper(tmp_path))
    ui = UiPlane(directory=tmp_path / "ui")
    unit = collected(tmp_path, ui, warm=False)
    ready_at_t0(unit, clob_since=20, spot_since=None)

    assert unit.prearm_summary()["feed_ready_ns"] is None
    assert supervisor._entry(unit, clean_cold())["evidence_eligible"] is False


def test_a_book_that_arrives_after_t0_is_not_prearm(tmp_path: Path) -> None:
    """Warm means warm *before* the market's first event, not eventually."""
    supervisor = acceptance(paper(tmp_path))
    ui = UiPlane(directory=tmp_path / "ui")
    unit = collected(tmp_path, ui, warm=False)
    ready_at_t0(unit, clob_since=-5, spot_since=10)

    prearm = unit.prearm_summary()
    assert prearm["feed_ready_before_t0"] is False
    assert prearm["feed_ready_lead_seconds"] == -5.0
    assert supervisor._entry(unit, clean_cold())["evidence_eligible"] is False


def test_a_warm_milestone_is_recorded_once(tmp_path: Path) -> None:
    ui = UiPlane(directory=tmp_path / "ui")
    unit = collected(tmp_path, ui, warm=False)
    unit._note_warm("clob_book_ready_ns", TimestampNs(100))
    unit._note_warm("clob_book_ready_ns", TimestampNs(999))
    assert unit.warm["clob_book_ready_ns"] == 100


# -- §18: the feed floors are enforced ---------------------------------------------------------


def test_a_market_with_no_clob_messages_is_ineligible(tmp_path: Path) -> None:
    supervisor = acceptance(paper(tmp_path))
    ui = UiPlane(directory=tmp_path / "ui")
    unit = collected(tmp_path, ui, clob=0, spot=10_000)
    entry = supervisor._entry(unit, clean_cold())
    assert entry["evidence_eligible"] is False
    assert any("CLOB messages" in fault for fault in entry["operational_faults"])


def test_a_market_with_no_spot_messages_is_ineligible(tmp_path: Path) -> None:
    supervisor = acceptance(paper(tmp_path))
    ui = UiPlane(directory=tmp_path / "ui")
    unit = collected(tmp_path, ui, clob=10_000, spot=0)
    entry = supervisor._entry(unit, clean_cold())
    assert entry["evidence_eligible"] is False
    assert any("BTC messages" in fault for fault in entry["operational_faults"])


def test_both_feeds_above_their_floors_raise_no_feed_fault(tmp_path: Path) -> None:
    supervisor = acceptance(paper(tmp_path))
    ui = UiPlane(directory=tmp_path / "ui")
    entry = supervisor._entry(collected(tmp_path, ui, clob=10_000, spot=500), clean_cold())
    assert not [f for f in entry["operational_faults"] if "messages" in f]
    assert entry["evidence_eligible"] is True


def test_the_supervisor_records_a_prearm_failure_without_stopping(tmp_path: Path) -> None:
    supervisor = acceptance(paper(tmp_path))
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
    import asyncio

    ui = UiPlane(directory=tmp_path / "ui")
    unit = session(tmp_path, ui, T0_A, "a")
    unit.capture = _Capture()

    asyncio.run(unit._drop_recorded_steps())

    assert unit.capture is None
    assert unit.feed_counters == {"clob_messages": 486_028, "spot_messages": 26_404}

    supervisor = acceptance(paper(tmp_path))
    entry = supervisor._entry(unit, {"verification_status": "COMPLETE", "replay": {}})
    assert entry["feed_counters"]["clob_messages"] == 486_028


def test_the_recorded_steps_are_freed_in_chunks_that_yield(tmp_path: Path) -> None:
    """Freeing 150,000 step graphs in one C traversal stalled the market that was trading.

    The corrected pilot measured a single 480 ms `observe` against a 25 microsecond median, with
    a 2,535-observation buffer high-water on the live market at the time. Nothing may stall the
    ingress owner — not even letting go of a closed market.
    """
    import asyncio

    from maker5m.bot.session import STEP_RELEASE_CHUNK

    ui = UiPlane(directory=tmp_path / "ui")
    unit = session(tmp_path, ui, T0_A, "a")
    unit.capture = _Capture()

    class Merger:
        def __init__(self) -> None:
            self.steps = list(range(3 * STEP_RELEASE_CHUNK + 7))

    class Pipeline:
        def __init__(self) -> None:
            self.merger = Merger()

    class Run:
        def __init__(self) -> None:
            self.pipeline = Pipeline()

    run = Run()
    unit.runs.append(run)  # type: ignore[arg-type]

    yields = 0

    async def drive() -> None:
        nonlocal yields
        task = asyncio.ensure_future(unit._drop_recorded_steps())
        while not task.done():
            await asyncio.sleep(0)
            yields += 1
        await task

    asyncio.run(drive())
    assert run.pipeline.merger.steps == []
    assert yields >= 4, "it gave the loop the chance to run between chunks"


def test_the_hot_path_cost_is_recorded_per_market(tmp_path: Path) -> None:
    """The session's own addition to a cycle, measured on the market rather than replayed."""
    ui = UiPlane(directory=tmp_path / "ui")
    unit = session(tmp_path, ui, T0_A, "a")
    unit.hot_path_ns.extend([100, 200, 300, 400])
    supervisor = acceptance(paper(tmp_path))
    entry = supervisor._entry(unit, {"verification_status": "COMPLETE", "replay": {}})
    tiers = entry["worker"]
    assert entry["hot_path_observe_ns"]["n"] == 4
    assert entry["hot_path_observe_ns"]["max"] == 400
    assert isinstance(tiers, dict)
