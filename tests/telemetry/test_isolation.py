"""Instrumentation must never become part of deterministic state.

A latency value describes *this run on this machine*. If one reached a `DecisionResult`, a
replayed decision would depend on the machine that recorded it and P5's byte contract would
break (I20). This is the guard that keeps observation and determinism apart.
"""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path

import maker5m
from maker5m.accounting import LedgerState
from maker5m.market import MarketState
from maker5m.replay import decode_journal, encode_journal, verify_replay
from maker5m.strategy import DecisionResult, DecisionTelemetry, StrategyConfig
from maker5m.strategy.decision import DecisionEconomics
from tests.replay.corpus import synthetic_run

SRC = Path(maker5m.__file__).parent
LATENCY_WORDS = ("latency", "perf_counter", "queue_ahead", "elapsed_ns", "duration_ns")


def field_names(cls: type) -> set[str]:
    return {f.name for f in dataclasses.fields(cls)}


def test_no_latency_field_exists_in_deterministic_state() -> None:
    for cls in (MarketState, LedgerState, DecisionResult, DecisionTelemetry, DecisionEconomics):
        for name in field_names(cls):
            lowered = name.lower()
            for word in LATENCY_WORDS:
                assert word not in lowered, f"{cls.__name__}.{name} looks like instrumentation"


def test_no_queue_estimate_field_exists_in_deterministic_state() -> None:
    for cls in (MarketState, DecisionTelemetry, StrategyConfig):
        for name in field_names(cls):
            assert "queue" not in name.lower(), f"{cls.__name__}.{name}"


def test_the_deterministic_core_does_not_import_telemetry() -> None:
    """Plane 2 must not depend on the observation layer."""
    for package in ("numeric", "market", "strategy", "accounting"):
        for path in (SRC / package).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    assert not node.module.startswith("maker5m.telemetry"), path.name
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        assert not alias.name.startswith("maker5m.telemetry"), path.name


def test_no_plane2_module_reads_a_performance_counter() -> None:
    for package in ("numeric", "market", "strategy", "accounting"):
        for path in (SRC / package).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute):
                    assert node.attr != "perf_counter_ns", path.name
                    assert node.attr != "perf_counter", path.name


def test_replay_bytes_are_unchanged_by_the_presence_of_instrumentation() -> None:
    """The P5 byte contract still holds with the telemetry package installed."""
    run = synthetic_run()
    raw = encode_journal(run.journal)
    assert encode_journal(decode_journal(raw)) == raw
    outcome = verify_replay(decode_journal(raw))
    assert outcome.verified
    assert outcome.final_state == run.final_state


def test_a_journal_contains_no_latency_or_queue_data() -> None:
    text = encode_journal(synthetic_run().journal).decode("utf-8")
    for word in ("latency", "perf_counter", "queue_ahead", "AT_FRONT", "SHADOW"):
        assert word not in text, f"{word!r} leaked into the replay journal"


def test_decisions_are_identical_whether_or_not_traces_are_taken() -> None:
    """Taking a measurement must not change the thing being measured."""
    from maker5m.telemetry import Stage, TraceBuilder, perf_now_ns
    from tests.execution.builders import decision, desired, state_at

    state = state_at()
    plain = decision(desired(), state)

    trace = TraceBuilder()
    trace.mark(Stage.RAW_RECEIVE, perf_now_ns)
    instrumented = decision(desired(), state)
    trace.mark(Stage.DECIDE_DONE, perf_now_ns)

    assert instrumented == plain
