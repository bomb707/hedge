"""Constructed inputs for the persistence tests.

**SUPPORTING UNIT TEST ONLY.** Everything built here is synthetic and is labelled as such
through `TelemetryProvenance.SYNTHETIC_SUPPORTING_TEST_ONLY`. It exists to exercise schema
round-trips, arithmetic, and refusal paths — the branches a real market cannot be made to
produce on demand. None of it is evidence about a market, and none of it may pass a P11
real-market gate.
"""

from __future__ import annotations

from typing import Any

from maker5m.domain import Outcome, ParameterStatus
from maker5m.execution.live_orders import LiveOrder, OrderLifecycle
from maker5m.execution.prepare import PreparationOutcome, PreparedOrder
from maker5m.execution.reconciler import ReconcileAction, ReconcilePlan, SideAction, SideReason
from maker5m.market.btc_price import BtcPrice
from maker5m.market.events import BookLevel, BookUpdate, EventMeta, SpotTick
from maker5m.market.timebase import TimestampNs
from maker5m.numeric.units import MoneyUnits, PriceUnits, ShareUnits
from maker5m.persistence import MarketIdentity, TelemetryProvenance
from maker5m.strategy.centre import CentreSource
from maker5m.strategy.decision import DecisionEconomics, DecisionTelemetry
from maker5m.strategy.grid import GridPolicy, GridRounding
from maker5m.strategy.quantization import TickRounding

MARKET_ID = "0xmarket"
SLUG = "btc-updown-5m-1787733300"
UP_TOKEN = "2562042478830498198690123456789012345678901234567890123456789012345"
DOWN_TOKEN = "3478458560157955204309450796333112863849099883376864244503863024303"


def identity() -> MarketIdentity:
    return MarketIdentity(
        market_id=MARKET_ID,
        slug=SLUG,
        condition_id="0x" + "ab" * 32,
        provenance=TelemetryProvenance.SYNTHETIC_SUPPORTING_TEST_ONLY.value,
    )


def economics(**overrides: Any) -> DecisionEconomics:
    base: dict[str, Any] = {
        "inventory": ShareUnits(0),
        "n_up": ShareUnits(0),
        "n_down": ShareUnits(0),
        "cost_up": MoneyUnits(0),
        "cost_down": MoneyUnits(0),
        "total_cost": MoneyUnits(0),
        "fees": MoneyUnits(0),
        "estimated_rebates": MoneyUnits(0),
        "realised_rebates": MoneyUnits(0),
        "pnl_if_up_without_rebate": MoneyUnits(0),
        "pnl_if_down_without_rebate": MoneyUnits(0),
        "pnl_if_up_estimated_rebate": MoneyUnits(0),
        "pnl_if_down_estimated_rebate": MoneyUnits(0),
    }
    base.update(overrides)
    return DecisionEconomics(**base)


def telemetry(**overrides: Any) -> DecisionTelemetry:
    from maker5m.market.phase import Phase

    base: dict[str, Any] = {
        "phase": Phase.QUOTE,
        "centre_source": CentreSource.CLOB_MID,
        "centre_status": ParameterStatus.OPEN,
        "raw_centre": None,
        "centre_unavailable": None,
        "tick_rounding": TickRounding.HALF_EVEN,
        "tick_rounding_status": ParameterStatus.OPEN,
        "quantized_centre": PriceUnits(500_000),
        "tick": PriceUnits(10_000),
        "grid_policy": GridPolicy.CANONICAL_OFFSET,
        "grid_policy_status": ParameterStatus.OPEN,
        "grid_rounding": GridRounding.HALF_EVEN,
        "base_lot": None,
        "base_lot_status": ParameterStatus.OPEN,
        "candidate_up_price": PriceUnits(490_000),
        "candidate_up_size": ShareUnits(15_000_000),
        "candidate_down_price": PriceUnits(490_000),
        "candidate_down_size": ShareUnits(15_000_000),
        "eligibility": _eligibility(),
        "band_hard": ShareUnits(100_000_000),
        "band_hard_status": ParameterStatus.OPEN,
        "endgame": None,
        "economics": economics(),
    }
    base.update(overrides)
    return DecisionTelemetry(**base)


def _eligibility() -> Any:
    from maker5m.strategy.eligibility import EligibilityResult

    return EligibilityResult(up_allowed=True, down_allowed=True)


def meta(ordinal: int = 7, timestamp: int = 1_787_733_400_000_000_000) -> EventMeta:
    return EventMeta(
        market_id=MARKET_ID,
        event_id=f"evt-{ordinal:06d}",
        ingress_ordinal=ordinal,
        timestamp=TimestampNs(timestamp),
    )


def book(timestamp: int = 1_787_733_399_000_000_000) -> BookUpdate:
    return BookUpdate(
        meta=meta(timestamp=timestamp),
        up_bid=BookLevel(price=PriceUnits(490_000), size=ShareUnits(50_000_000)),
        up_ask=BookLevel(price=PriceUnits(510_000), size=ShareUnits(40_000_000)),
        down_bid=BookLevel(price=PriceUnits(480_000), size=ShareUnits(30_000_000)),
        down_ask=BookLevel(price=PriceUnits(520_000), size=ShareUnits(20_000_000)),
    )


def spot(timestamp: int = 1_787_733_398_000_000_000) -> SpotTick:
    return SpotTick(
        meta=meta(timestamp=timestamp), price=BtcPrice(units=6_400_012_345_678, scale_decimals=8)
    )


def prepared(outcome: Outcome = Outcome.UP) -> PreparedOrder:
    return PreparedOrder(
        outcome=outcome,
        token_id=UP_TOKEN if outcome is Outcome.UP else DOWN_TOKEN,
        strategy_price=PriceUnits(490_000),
        submission_price=PriceUnits(490_000),
        strategy_size=ShareUnits(15_000_000),
        submission_size=ShareUnits(15_000_000),
        venue_tick=PriceUnits(10_000),
        min_order_size=ShareUnits(5_000_000),
        outcome_status=PreparationOutcome.SAFE,
        observed_ask=PriceUnits(510_000),
    )


def live(outcome: Outcome = Outcome.UP, client_order_id: str = "coid-1") -> LiveOrder:
    return LiveOrder(
        client_order_id=client_order_id,
        outcome=outcome,
        price=PriceUnits(490_000),
        original_size=ShareUnits(15_000_000),
        remaining_size=ShareUnits(15_000_000),
        status=OrderLifecycle.LIVE,
        venue_order_id="venue-1",
    )


def plan(action: ReconcileAction = ReconcileAction.KEEP) -> ReconcilePlan:
    def side(outcome: Outcome) -> SideAction:
        return SideAction(
            outcome=outcome,
            action=action,
            reason=SideReason.UNCHANGED,
            prepared=prepared(outcome),
            live=live(outcome, f"coid-{outcome.value}"),
        )

    return ReconcilePlan(up=side(Outcome.UP), down=side(Outcome.DOWN))


def _default_intent() -> tuple[object, ...]:
    """What the strategy wanted on each side, as primitives."""
    return (
        PriceUnits(490_000),
        ShareUnits(15_000_000),
        PriceUnits(490_000),
        ShareUnits(15_000_000),
    )


def observation(
    seq: int = 0,
    *,
    ordinal: int = 7,
    with_book: bool = True,
    with_spot: bool = True,
    source_timestamp_ns: int | None = None,
    risk: tuple[object, ...] | None = None,
    event_id: str = "clob-000123",
    strategy_intent: tuple[object, ...] | None = None,
    action: ReconcileAction = ReconcileAction.KEEP,
    fill: tuple[object, ...] | None = None,
    decision_telemetry: DecisionTelemetry | None = None,
) -> tuple[object, ...]:
    """One captured cycle in exactly the shape the hot path produces."""
    return (
        seq,
        ordinal,
        "BookUpdate",
        True,
        1_000_000 + seq,
        2_000_000 + seq,
        3_000_000 + seq,
        4_000_000 + seq,
        500,
        600,
        plan(action),
        ShareUnits(50_000_000),
        ShareUnits(30_000_000),
        None,
        None,
        _eligibility(),
        fill,
        decision_telemetry or telemetry(),
        book() if with_book else None,
        spot() if with_spot else None,
        TimestampNs(1_787_733_400_000_000_000),
        source_timestamp_ns,
        risk,
        event_id,
        strategy_intent
        if strategy_intent is not None
        else (
            PriceUnits(490_000),
            ShareUnits(15_000_000),
            PriceUnits(490_000),
            ShareUnits(15_000_000),
        ),
    )


def fill_capture(liquidity: Any = None, **overrides: Any) -> Any:
    """One canonical fill and the two ledger states around the single real apply_fill."""
    from maker5m.accounting.ledger import Fill, LedgerState
    from maker5m.persistence import FillCapture, FillProvenance
    from maker5m.persistence.records import Liquidity

    before = overrides.pop("before", LedgerState())
    fill = overrides.pop(
        "fill",
        Fill(
            outcome=Outcome.UP,
            shares=ShareUnits(10_000_000),
            cost=MoneyUnits(4_900_000),
            fee=MoneyUnits(1_234),
            price=PriceUnits(490_000),
        ),
    )
    after = overrides.pop("after", before.apply_fill(fill))
    base: dict[str, Any] = {
        "fill": fill,
        "before": before,
        "after": after,
        "event_id": "clob-000456",
        "ingress_ordinal": 9,
        "timestamp": TimestampNs(1_787_733_400_000_000_000),
        "token_id": UP_TOKEN,
        "liquidity": liquidity or Liquidity.MAKER,
        "provenance": FillProvenance.SHADOW_MODEL,
        "client_order_id": "coid-1",
        "venue_order_id": "venue-1",
    }
    base.update(overrides)
    return FillCapture(**base)
