"""The API error monitor, and why a bare counter would not do.

**SUPPORTING UNIT TEST ONLY.** Real write-API error behaviour is UNRUN / DEFERRED TO P14: no
credential exists, no order has been sent, and no venue has rejected anything. These pin the
window arithmetic that will consume real outcomes when there are some.
"""

from __future__ import annotations

from maker5m.market.timebase import TimestampNs, millis, seconds
from maker5m.risk import ApiErrorMonitor, RiskConfig, clock_drift_exceeded

START = TimestampNs(1_787_647_500_000_000_000)


def at(offset_s: int) -> TimestampNs:
    return TimestampNs(START + seconds(offset_s))


def monitor(threshold: int = 3, window_s: int = 30) -> ApiErrorMonitor:
    return ApiErrorMonitor(window=seconds(window_s), threshold=threshold)


def test_a_single_failure_is_not_a_rate() -> None:
    m = monitor()
    m.record(at(0), success=False)
    assert not m.exceeded(at(0))
    assert m.failures_in_window(at(0)) == 1


def test_reaching_the_threshold_inside_the_window_trips() -> None:
    m = monitor(threshold=3)
    for offset in (0, 1, 2):
        m.record(at(offset), success=False)
    assert m.exceeded(at(2))


def test_failures_age_out_of_the_window() -> None:
    m = monitor(threshold=3, window_s=30)
    for offset in (0, 1, 2):
        m.record(at(offset), success=False)
    assert m.exceeded(at(2))
    assert not m.exceeded(at(40)), "old failures must stop counting"
    assert m.failures_in_window(at(40)) == 0


def test_a_success_does_not_erase_the_window() -> None:
    """Four failures and one success is not a healthy venue."""
    m = monitor(threshold=3)
    for offset in (0, 1, 2, 3):
        m.record(at(offset), success=False)
    m.record(at(4), success=True)
    assert m.exceeded(at(4)), "one success must not clear a window still above threshold"
    assert m.total_successes == 1
    assert m.total_failures == 4


def test_the_monitor_reads_no_clock() -> None:
    """Every answer depends only on the timestamps it was handed."""
    m = monitor(threshold=2)
    m.record(at(0), success=False)
    m.record(at(1), success=False)
    for _ in range(10):
        assert m.exceeded(at(1)) is True
        assert m.exceeded(at(100)) is False


def test_it_is_built_from_configuration() -> None:
    config = RiskConfig()
    m = ApiErrorMonitor.from_config(config)
    assert m.window == config.api_error_window
    assert m.threshold == config.api_error_threshold


def test_clock_drift_is_absolute() -> None:
    limit = millis(250)
    assert not clock_drift_exceeded(0, limit)
    assert not clock_drift_exceeded(int(millis(250)), limit), "at the limit is not beyond it"
    assert clock_drift_exceeded(int(millis(251)), limit)
    assert clock_drift_exceeded(-int(millis(251)), limit), "a clock behind is just as wrong"
