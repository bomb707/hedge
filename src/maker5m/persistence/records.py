"""Turning captured observations into durable records. Plane 3, pure, and slow if it likes.

Everything expensive lives here: attribute walks, enum-to-string conversion, tuple building,
age arithmetic. None of it runs on the trading path — the hot path captured references and
returned, which is the boundary P8 established and P11 inherits rather than renegotiates.

The economics are **not recomputed**. `DecisionTelemetry` was built by the strategy at decision
time from the authoritative ledger, and it is carried through unchanged. A second projection
here would be a second PnL formula, and the whole reason P1 exists is that there is one.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from maker5m.accounting.ledger import RebateMode
from maker5m.execution.reconciler import ReconcileAction, ReconcilePlan, SideAction
from maker5m.numeric.units import PriceUnits, ShareUnits
from maker5m.persistence.schema import (
    DECISION_SCHEMA_VERSION,
    FILL_SCHEMA_VERSION,
    DecisionRecord,
    ExactRatio,
    FillRecord,
    SideRecord,
)
from maker5m.telemetry.observation import (
    NOT_CAPTURED,
    OBS_BOOK,
    OBS_DECIDE_DONE_NS,
    OBS_DOWN_DEPTH,
    OBS_ELIGIBILITY,
    OBS_EVENT_ID,
    OBS_EVENT_KIND,
    OBS_EVENT_TS,
    OBS_FILL,
    OBS_HEALTHY,
    OBS_INGRESS_ORDINAL,
    OBS_PLAN,
    OBS_PREPARE_DONE_NS,
    OBS_RAW_RECEIVE_NS,
    OBS_RECONCILE_DONE_NS,
    OBS_RISK,
    OBS_SEQ,
    OBS_SOURCE_TS,
    OBS_SPOT,
    OBS_STRATEGY_INTENT,
    OBS_TELEMETRY,
    OBS_UP_DEPTH,
    Observation,
)
from maker5m.telemetry.queue_estimate import QueueEstimate

__all__ = [
    "Liquidity",
    "MarketIdentity",
    "build_decision_record",
    "build_fill_record",
    "is_fill_observation",
    "latency_sample",
]


def _as_int(value: object) -> int:
    """Read an int out of the untyped observation tuple, refusing anything else.

    The tuple is deliberately untyped for speed, so this is where that ends. An assertion rather
    than a coercion: `int(...)` on the wrong thing would invent a number and carry it to disk.
    """
    assert isinstance(value, int), f"expected an int in the observation, got {type(value)}"
    return value


class MarketIdentity:
    """The identity every record in one market carries.

    A separate object because a five-minute market can still be awaiting settlement while the
    next one is already trading, so "the current market" is not a thing the sink may assume.
    """

    __slots__ = ("condition_id", "market_id", "provenance", "slug")

    def __init__(
        self, *, market_id: str, slug: str, condition_id: str | None, provenance: str
    ) -> None:
        self.market_id = market_id
        self.slug = slug
        self.condition_id = condition_id
        self.provenance = provenance


def is_fill_observation(observation: Observation) -> bool:
    return observation[OBS_FILL] is not None


def _age(now: object, then: object) -> int | None:
    """Elapsed nanoseconds, or ``None`` when either end is genuinely unknown.

    Never zero-as-default. An age of zero means the two clocks agreed; an unknown age means the
    source never told us when it spoke, and a reader deciding whether a quote was stale needs to
    be able to tell those apart.
    """
    if not isinstance(now, int) or not isinstance(then, int):
        return None
    return now - then


def _enum_value(value: object) -> str | None:
    return None if value is None else str(getattr(value, "value", value))


def latency_sample(observation: Observation) -> dict[str, int] | None:
    """P8's own stage timings for this cycle, taken from the captured observation.

    The facts are already in the observation — P8 put them there — so nothing is re-timed and
    nothing later reads a mutable merger. An unsampled cycle has `NOT_CAPTURED` stage stamps and
    yields ``None`` rather than zeros: a latency of zero is a measurement, and there was not one.
    """
    receive = observation[OBS_RAW_RECEIVE_NS]
    decide_done = observation[OBS_DECIDE_DONE_NS]
    prepare_done = observation[OBS_PREPARE_DONE_NS]
    reconcile_done = observation[OBS_RECONCILE_DONE_NS]
    if not all(isinstance(v, int) for v in (receive, decide_done, prepare_done, reconcile_done)):
        return None
    if decide_done == NOT_CAPTURED or prepare_done == NOT_CAPTURED:
        return None
    assert isinstance(receive, int)
    assert isinstance(decide_done, int)
    assert isinstance(prepare_done, int)
    assert isinstance(reconcile_done, int)
    if reconcile_done == NOT_CAPTURED:
        return None
    return {
        "decide_ns": decide_done - receive,
        "prepare_ns": prepare_done - decide_done,
        "reconcile_ns": reconcile_done - prepare_done,
        "receive_to_reconcile_ns": reconcile_done - receive,
    }


def _event_id(observation: Observation) -> str:
    """P2's real event identity, or the empty string when the stream genuinely had none.

    Never manufactured. An earlier version built this from the slug and the capture counter,
    which produced a string that looked authoritative, joined to nothing, and changed if the
    telemetry was replayed with different sampling. The real id already exists at ingress.
    """
    value = observation[OBS_EVENT_ID] if len(observation) > OBS_EVENT_ID else ""
    return value if isinstance(value, str) else ""


def _price(intent: object, index: int) -> PriceUnits | None:
    if not isinstance(intent, tuple):
        return None
    value = intent[index]
    return PriceUnits(value) if isinstance(value, int) else None


def _shares(intent: object, index: int) -> ShareUnits | None:
    if not isinstance(intent, tuple):
        return None
    value = intent[index]
    return ShareUnits(value) if isinstance(value, int) else None


def _withdrew(intent: object, plan: ReconcilePlan) -> bool:
    """Whether safety emptied an intent the strategy actually had.

    Read from the two records rather than from a flag someone remembered to set: the strategy
    wanted something on a side, and nothing was prepared for it. That is exactly the situation
    `risk_adjust` produces, and it is the one a bare "no quote" could otherwise hide.
    """
    if not isinstance(intent, tuple):
        return False
    wanted_up = intent[0] is not None
    wanted_down = intent[2] is not None
    return (wanted_up and plan.up.prepared is None) or (wanted_down and plan.down.prepared is None)


def _side_record(
    side: SideAction,
    depth: object,
    estimate: QueueEstimate | None,
    strategy_reason: object,
) -> SideRecord:
    prepared = side.prepared
    live = side.live
    return SideRecord(
        outcome=side.outcome.value,
        action=side.action.value,
        reason=side.reason.value,
        desired_price=None if prepared is None else prepared.submission_price,
        desired_size=None if prepared is None else prepared.submission_size,
        strategy_price=None if prepared is None else prepared.strategy_price,
        strategy_size=None if prepared is None else prepared.strategy_size,
        preparation_outcome=None if prepared is None else prepared.outcome_status.value,
        observed_ask=None if prepared is None else prepared.observed_ask,
        live_client_order_id=None if live is None else live.client_order_id,
        live_venue_order_id=None if live is None else live.venue_order_id,
        live_price=None if live is None else live.price,
        live_original_size=None if live is None else live.original_size,
        live_remaining_size=None if live is None else live.remaining_size,
        live_status=None if live is None else live.status.value,
        queue_ahead=None if estimate is None else estimate.ahead,
        queue_confidence=None if estimate is None else estimate.confidence.value,
        displayed_depth=ShareUnits(depth) if isinstance(depth, int) else None,
        strategy_reason=_enum_value(strategy_reason),
    )


def build_decision_record(
    observation: Observation,
    identity: MarketIdentity,
    *,
    persistence_sequence: int,
    up_estimate: QueueEstimate | None = None,
    down_estimate: QueueEstimate | None = None,
    up_strategy_reason: object = None,
    down_strategy_reason: object = None,
) -> DecisionRecord:
    """Project one captured cycle into a durable record.

    Queue estimates are passed in rather than computed: P8 owns the queue model, and building a
    second one here — even an identical one — would create two answers to a question with one
    right answer. What arrives is P8's own downstream estimate for each side.
    """
    plan = observation[OBS_PLAN]
    assert isinstance(plan, ReconcilePlan)
    intent = observation[OBS_STRATEGY_INTENT]
    telemetry: Any = observation[OBS_TELEMETRY]
    economics = telemetry.economics
    endgame = telemetry.endgame
    book: Any = observation[OBS_BOOK]
    spot: Any = observation[OBS_SPOT]
    event_ts = observation[OBS_EVENT_TS]
    risk = observation[OBS_RISK]

    eligibility = observation[OBS_ELIGIBILITY]
    reasons = tuple(
        sorted(str(getattr(item, "value", item)) for item in getattr(eligibility, "reasons", ()))
    )

    spot_ts = None if spot is None else spot.meta.timestamp
    book_ts = None if book is None else book.meta.timestamp

    return DecisionRecord(
        schema_version=DECISION_SCHEMA_VERSION,
        record_type="decision",
        persistence_sequence=persistence_sequence,
        market_id=identity.market_id,
        slug=identity.slug,
        condition_id=identity.condition_id,
        ingress_ordinal=_as_int(observation[OBS_INGRESS_ORDINAL]),
        event_id=_event_id(observation),
        event_kind=str(observation[OBS_EVENT_KIND]),
        capture_sequence=_as_int(observation[OBS_SEQ]),
        local_monotonic_ns=_as_int(observation[OBS_RAW_RECEIVE_NS]),
        event_timestamp_ns=event_ts,  # type: ignore[arg-type]
        exchange_timestamp_ns=observation[OBS_SOURCE_TS],  # type: ignore[arg-type]
        phase=telemetry.phase.value,
        spot_price_units=None if spot is None else spot.price.units,
        spot_price_scale_decimals=None if spot is None else spot.price.scale_decimals,
        spot_timestamp_ns=spot_ts,
        spot_age_ns=_age(event_ts, spot_ts),
        up_best_bid=None if book is None or book.up_bid is None else book.up_bid.price,
        up_best_ask=None if book is None or book.up_ask is None else book.up_ask.price,
        down_best_bid=None if book is None or book.down_bid is None else book.down_bid.price,
        down_best_ask=None if book is None or book.down_ask is None else book.down_ask.price,
        book_timestamp_ns=book_ts,
        book_age_ns=_age(event_ts, book_ts),
        raw_centre=(
            None
            if telemetry.raw_centre is None
            else ExactRatio(
                numerator=telemetry.raw_centre.numerator,
                denominator=telemetry.raw_centre.denominator,
            )
        ),
        quantized_centre=telemetry.quantized_centre,
        centre_source=telemetry.centre_source.value,
        centre_status=telemetry.centre_status.value,
        centre_unavailable=_enum_value(telemetry.centre_unavailable),
        inventory=economics.inventory,
        n_up=economics.n_up,
        n_down=economics.n_down,
        cost_up=economics.cost_up,
        cost_down=economics.cost_down,
        total_cost=economics.total_cost,
        fees=economics.fees,
        estimated_rebates=economics.estimated_rebates,
        realised_rebates=economics.realised_rebates,
        pnl_if_up_without_rebate=economics.pnl_if_up_without_rebate,
        pnl_if_down_without_rebate=economics.pnl_if_down_without_rebate,
        pnl_if_up_estimated_rebate=economics.pnl_if_up_estimated_rebate,
        pnl_if_down_estimated_rebate=economics.pnl_if_down_estimated_rebate,
        favourite=None if endgame is None else endgame.favourite.value,
        target_inventory=None if endgame is None else endgame.target_inventory,
        base_lot=None if telemetry.base_lot is None else telemetry.base_lot.shares,
        base_lot_status=telemetry.base_lot_status.value,
        grid_policy=telemetry.grid_policy.value,
        grid_policy_status=telemetry.grid_policy_status.value,
        endgame_tilt=None if endgame is None else endgame.tilt,
        endgame_tilt_status=None if endgame is None else endgame.tilt_status.value,
        endgame_band=None if endgame is None else endgame.band,
        endgame_band_status=None if endgame is None else endgame.band_status.value,
        band_hard=telemetry.band_hard,
        band_hard_status=telemetry.band_hard_status.value,
        up=_side_record(plan.up, observation[OBS_UP_DEPTH], up_estimate, up_strategy_reason),
        down=_side_record(
            plan.down, observation[OBS_DOWN_DEPTH], down_estimate, down_strategy_reason
        ),
        strategy_up_price=_price(intent, 0),
        strategy_up_size=_shares(intent, 1),
        strategy_down_price=_price(intent, 2),
        strategy_down_size=_shares(intent, 3),
        risk_withdrew_intent=_withdrew(intent, plan),
        eligibility_reasons=reasons,
        clob_healthy=bool(observation[OBS_HEALTHY]),
        risk_state=None if risk is None else str(risk[1]),  # type: ignore[index]
        risk_sequence=None if risk is None else _as_int(risk[0]),  # type: ignore[index]
        risk_allows_place=None if risk is None else bool(risk[2]),  # type: ignore[index]
        risk_allows_cancel=None if risk is None else bool(risk[3]),  # type: ignore[index]
        provenance=identity.provenance,
    )


ACTION_NAMES: tuple[str, ...] = tuple(action.value for action in ReconcileAction)
"""Every typed reconciler action, so metrics count all of them and none by accident."""


class Liquidity(Enum):
    """How a fill was executed, as the venue reported it.

    ``UNKNOWN`` is a real answer, not a placeholder. A venue that does not say whether we were
    the maker leaves the maker fraction genuinely unknown, and guessing MAKER because we only
    ever post passively would turn an assumption into a statistic. Canonical §27's whole point
    is that a small number of taker fills removes the entire edge, so this field is the one
    least entitled to a default.
    """

    MAKER = "MAKER"
    TAKER = "TAKER"
    UNKNOWN = "UNKNOWN"


def build_fill_record(
    capture: Any,
    identity: MarketIdentity,
    *,
    persistence_sequence: int,
) -> FillRecord:
    """One fill, with both ledger states as they actually were.

    The two states come from the capture and are the ones that existed either side of the
    single authoritative ``apply_fill``. Nothing here re-applies the fill or subtracts it back
    out: a before-state derived by reversing the arithmetic would agree with the ledger by
    construction and could therefore never disagree with it. There is still exactly one
    application of a fill, and it is not this one.
    """
    fill = capture.fill
    before = capture.before
    after = capture.after
    book = capture.book
    spot = capture.spot
    return FillRecord(
        schema_version=FILL_SCHEMA_VERSION,
        record_type="fill",
        persistence_sequence=persistence_sequence,
        market_id=identity.market_id,
        event_id=capture.event_id,
        ingress_ordinal=capture.ingress_ordinal,
        outcome=fill.outcome.value,
        token_id=capture.token_id,
        price=fill.price if fill.price is not None else PriceUnits(0),
        size=fill.shares,
        liquidity=capture.liquidity.value,
        fee=fill.fee,
        provenance=capture.provenance.value,
        inventory_before=before.net_inventory,
        inventory_after=after.net_inventory,
        n_up_before=before.n_up,
        n_up_after=after.n_up,
        n_down_before=before.n_down,
        n_down_after=after.n_down,
        cost_up_before=before.cost_up,
        cost_up_after=after.cost_up,
        cost_down_before=before.cost_down,
        cost_down_after=after.cost_down,
        total_cost_before=before.total_cost,
        total_cost_after=after.total_cost,
        fees_before=before.fees,
        fees_after=after.fees,
        estimated_rebates_before=before.estimated_rebates,
        estimated_rebates_after=after.estimated_rebates,
        realised_rebates_before=before.realised_rebates,
        realised_rebates_after=after.realised_rebates,
        pnl_if_up_before=before.pnl_if_up(RebateMode.WITHOUT_REBATE),
        pnl_if_up_after=after.pnl_if_up(RebateMode.WITHOUT_REBATE),
        pnl_if_down_before=before.pnl_if_down(RebateMode.WITHOUT_REBATE),
        pnl_if_down_after=after.pnl_if_down(RebateMode.WITHOUT_REBATE),
        queue_ahead_before=capture.queue_ahead_before,
        queue_confidence=capture.queue_confidence,
        spot_price_units_at_fill=None if spot is None else spot.price.units,
        spot_price_scale_decimals_at_fill=None if spot is None else spot.price.scale_decimals,
        up_best_bid_at_fill=None if book is None or book.up_bid is None else book.up_bid.price,
        up_best_ask_at_fill=None if book is None or book.up_ask is None else book.up_ask.price,
        down_best_bid_at_fill=(
            None if book is None or book.down_bid is None else book.down_bid.price
        ),
        down_best_ask_at_fill=(
            None if book is None or book.down_ask is None else book.down_ask.price
        ),
        client_order_id=capture.client_order_id,
        venue_order_id=capture.venue_order_id,
    )
