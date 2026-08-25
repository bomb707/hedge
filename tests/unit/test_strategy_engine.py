"""StrategyEngine.decide: lifecycle, gates, economics, and configurability."""

from __future__ import annotations

import dataclasses

import pytest

from maker5m.accounting import Fill, LedgerState, RebateMode
from maker5m.domain import Outcome, ParameterStatus
from maker5m.market import MarketState, Phase, reduce_event
from maker5m.numeric import (
    ShareUnits,
    money_from_whole,
    parse_money,
    parse_price,
    parse_share,
    share_from_whole,
)
from maker5m.strategy import (
    BAND_HARD_STATUS,
    DEFAULT_ENDGAME_TILT,
    ENDGAME_BAND_STATUS,
    ENDGAME_TILT_STATUS,
    BaseLot,
    CentreUnavailable,
    DesiredOrder,
    DesiredOrders,
    EligibilityReason,
    GridPolicy,
    StrategyConfig,
    StrategyEngine,
    StrategyError,
    TickRounding,
    default_config,
)
from tests.unit.builders import at, initial_state, quoting_state, state_with_inventory

ENGINE = StrategyEngine(default_config())


def sh(text: str) -> ShareUnits:
    return parse_share(text)


def state_at(offset_s: int) -> MarketState:
    return quoting_state(offset_s=offset_s)


# -- A. lifecycle -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("offset_s", "phase"),
    [(0, Phase.PREARM), (2, Phase.PREARM), (285, Phase.SETTLING), (305, Phase.DONE)],
)
def test_non_quoting_phases_emit_no_orders(offset_s: int, phase: Phase) -> None:
    result = ENGINE.decide(state_at(offset_s))
    assert result.telemetry.phase is phase
    assert result.orders.is_empty
    assert result.telemetry.eligibility.up_reasons == (EligibilityReason.PHASE_NOT_QUOTING,)
    assert result.telemetry.eligibility.down_reasons == (EligibilityReason.PHASE_NOT_QUOTING,)


def test_quote_phase_builds_both_candidate_orders() -> None:
    result = ENGINE.decide(state_at(60))
    assert result.telemetry.phase is Phase.QUOTE
    assert result.orders.count == 2
    assert result.orders.up is not None
    assert result.orders.down is not None
    assert result.orders.up.outcome is Outcome.UP
    assert result.orders.down.outcome is Outcome.DOWN


def test_endgame_phase_quotes_under_the_gate() -> None:
    result = ENGINE.decide(state_at(250))
    assert result.telemetry.phase is Phase.ENDGAME
    assert result.telemetry.endgame is not None


def test_the_non_quoting_fast_path_skips_pricing_entirely() -> None:
    """No centre, no base lot, no grid plan - and still exact economics."""
    telemetry = ENGINE.decide(state_at(0)).telemetry
    assert telemetry.raw_centre is None
    assert telemetry.quantized_centre is None
    assert telemetry.base_lot is None
    assert telemetry.candidate_up_price is None
    assert telemetry.candidate_down_size is None
    assert telemetry.endgame is None
    assert telemetry.economics is not None


def test_zero_spread_is_preserved_in_the_emitted_orders() -> None:
    result = ENGINE.decide(state_at(60))
    assert result.orders.up is not None
    assert result.orders.down is not None
    assert result.orders.up.price + result.orders.down.price == parse_price("1")


# -- B. favourite from the raw centre through the engine ----------------------------------------


@pytest.mark.parametrize(
    ("bid", "ask", "expected"),
    [
        ("0.40", "0.42", Outcome.DOWN),
        ("0.49", "0.51", Outcome.DOWN),  # raw 0.50 exactly -> DOWN (A1)
        ("0.58", "0.60", Outcome.UP),
    ],
)
def test_favourite_direction_through_the_engine(bid: str, ask: str, expected: Outcome) -> None:
    result = ENGINE.decide(quoting_state(offset_s=250, bid=bid, ask=ask))
    assert result.telemetry.endgame is not None
    assert result.telemetry.endgame.favourite is expected


def test_engine_uses_the_raw_centre_not_the_quantized_price() -> None:
    """raw 0.504 quantizes to 0.50; the favourite must still be UP."""
    state = quoting_state(offset_s=250, bid="0.503", ask="0.505")
    result = ENGINE.decide(state)
    assert result.telemetry.raw_centre is not None
    assert result.telemetry.raw_centre.numerator == parse_price("0.504")
    assert result.telemetry.quantized_centre == parse_price("0.50")
    assert result.telemetry.endgame is not None
    assert result.telemetry.endgame.favourite is Outcome.UP
    assert result.telemetry.endgame.target_inventory == sh("30")


# -- C/D. endgame targets -----------------------------------------------------------------------


def test_up_favourite_targets_plus_thirty() -> None:
    result = ENGINE.decide(quoting_state(offset_s=250, bid="0.70", ask="0.72"))
    assert result.telemetry.endgame is not None
    assert result.telemetry.endgame.target_inventory == sh("30")
    assert result.telemetry.endgame.tilt == DEFAULT_ENDGAME_TILT
    assert result.telemetry.endgame.tilt_status is ParameterStatus.FITTED


def test_down_favourite_targets_minus_thirty() -> None:
    result = ENGINE.decide(quoting_state(offset_s=250, bid="0.28", ask="0.30"))
    assert result.telemetry.endgame is not None
    assert result.telemetry.endgame.target_inventory == sh("-30")


def test_fitted_and_confirmed_statuses_are_visible_in_telemetry() -> None:
    telemetry = ENGINE.decide(state_at(250)).telemetry
    assert telemetry.endgame is not None
    assert telemetry.endgame.tilt_status is ENDGAME_TILT_STATUS is ParameterStatus.FITTED
    assert telemetry.endgame.band_status is ENDGAME_BAND_STATUS is ParameterStatus.FITTED
    assert telemetry.band_hard_status is BAND_HARD_STATUS is ParameterStatus.CONFIRMED
    assert telemetry.centre_status is ParameterStatus.OPEN
    assert telemetry.grid_policy_status is ParameterStatus.OPEN
    assert telemetry.tick_rounding_status is ParameterStatus.OPEN
    assert telemetry.base_lot_status is ParameterStatus.OPEN


# -- E. gate boundaries through the engine ------------------------------------------------------


@pytest.mark.parametrize(
    ("inventory", "up_emitted", "down_emitted"),
    [
        ("24.999999", True, False),
        ("25", True, False),
        ("25.000001", True, True),
        ("30", True, True),
        ("34.999999", True, True),
        ("35", False, True),
        ("35.000001", False, True),
    ],
)
def test_endgame_gate_boundaries_decide_emission(
    inventory: str, up_emitted: bool, down_emitted: bool
) -> None:
    state = state_with_inventory(inventory, offset_s=250, bid="0.70", ask="0.72")
    result = ENGINE.decide(state)
    assert result.telemetry.endgame is not None
    assert result.telemetry.endgame.favourite is Outcome.UP
    assert (result.orders.up is not None) is up_emitted
    assert (result.orders.down is not None) is down_emitted


# -- F. band_hard through the engine ------------------------------------------------------------


@pytest.mark.parametrize("inventory", ["100", "120"])
def test_hard_band_blocks_only_the_outward_up_side(inventory: str) -> None:
    result = ENGINE.decide(state_with_inventory(inventory, offset_s=60))
    assert result.orders.up is None
    assert EligibilityReason.HARD_BAND in result.telemetry.eligibility.up_reasons
    assert result.orders.down is not None


@pytest.mark.parametrize("inventory", ["-100", "-120"])
def test_hard_band_blocks_only_the_outward_down_side(inventory: str) -> None:
    result = ENGINE.decide(state_with_inventory(inventory, offset_s=60))
    assert result.orders.down is None
    assert EligibilityReason.HARD_BAND in result.telemetry.eligibility.down_reasons
    assert result.orders.up is not None


def test_hard_band_never_changes_a_price() -> None:
    """It is an eligibility wall only - not a skew, not a pull toward zero (I17)."""
    inside = ENGINE.decide(state_with_inventory("0", offset_s=60))
    at_wall = ENGINE.decide(state_with_inventory("100", offset_s=60))
    assert inside.telemetry.candidate_up_price == at_wall.telemetry.candidate_up_price
    assert inside.telemetry.candidate_down_price == at_wall.telemetry.candidate_down_price
    assert inside.telemetry.quantized_centre == at_wall.telemetry.quantized_centre


# -- H. ENDGAME changes eligibility only (A5) ---------------------------------------------------


@pytest.mark.parametrize("inventory", ["0", "12", "-12", "30", "-30", "28.63", "-28.63"])
def test_candidate_prices_and_sizes_are_identical_between_quote_and_endgame(
    inventory: str,
) -> None:
    """A5, Detailed §29: the endgame is an overlay on the normal candidate quote."""
    quote = ENGINE.decide(state_with_inventory(inventory, offset_s=60, bid="0.70", ask="0.72"))
    endgame = ENGINE.decide(state_with_inventory(inventory, offset_s=250, bid="0.70", ask="0.72"))
    assert quote.telemetry.phase is Phase.QUOTE
    assert endgame.telemetry.phase is Phase.ENDGAME
    assert quote.telemetry.candidate_up_price == endgame.telemetry.candidate_up_price
    assert quote.telemetry.candidate_down_price == endgame.telemetry.candidate_down_price
    assert quote.telemetry.candidate_up_size == endgame.telemetry.candidate_up_size
    assert quote.telemetry.candidate_down_size == endgame.telemetry.candidate_down_size
    assert quote.telemetry.raw_centre == endgame.telemetry.raw_centre
    assert quote.telemetry.base_lot == endgame.telemetry.base_lot


def test_endgame_never_manufactures_a_special_thirty_share_order() -> None:
    state = state_with_inventory("0", offset_s=250, bid="0.70", ask="0.72")
    result = ENGINE.decide(state)
    assert result.orders.up is not None
    assert result.orders.up.size == sh("15")  # the ordinary grid size for L=15 at I=0


# -- I. centre unavailable ----------------------------------------------------------------------


def test_missing_book_produces_no_orders_and_a_typed_reason() -> None:
    state = quoting_state(offset_s=60, with_book=False)
    result = ENGINE.decide(state)
    assert result.orders.is_empty
    assert result.telemetry.centre_unavailable is CentreUnavailable.NO_BOOK
    assert result.telemetry.eligibility.up_reasons == (EligibilityReason.CENTRE_UNAVAILABLE,)


def test_no_price_is_invented_when_the_centre_is_unavailable() -> None:
    telemetry = ENGINE.decide(quoting_state(offset_s=60, with_book=False)).telemetry
    assert telemetry.raw_centre is None
    assert telemetry.quantized_centre is None
    assert telemetry.candidate_up_price is None


def test_one_sided_book_reports_the_precise_missing_side() -> None:
    from maker5m.market import BookLevel, BookUpdate
    from tests.unit.builders import meta

    state = reduce_event(
        initial_state(),
        BookUpdate(
            meta=meta(0, 60),
            up_bid=BookLevel(parse_price("0.62"), parse_share("100")),
            up_ask=None,
            down_bid=None,
            down_ask=None,
        ),
    )
    result = ENGINE.decide(state)
    assert result.telemetry.centre_unavailable is CentreUnavailable.NO_UP_ASK
    assert result.orders.is_empty


# -- J/K. economics -----------------------------------------------------------------------------


def mandatory_ledger_state(offset_s: int = 60) -> MarketState:
    """120 UP at $72, 100 DOWN at $50 - the load-bearing accounting example."""
    ledger = (
        LedgerState()
        .apply_fill(Fill(Outcome.UP, share_from_whole(120), money_from_whole(72)))
        .apply_fill(Fill(Outcome.DOWN, share_from_whole(100), money_from_whole(50)))
    )
    return dataclasses.replace(quoting_state(offset_s=offset_s), ledger=ledger)


def test_mandatory_economic_regression_through_decision_telemetry() -> None:
    """Inventory +20 of the winning side is still a losing market (Canonical §3.1, I01)."""
    economics = ENGINE.decide(mandatory_ledger_state()).telemetry.economics
    assert economics.n_up == share_from_whole(120)
    assert economics.n_down == share_from_whole(100)
    assert economics.cost_up == money_from_whole(72)
    assert economics.cost_down == money_from_whole(50)
    assert economics.total_cost == money_from_whole(122)
    assert economics.inventory == share_from_whole(20)
    assert economics.pnl_if_up_without_rebate == money_from_whole(-2)
    assert economics.pnl_if_down_without_rebate == money_from_whole(-22)
    assert economics.inventory > 0
    assert economics.pnl_if_up_without_rebate < 0


def test_economics_are_present_in_every_phase() -> None:
    for offset in (0, 60, 250, 285, 305):
        economics = ENGINE.decide(mandatory_ledger_state(offset)).telemetry.economics
        assert economics.total_cost == money_from_whole(122)
        assert economics.pnl_if_up_without_rebate == money_from_whole(-2)


def test_estimated_and_no_rebate_views_are_both_exact_and_distinct() -> None:
    base = mandatory_ledger_state()
    state = dataclasses.replace(
        base, ledger=base.ledger.accrue_estimated_rebate(parse_money("0.98"))
    )
    economics = ENGINE.decide(state).telemetry.economics
    assert economics.pnl_if_up_without_rebate == money_from_whole(-2)
    assert economics.pnl_if_up_estimated_rebate == money_from_whole(-2) + parse_money("0.98")
    assert economics.pnl_if_up_without_rebate != economics.pnl_if_up_estimated_rebate
    assert economics.estimated_rebates == parse_money("0.98")
    assert economics.realised_rebates == 0


def test_endgame_settlement_edges_are_recorded() -> None:
    base = mandatory_ledger_state(offset_s=250)
    result = ENGINE.decide(base)
    endgame = result.telemetry.endgame
    assert endgame is not None
    assert endgame.settlement_edge_favourite == base.ledger.pnl_if(
        endgame.favourite, RebateMode.ESTIMATED_REBATE
    )
    assert endgame.settlement_edge_underdog == base.ledger.pnl_if(
        endgame.favourite.other, RebateMode.ESTIMATED_REBATE
    )
    assert endgame.settlement_edge_favourite != endgame.settlement_edge_underdog


def test_no_invented_pnl_eligibility_gate_exists() -> None:
    """Both settlement branches deeply negative must not suppress a side.

    Canonical §17 says *monitor*; §32 records the edges and gates on phase, the endgame band,
    and band_hard only. Inventing a threshold would be a strategy change.
    """
    ledger = (
        LedgerState()
        .apply_fill(Fill(Outcome.UP, share_from_whole(10), money_from_whole(90)))
        .apply_fill(Fill(Outcome.DOWN, share_from_whole(10), money_from_whole(90)))
    )
    state = dataclasses.replace(quoting_state(offset_s=60), ledger=ledger)
    result = ENGINE.decide(state)
    economics = result.telemetry.economics
    assert economics.pnl_if_up_without_rebate < 0
    assert economics.pnl_if_down_without_rebate < 0
    assert result.orders.count == 2


# -- L/M. configurability -----------------------------------------------------------------------


@pytest.mark.parametrize("policy", list(GridPolicy))
def test_the_engine_runs_under_both_o04_grid_policies(policy: GridPolicy) -> None:
    """Neither is asserted to be target-wallet correct; both must simply work."""
    config = dataclasses.replace(default_config(), grid_policy=policy)
    result = StrategyEngine(config).decide(state_with_inventory("-28.63", offset_s=60))
    assert result.orders.count == 2
    assert result.telemetry.grid_policy is policy
    assert result.orders.up is not None
    assert result.orders.up.size == sh("13.63")


def test_the_two_o04_policies_still_diverge_through_the_engine() -> None:
    state = state_with_inventory("-28.63", offset_s=60)
    canonical = StrategyEngine(
        dataclasses.replace(default_config(), grid_policy=GridPolicy.CANONICAL_OFFSET)
    ).decide(state)
    observed = StrategyEngine(
        dataclasses.replace(default_config(), grid_policy=GridPolicy.OBSERVED_ADJACENT)
    ).decide(state)
    assert canonical.orders.down is not None
    assert observed.orders.down is not None
    assert canonical.orders.down.size == sh("16.37")
    assert observed.orders.down.size == sh("1.37")


@pytest.mark.parametrize("rounding", list(TickRounding))
def test_the_engine_runs_under_every_o13_tick_policy(rounding: TickRounding) -> None:
    config = dataclasses.replace(default_config(), tick_rounding=rounding)
    result = StrategyEngine(config).decide(quoting_state(offset_s=60, bid="0.62", ask="0.63"))
    assert result.orders.count == 2
    assert result.telemetry.tick_rounding is rounding


def test_the_tick_policy_can_change_the_quoted_price() -> None:
    """raw 0.625 is an exact tie, so the named policy decides the level."""
    prices = {}
    for rounding in TickRounding:
        config = dataclasses.replace(default_config(), tick_rounding=rounding)
        result = StrategyEngine(config).decide(quoting_state(offset_s=60, bid="0.62", ask="0.63"))
        assert result.orders.up is not None
        prices[rounding] = result.orders.up.price
    assert prices[TickRounding.HALF_UP] == parse_price("0.63")
    assert prices[TickRounding.HALF_DOWN] == parse_price("0.62")
    assert prices[TickRounding.HALF_EVEN] == parse_price("0.62")


@pytest.mark.parametrize("whole", [15, 20, 25])
def test_the_engine_runs_under_every_supported_base_lot(whole: int) -> None:
    config = default_config(BaseLot.of(whole))
    result = StrategyEngine(config).decide(state_with_inventory("0", offset_s=60))
    assert result.orders.up is not None
    assert result.orders.up.size == sh(str(whole))
    assert result.telemetry.base_lot == BaseLot.of(whole)


# -- config validation --------------------------------------------------------------------------


def test_config_rejects_incoherent_regime_parameters() -> None:
    base = default_config()
    with pytest.raises(StrategyError):
        dataclasses.replace(base, endgame_tilt=ShareUnits(0))
    with pytest.raises(StrategyError):
        dataclasses.replace(base, endgame_band=ShareUnits(0))
    with pytest.raises(StrategyError):
        dataclasses.replace(base, band_hard=ShareUnits(0))
    with pytest.raises(StrategyError):
        dataclasses.replace(base, band_hard=sh("20"))  # inside the 30-share tilt


def test_config_has_no_skew_knobs() -> None:
    """I12: gamma and band_skew are zero and no restoring-skew path may exist."""
    names = {f.name for f in dataclasses.fields(StrategyConfig)}
    assert "gamma" not in names
    assert "band_skew" not in names
    assert not any("skew" in n for n in names)


# -- N/O. determinism and immutability ----------------------------------------------------------


def test_the_same_state_and_config_give_exactly_the_same_result() -> None:
    state = state_with_inventory("-28.63", offset_s=250)
    first = ENGINE.decide(state)
    for _ in range(100):
        assert ENGINE.decide(state) == first


def test_deciding_never_mutates_market_state() -> None:
    state = state_with_inventory("-28.63", offset_s=250)
    before = dataclasses.astuple(state.ledger)
    ordinal = state.last_ingress_ordinal
    timestamp = state.last_event_timestamp
    ENGINE.decide(state)
    assert dataclasses.astuple(state.ledger) == before
    assert state.last_ingress_ordinal == ordinal
    assert state.last_event_timestamp == timestamp
    assert state.phase_at_timestamp(at(250)) is Phase.ENDGAME


def test_decision_values_are_immutable() -> None:
    result = ENGINE.decide(state_at(250))
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.orders = DesiredOrders()  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.telemetry.phase = Phase.DONE  # type: ignore[misc]
    assert result.orders.up is not None
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.orders.up.size = sh("1")  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.telemetry.economics.total_cost = money_from_whole(1)  # type: ignore[misc]


def test_only_buy_intents_exist() -> None:
    """No SELL, HEDGE, FLATTEN, MERGE, SPLIT, or CONVERT is representable (I15, I16)."""
    names = {f.name for f in dataclasses.fields(DesiredOrder)}
    assert names == {"outcome", "price", "size"}
    assert "side" not in names
    assert "action" not in names


def test_at_most_two_intents_exist() -> None:
    names = {f.name for f in dataclasses.fields(DesiredOrders)}
    assert names == {"up", "down"}


def test_desired_orders_reject_a_mismatched_slot() -> None:
    from maker5m.numeric import DomainError

    up = DesiredOrder(Outcome.UP, parse_price("0.63"), sh("1"))
    with pytest.raises(DomainError):
        DesiredOrders(down=up)


def test_desired_order_rejects_a_non_positive_size() -> None:
    from maker5m.numeric import DomainError

    with pytest.raises(DomainError):
        DesiredOrder(Outcome.UP, parse_price("0.63"), sh("0"))
