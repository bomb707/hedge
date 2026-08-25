"""Exactly one component decides "has this stream been quiet too long?".

**SUPPORTING UNIT TEST ONLY.**

P9 originally carried its own ``last_message_at`` comparison against its own copy of the
threshold. Two authorities for one question is one too many: they can disagree, and the one that
is wrong stays invisible until it matters. P6 owns the monitor, the ``OPERATIONAL`` numbers, and
the ``STALE`` transition; P9 consumes the resulting status and nothing else.
"""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path

import maker5m
from maker5m.feeds.health import DEFAULT_SPOT_STALE_AFTER, StalenessMonitor, StreamHealth
from maker5m.market.events import HealthComponent, HealthStatus
from maker5m.market.timebase import TimestampNs, seconds
from maker5m.risk import RiskConfig, RiskInputs, RiskReason, active_reasons

RISK_SRC = Path(maker5m.__file__).parent / "risk"
NOW = TimestampNs(1_787_647_500_000_000_000)


def risk_sources() -> list[Path]:
    return sorted(RISK_SRC.rglob("*.py"))


def test_risk_never_imports_a_staleness_threshold() -> None:
    banned = {"DEFAULT_CLOB_STALE_AFTER", "DEFAULT_SPOT_STALE_AFTER", "StalenessMonitor"}
    for path in risk_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    assert alias.name not in banned, f"{path.name} imports {alias.name}"


def test_risk_never_instantiates_a_staleness_monitor() -> None:
    for path in risk_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id != "StalenessMonitor", f"{path.name}"


def test_risk_holds_no_last_message_timestamp() -> None:
    """Without the input, the comparison cannot be reintroduced by accident."""
    fields = set(RiskInputs._fields)
    assert not any("last_message" in name for name in fields), sorted(fields)


def test_the_risk_config_owns_no_feed_staleness_threshold() -> None:
    names = {f.name for f in dataclasses.fields(RiskConfig)}
    assert not any("stale" in name for name in names), sorted(names)


def test_risk_compares_no_age_against_a_threshold() -> None:
    """A subtraction of two timestamps compared to a limit is the shape being forbidden."""
    for path in risk_sources():
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            rendered = ast.unparse(node)
            if "now_ns -" in rendered or "- last" in rendered:
                assert "stale" not in rendered.lower(), f"{path.name}: {rendered}"


# -- P6 is the one that decides -------------------------------------------------------------


def test_p6_alone_turns_silence_into_stale() -> None:
    health = StreamHealth(HealthComponent.SPOT_FEED)
    health.mark_snapshot(TimestampNs(NOW))
    monitor = StalenessMonitor(DEFAULT_SPOT_STALE_AFTER)

    quiet_but_fine = TimestampNs(NOW + DEFAULT_SPOT_STALE_AFTER)
    assert not monitor.is_stale(health, quiet_but_fine)
    assert RiskReason.SPOT_STALE not in active_reasons(
        RiskInputs(now_ns=quiet_but_fine, spot_status=health.status, clob_awaiting_snapshot=False),
        RiskConfig(),
    )

    too_quiet = TimestampNs(NOW + DEFAULT_SPOT_STALE_AFTER + 1)
    assert monitor.is_stale(health, too_quiet)
    health.mark_stale()
    assert RiskReason.SPOT_STALE in active_reasons(
        RiskInputs(now_ns=too_quiet, spot_status=health.status, clob_awaiting_snapshot=False),
        RiskConfig(),
    )


def test_changing_the_p6_threshold_changes_when_stale_fires_with_no_risk_change() -> None:
    """The number lives in one place, so tightening it needs no RiskConfig edit."""
    health = StreamHealth(HealthComponent.SPOT_FEED)
    health.mark_snapshot(TimestampNs(NOW))
    at_three_seconds = TimestampNs(NOW + seconds(3))

    lenient = StalenessMonitor(seconds(10))
    strict = StalenessMonitor(seconds(1))

    assert not lenient.is_stale(health, at_three_seconds)
    assert strict.is_stale(health, at_three_seconds)

    # Same RiskConfig either way: P9 has no opinion about when quiet becomes too quiet.
    config = RiskConfig()
    assert not any("stale" in f.name for f in dataclasses.fields(config))


def test_risk_maps_every_p6_status_to_the_documented_reason() -> None:
    config = RiskConfig()

    def reasons(**kwargs: object) -> frozenset[RiskReason]:
        base = {
            "now_ns": NOW,
            "clob_awaiting_snapshot": False,
            "clob_status": HealthStatus.HEALTHY,
            "spot_status": HealthStatus.HEALTHY,
        }
        base.update(kwargs)
        return active_reasons(RiskInputs(**base), config)  # type: ignore[arg-type]

    assert RiskReason.CLOB_STALE in reasons(clob_status=HealthStatus.STALE)
    for status in (HealthStatus.DISCONNECTED, HealthStatus.SEQUENCE_GAP, HealthStatus.UNKNOWN):
        assert RiskReason.CLOB_CONTINUITY_UNCERTAIN in reasons(clob_status=status)
        assert RiskReason.SPOT_STALE in reasons(spot_status=status)
    assert RiskReason.CLOB_CONTINUITY_UNCERTAIN in reasons(clob_awaiting_snapshot=True)
    assert RiskReason.SPOT_STALE in reasons(spot_status=HealthStatus.STALE)
    assert reasons() == frozenset(), "a fully healthy frame names no reason"


def test_the_capture_loop_checks_staleness_without_a_market_event() -> None:
    """Removing P9's timer must not make detection wait for the next message."""
    source = (Path(maker5m.__file__).parent / "feeds" / "capture.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "check_staleness"
    ]
    assert len(calls) >= 3, "the idle, gated, and post-payload paths must all check"
    # And the idle path is inside the queue-timeout handler.
    assert "except TimeoutError" in source
    idle = source[source.index("except TimeoutError") : source.index("except TimeoutError") + 400]
    assert "check_staleness" in idle, "silence must be detected while the queue is quiet"
