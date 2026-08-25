"""Shadow queue slots must follow the executable order lifecycle, not strategy intent.

Independent review found the first P8 implementation advancing a slot whenever the strategy
*wanted* a price, including the ~119,116 sides the reconciler refused to submit because
post-only would have crossed. A blocked quote therefore acquired a queue estimate, aged it,
credited itself with every depth decrease at that level, and reported itself AT_FRONT of a
queue it had never joined.

These are the tests that make that impossible.
"""

from __future__ import annotations

from maker5m.domain import Outcome
from maker5m.execution import OrderLifecycle, ReconcileAction
from maker5m.numeric import parse_share
from maker5m.telemetry import ExecutionQuality, QualityReason, QueueConfidence
from tests.telemetry.harness import analyzed, build, wants

# -- the mandatory post-only regression ------------------------------------------------


def test_a_post_only_blocked_quote_never_owns_a_queue_slot() -> None:
    """Desired 0.63 against a 0.63 ask: P7 blocks it, so no slot may exist.

    Then the same blocked price persists for many book cycles while depth at 0.63 falls from
    40 to 5. A desired-price model would have banked all 35 of that as consumption ahead and
    declared itself at the front. There must be no slot to bank it into.
    """
    harness = build()

    harness_ask = "0.63"
    for size in ("40", "30", "20", "5"):
        step_blocked(harness, ask=harness_ask, bid_size=size)

    assert analyzed(harness).shadow.acquired == 0
    assert analyzed(harness).shadow.estimate(Outcome.UP) is None
    assert analyzed(harness).counters.actions.get("BLOCKED", 0) >= 4
    assert analyzed(harness).counters.quality.get("AT_FRONT", 0) == 0
    assert analyzed(harness).counters.reasons.get(QualityReason.POST_ONLY_BLOCK.value, 0) >= 4
    # No slot means no age, no keeps, and nothing to lose.
    assert analyzed(harness).shadow.kept == 0
    assert analyzed(harness).shadow.lost == 0


def test_the_slot_begins_only_when_the_quote_becomes_submittable() -> None:
    """Same desired price throughout. The ask moves, so the order becomes placeable."""
    harness = build()
    for size in ("40", "30", "20"):
        step_blocked(harness, ask="0.63", bid_size=size)
    assert analyzed(harness).shadow.acquired == 0

    # The ask steps away; 0.63 is now a legal post-only bid. Depth there is 12.
    step_quoting(harness, ask="0.64", bid_size="12")

    estimate = analyzed(harness).shadow.estimate(Outcome.UP)
    assert estimate is not None, "a placed order must own a slot"
    assert analyzed(harness).shadow.acquired == 1
    # It inherits nothing from the blocked period: the initial estimate is exactly the depth
    # displayed at the moment of dispatch.
    assert estimate.ahead == parse_share("12")
    assert estimate.displayed_at_submit == parse_share("12")
    assert estimate.confidence is QueueConfidence.ESTIMATED
    assert not estimate.at_front


# -- the rest of the lifecycle ----------------------------------------------------------


def test_place_acquires_and_keep_preserves_the_same_slot() -> None:
    harness = build()
    step_quoting(harness, ask="0.64", bid_size="20")
    first = analyzed(harness).shadow.estimate(Outcome.UP)
    assert first is not None

    for size in ("18", "14", "9"):
        step_quoting(harness, ask="0.64", bid_size=size)

    later = analyzed(harness).shadow.estimate(Outcome.UP)
    assert later is not None
    assert later.client_order_id == first.client_order_id, "KEEP must not change identity"
    assert analyzed(harness).shadow.acquired == 1
    assert analyzed(harness).shadow.kept == 3
    assert analyzed(harness).shadow.lost == 0
    # 20 -> 9 is 11 units of consumption ahead of us.
    assert later.ahead == parse_share("9")
    assert analyzed(harness).counters.actions.get("KEEP", 0) >= 3


def test_a_price_change_loses_the_slot_and_grants_none_until_a_later_place() -> None:
    """P7 is CANCEL_THEN_PLACE: a REPLACE gives up the slot and waits."""
    harness = build()
    step_quoting(harness, ask="0.64", bid_size="20")
    original = analyzed(harness).shadow.estimate(Outcome.UP)
    assert original is not None

    step_quoting(harness, ask="0.64", bid_size="20", price="0.62")

    assert analyzed(harness).counters.actions.get("REPLACE", 0) == 1
    assert analyzed(harness).shadow.lost == 1
    assert analyzed(harness).shadow.loss_reasons == {"PRICE_CHANGE": 1}
    assert analyzed(harness).shadow.estimate(Outcome.UP) is None, (
        "REPLACE must not grant a new slot"
    )
    assert analyzed(harness).shadow.acquired == 1

    # The next cycle finds the side free and actually places.
    step_quoting(harness, ask="0.64", bid_size="7", price="0.62")
    replacement = analyzed(harness).shadow.estimate(Outcome.UP)
    assert replacement is not None
    assert replacement.client_order_id != original.client_order_id
    assert analyzed(harness).shadow.acquired == 2


def test_withdrawing_the_desired_order_cancels_and_loses_the_slot() -> None:
    harness = build()
    step_quoting(harness, ask="0.64", bid_size="20")
    assert analyzed(harness).shadow.estimate(Outcome.UP) is not None

    step_none(harness)

    assert analyzed(harness).counters.actions.get("CANCEL", 0) == 1
    assert analyzed(harness).shadow.estimate(Outcome.UP) is None
    assert analyzed(harness).shadow.loss_reasons == {"DESIRED_WITHDRAWN": 1}


def test_a_partial_fill_keeps_the_slot_and_reports_the_front() -> None:
    harness = build()
    step_quoting(harness, ask="0.64", bid_size="20")
    before = analyzed(harness).shadow.estimate(Outcome.UP)
    assert before is not None and before.ahead == parse_share("20")

    harness.apply_shadow_fill(Outcome.UP, parse_share("5"))

    order = harness.executor.orders.current(Outcome.UP)
    assert order is not None
    assert order.status is OrderLifecycle.PARTIALLY_FILLED
    assert order.remaining_size == parse_share("10")

    after = analyzed(harness).shadow.estimate(Outcome.UP)
    assert after is not None
    assert after.client_order_id == before.client_order_id, "a partial fill keeps the slot"
    assert after.ahead == 0

    # The remainder matches the newly desired size, so the reconciler KEEPs it.
    step_quoting(harness, ask="0.64", bid_size="20", size="10")
    assert analyzed(harness).counters.actions.get("KEEP", 0) == 1
    kept = analyzed(harness).shadow.estimate(Outcome.UP)
    assert kept is not None and kept.client_order_id == before.client_order_id


def test_continuity_loss_invalidates_the_slot_without_losing_it() -> None:
    from tests.telemetry.harness import step

    harness = build()
    step_quoting(harness, ask="0.64", bid_size="20")
    # The feed drops. The order still rests, but nothing about its position survives.
    step(harness, up=wants("0.63"), up_bid="0.63", up_bid_size="20", up_ask="0.64", healthy=False)

    result = analyzed(harness)
    estimate = result.shadow.estimate(Outcome.UP)
    assert estimate is not None
    assert estimate.confidence is QueueConfidence.UNKNOWN
    assert result.shadow.lost == 0, "the order still rests; only the estimate is gone"
    assert result.counters.quality.get(ExecutionQuality.STALE.value, 0) >= 1
    assert result.counters.quality.get(ExecutionQuality.STALE.value, 0) == 2, (
        "both sides of the unhealthy cycle read STALE"
    )
    assert result.gaps == 0, "nothing was dropped; the feed failed, not the telemetry"


# -- classification cannot outrun the order ---------------------------------------------


def test_at_front_requires_a_real_slot_even_when_the_level_is_empty() -> None:
    """An empty level plus a blocked quote is the exact shape that used to read AT_FRONT."""
    harness = build()
    for _ in range(5):
        step_blocked(harness, ask="0.63", bid_size="0")
    assert analyzed(harness).counters.quality.get("AT_FRONT", 0) == 0
    assert analyzed(harness).counters.quality.get("NOT_QUOTING", 0) >= 5


def test_a_placed_order_at_an_empty_level_does_read_at_front() -> None:
    """The corollary: a real order at a fresh level is genuinely at the front."""
    harness = build()
    step_quoting(harness, ask="0.64", bid_size="0")
    assert analyzed(harness).counters.quality.get("AT_FRONT", 0) == 1
    estimate = analyzed(harness).shadow.estimate(Outcome.UP)
    assert estimate is not None and estimate.at_front


# -- metric consistency ------------------------------------------------------------------


def test_loss_totals_reconcile_to_their_typed_reasons() -> None:
    harness = build()
    step_quoting(harness, ask="0.64", bid_size="20")
    step_quoting(harness, ask="0.64", bid_size="20", price="0.62")
    step_quoting(harness, ask="0.64", bid_size="20", price="0.62")
    step_none(harness)

    counters = analyzed(harness).counters
    assert counters.execution_queue_loss_actions == sum(
        counters.execution_queue_loss_reasons.values()
    )
    assert analyzed(harness).shadow.lost == sum(analyzed(harness).shadow.loss_reasons.values())
    assert counters.execution_queue_loss_actions == counters.actions.get(
        "REPLACE", 0
    ) + counters.actions.get("CANCEL", 0)


def test_keep_increments_neither_loss_count() -> None:
    harness = build()
    step_quoting(harness, ask="0.64", bid_size="20")
    before_execution = analyzed(harness).counters.execution_queue_loss_actions
    before_shadow = analyzed(harness).shadow.lost

    for _ in range(6):
        step_quoting(harness, ask="0.64", bid_size="20")

    assert analyzed(harness).counters.actions.get("KEEP", 0) == 6
    assert analyzed(harness).counters.execution_queue_loss_actions == before_execution
    assert analyzed(harness).shadow.lost == before_shadow


def test_a_blocked_side_neither_acquires_keeps_nor_loses() -> None:
    harness = build()
    for _ in range(8):
        step_blocked(harness, ask="0.63", bid_size="30")
    assert (
        analyzed(harness).shadow.acquired,
        analyzed(harness).shadow.kept,
        analyzed(harness).shadow.lost,
    ) == (0, 0, 0)
    assert analyzed(harness).counters.execution_queue_loss_actions == 0


# -- helpers -----------------------------------------------------------------------------


def step_blocked(harness: object, *, ask: str, bid_size: str) -> None:
    """Desire 0.63 while the ask sits at ``ask``; when ask == 0.63 post-only blocks it."""
    from tests.telemetry.harness import step

    assert isinstance(harness, type(build()))
    step(harness, up=wants("0.63"), up_bid="0.63", up_bid_size=bid_size, up_ask=ask)
    plan_action_is(harness, ReconcileAction.BLOCKED)


def step_quoting(
    harness: object, *, ask: str, bid_size: str, price: str = "0.63", size: str = "15"
) -> None:
    from tests.telemetry.harness import step

    assert isinstance(harness, type(build()))
    step(harness, up=wants(price, size), up_bid=price, up_bid_size=bid_size, up_ask=ask)


def step_none(harness: object) -> None:
    from tests.telemetry.harness import step

    assert isinstance(harness, type(build()))
    step(harness, up=None, up_bid="0.62", up_bid_size="0", up_ask="0.64")


def plan_action_is(harness: object, action: ReconcileAction) -> None:
    """Assert the harness really produced the action the test name claims."""
    from maker5m.telemetry import InstrumentedRun

    assert isinstance(harness, InstrumentedRun)
    assert analyzed(harness).counters.actions.get(action.value, 0) > 0


def test_derived_acting_matches_the_plan_the_observation_carries() -> None:
    """The analyzer derives ``acting`` from the plan; it must equal the plan's own answer.

    ``acting`` decides whether a cycle is always-traced, so a disagreement would silently stop
    actions being observed.
    """
    from maker5m.execution.reconciler import ReconcilePlan
    from maker5m.telemetry.analyzer import TelemetryAnalyzer
    from maker5m.telemetry.observation import (
        OBS_DOWN_DEPTH,
        OBS_DOWN_PLACED_ID,
        OBS_PLAN,
        OBS_UP_DEPTH,
        OBS_UP_PLACED_ID,
    )
    from tests.telemetry.harness import step

    harness = build()
    step(harness, up=wants("0.63"), up_bid="0.63", up_bid_size="10", up_ask="0.64")
    for size in ("9", "8", "7"):
        step(harness, up=wants("0.63"), up_bid="0.63", up_bid_size=size, up_ask="0.64")
    step(harness, up=wants("0.61"), up_bid="0.61", up_bid_size="4", up_ask="0.64")
    step(harness, up=wants("0.61"), up_bid="0.61", up_bid_size="4", up_ask="0.64")
    step(harness, up=None, up_bid="0.62", up_bid_size="0", up_ask="0.64")

    derived: list[bool] = []
    expected: list[bool] = []
    replay = TelemetryAnalyzer(sampling=harness.sampling)
    for observation in harness.buffer:
        plan = observation[OBS_PLAN]
        assert isinstance(plan, ReconcilePlan)
        derived.append(
            replay._advance_slots(
                plan,
                observation[OBS_UP_DEPTH],
                observation[OBS_DOWN_DEPTH],
                observation[OBS_UP_PLACED_ID],
                observation[OBS_DOWN_PLACED_ID],
            )
        )
        expected.append(plan.request_count > 0)

    assert derived == expected, f"derived {derived}, plan says {expected}"
    assert any(expected), "the run must contain acting cycles"
    assert not all(expected), "and non-acting ones"
