"""Token bucket: free under normal load, bounded under excess, cancels never starved."""

from __future__ import annotations

import pytest

from maker5m.domain import ParameterStatus
from maker5m.execution import (
    DEFAULT_CANCEL_RESERVE,
    DEFAULT_RATE_PER_SECOND,
    RATE_LIMIT_STATUS,
    RateDecision,
    RequestClass,
    TokenBucket,
)
from maker5m.market.timebase import NANOS_PER_SECOND, TimestampNs

T0 = TimestampNs(1_000_000_000_000)


def at(seconds: float) -> TimestampNs:
    return TimestampNs(T0 + int(seconds * NANOS_PER_SECOND))


def test_the_rate_is_operational_not_strategy_evidence() -> None:
    """Canonical §20 offers ~8/s as an example operational budget, not a venue limit."""
    assert RATE_LIMIT_STATUS is ParameterStatus.OPERATIONAL
    assert DEFAULT_RATE_PER_SECOND == 8


def test_a_normal_request_passes_immediately() -> None:
    """No fixed requote delay: capacity is available, so the action proceeds now."""
    bucket = TokenBucket()
    assert bucket.acquire(RequestClass.PLACE, at(0)) is RateDecision.ALLOWED


def test_normal_two_sided_quoting_is_never_limited() -> None:
    """One UP and one DOWN replacement per decision, at a realistic event rate."""
    bucket = TokenBucket()
    allowed = 0
    for step in range(200):
        now = at(step * 0.25)  # 4 decisions per second, 2 requests each = 8/s
        for _ in range(2):
            if bucket.acquire(RequestClass.PLACE, now) is RateDecision.ALLOWED:
                allowed += 1
    assert allowed == 400, "normal activity must never be throttled"


def test_excess_is_bounded() -> None:
    bucket = TokenBucket(rate_per_second=8, burst=8, cancel_reserve=2)
    decisions = [bucket.acquire(RequestClass.PLACE, at(0)) for _ in range(20)]
    allowed = sum(1 for d in decisions if d is RateDecision.ALLOWED)
    assert allowed == 8 - DEFAULT_CANCEL_RESERVE
    assert decisions[-1] is RateDecision.DEFERRED


def test_capacity_refills_continuously_not_in_windows() -> None:
    bucket = TokenBucket(rate_per_second=8, burst=8, cancel_reserve=0)
    for _ in range(8):
        bucket.acquire(RequestClass.PLACE, at(0))
    assert bucket.acquire(RequestClass.PLACE, at(0)) is RateDecision.DEFERRED
    # An eighth of a second is one token's worth; no window boundary is involved.
    assert bucket.acquire(RequestClass.PLACE, at(0.125)) is RateDecision.ALLOWED


def test_a_cancel_is_never_starved_by_placements() -> None:
    """Cancelling is how the strategy stops quoting at SETTLING and retires unsafe orders."""
    bucket = TokenBucket(rate_per_second=8, burst=8, cancel_reserve=2)
    for _ in range(50):
        bucket.acquire(RequestClass.PLACE, at(0))
    assert bucket.acquire(RequestClass.PLACE, at(0)) is RateDecision.DEFERRED
    assert bucket.acquire(RequestClass.CANCEL, at(0)) is RateDecision.ALLOWED
    assert bucket.acquire(RequestClass.CANCEL, at(0)) is RateDecision.ALLOWED


def test_the_reserve_is_exhaustible_only_by_cancels() -> None:
    bucket = TokenBucket(rate_per_second=8, burst=8, cancel_reserve=2)
    for _ in range(6):
        assert bucket.acquire(RequestClass.PLACE, at(0)) is RateDecision.ALLOWED
    assert bucket.acquire(RequestClass.PLACE, at(0)) is RateDecision.DEFERRED
    assert bucket.acquire(RequestClass.CANCEL, at(0)) is RateDecision.ALLOWED
    assert bucket.acquire(RequestClass.CANCEL, at(0)) is RateDecision.ALLOWED
    assert bucket.acquire(RequestClass.CANCEL, at(0)) is RateDecision.DEFERRED


def test_the_bucket_never_reads_a_clock() -> None:
    """Time is an argument, so the limiter is deterministically testable (I20)."""
    import ast
    from pathlib import Path

    import maker5m

    source = (Path(maker5m.__file__).parent / "execution" / "rate_limit.py").read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            assert node.attr not in {"monotonic", "monotonic_ns", "time", "time_ns"}
    assert "import time" not in source


def test_no_execution_module_sleeps() -> None:
    """Canonical §20.1: no fixed requote delay anywhere."""
    import ast
    from pathlib import Path

    import maker5m
    from tests.execution.builders import code_without_docstrings

    execution = Path(maker5m.__file__).parent / "execution"
    for path in execution.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                assert node.attr != "sleep", f"{path.name} sleeps"
            if isinstance(node, ast.Name):
                assert node.id != "sleep", f"{path.name} sleeps"
        assert "min_requote" not in code_without_docstrings(path)


@pytest.mark.parametrize(
    ("rate", "burst", "reserve"), [(0, 8, 2), (8, 0, 2), (8, 8, 8), (8, 8, -1)]
)
def test_incoherent_configuration_is_rejected(rate: int, burst: int, reserve: int) -> None:
    with pytest.raises(ValueError):
        TokenBucket(rate_per_second=rate, burst=burst, cancel_reserve=reserve)
