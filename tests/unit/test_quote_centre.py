"""Quote centre: exact CLOB midpoint behind a replaceable interface (O01)."""

from __future__ import annotations

import dataclasses

import pytest

from maker5m.domain import ParameterStatus
from maker5m.market import BookLevel, BookUpdate, MarketState, reduce_event
from maker5m.numeric import PRICE_SCALE, DomainError, parse_price, parse_share
from maker5m.strategy import (
    CLOB_MID_STATUS,
    CentreResult,
    CentreSource,
    CentreUnavailable,
    ClobMidCentre,
    QuoteCentre,
    RawCentre,
    quantize_centre,
)
from tests.unit.builders import initial_state, meta

CENTRE = ClobMidCentre()


def state_with_book(**levels: BookLevel | None) -> MarketState:
    update = BookUpdate(
        meta=meta(0, 10),
        up_bid=levels.get("up_bid"),
        up_ask=levels.get("up_ask"),
        down_bid=levels.get("down_bid"),
        down_ask=levels.get("down_ask"),
    )
    return reduce_event(initial_state(), update)


def level(price: str, size: str = "100") -> BookLevel:
    return BookLevel(parse_price(price), parse_share(size))


# -- interface ----------------------------------------------------------------------------


def test_clob_mid_satisfies_the_quote_centre_protocol() -> None:
    centre: QuoteCentre = CENTRE
    assert centre.source is CentreSource.CLOB_MID


def test_the_centre_source_is_labelled_open_not_confirmed() -> None:
    """O01: recommended as the starting point, not established as the wallet's choice."""
    assert CLOB_MID_STATUS is ParameterStatus.OPEN
    assert CENTRE.status is ParameterStatus.OPEN


def test_other_candidate_sources_are_declared_but_not_implemented() -> None:
    assert {s.value for s in CentreSource} >= {"CLOB_MID", "BINANCE_FV", "BLEND"}


# -- exact midpoint -----------------------------------------------------------------------


def test_even_midpoint_is_exact() -> None:
    result = CENTRE.compute(state_with_book(up_bid=level("0.62"), up_ask=level("0.64")))
    assert result.available
    assert result.centre == RawCentre(parse_price("0.63"), 1)


def test_a_tick_aligned_book_always_yields_a_whole_price_unit_midpoint() -> None:
    """Two multiples of a tick sum to an even number of PriceUnits, so the mid is exact.

    The rational representation is still the right contract - it must not depend on the book
    happening to be aligned, and a future blended centre will not be - but on the venue's own
    grid the denominator collapses to 1.
    """
    result = CENTRE.compute(state_with_book(up_bid=level("0.62"), up_ask=level("0.63")))
    assert result.centre == RawCentre(parse_price("0.625"), 1)
    assert result.centre is not None
    assert result.centre.is_exact_price_unit


def test_an_odd_sum_midpoint_is_kept_exact_as_a_half_unit() -> None:
    """An off-grid book would give a genuine half PriceUnit; it is preserved, not rounded.

    Rounding here would move the quantization decision out of the one place that names its
    tie rule, quietly hiding O13.
    """
    result = CENTRE.compute(state_with_book(up_bid=level("0.620000"), up_ask=level("0.630001")))
    assert result.centre is not None
    assert result.centre.denominator == 2
    assert result.centre.numerator == 1_250_001
    assert not result.centre.is_exact_price_unit


def test_a_half_unit_midpoint_still_quantizes_deterministically() -> None:
    from maker5m.numeric import PriceUnits

    result = CENTRE.compute(state_with_book(up_bid=level("0.620000"), up_ask=level("0.630001")))
    assert result.centre is not None
    assert quantize_centre(result.centre, PriceUnits(10_000)) == parse_price("0.63")


def test_endpoint_book_is_handled() -> None:
    result = CENTRE.compute(state_with_book(up_bid=level("0"), up_ask=level("1")))
    assert result.centre == RawCentre(PRICE_SCALE, 2)


def test_crossed_or_locked_book_is_not_rejected_here() -> None:
    """P3 prices the strategy's intent; post-only safety is P7's job (I05, I06)."""
    result = CENTRE.compute(state_with_book(up_bid=level("0.64"), up_ask=level("0.62")))
    assert result.available


# -- unavailable --------------------------------------------------------------------------


def test_no_book_yields_an_explicit_reason() -> None:
    result = CENTRE.compute(initial_state())
    assert not result.available
    assert result.unavailable is CentreUnavailable.NO_BOOK


def test_missing_bid_yields_an_explicit_reason() -> None:
    result = CENTRE.compute(state_with_book(up_ask=level("0.63")))
    assert result.unavailable is CentreUnavailable.NO_UP_BID


def test_missing_ask_yields_an_explicit_reason() -> None:
    result = CENTRE.compute(state_with_book(up_bid=level("0.62")))
    assert result.unavailable is CentreUnavailable.NO_UP_ASK


def test_a_midpoint_is_never_invented_from_one_side() -> None:
    assert CENTRE.compute(state_with_book(up_bid=level("0.62"))).centre is None
    assert CENTRE.compute(state_with_book(up_ask=level("0.63"))).centre is None


def test_the_down_book_alone_never_produces_a_centre() -> None:
    """Canonical §5.2's mirror identity is conditional; it is not assumed here."""
    result = CENTRE.compute(state_with_book(down_bid=level("0.37"), down_ask=level("0.38")))
    assert not result.available
    assert result.unavailable is CentreUnavailable.NO_UP_BID


# -- value objects ------------------------------------------------------------------------


def test_raw_centre_normalises_so_equality_is_meaningful() -> None:
    assert RawCentre(1_260_000, 2) == RawCentre(630_000, 1)
    assert RawCentre(1_260_000, 2).denominator == 1


@pytest.mark.parametrize(
    ("numerator", "denominator"),
    [(-1, 1), (PRICE_SCALE + 1, 1), (2 * PRICE_SCALE + 1, 2), (1, 0), (1, -2)],
)
def test_raw_centre_rejects_out_of_domain_values(numerator: int, denominator: int) -> None:
    with pytest.raises(DomainError):
        RawCentre(numerator, denominator)


def test_centre_result_must_carry_exactly_one_of_centre_or_reason() -> None:
    with pytest.raises(DomainError):
        CentreResult(CentreSource.CLOB_MID)
    with pytest.raises(DomainError):
        CentreResult(
            CentreSource.CLOB_MID,
            centre=RawCentre(1, 1),
            unavailable=CentreUnavailable.NO_BOOK,
        )


def test_centre_values_are_immutable() -> None:
    raw = RawCentre(630_000, 1)
    with pytest.raises(dataclasses.FrozenInstanceError):
        raw.numerator = 1  # type: ignore[misc]


def test_computing_a_centre_does_not_mutate_market_state() -> None:
    state = state_with_book(up_bid=level("0.62"), up_ask=level("0.63"))
    before = dataclasses.astuple(state.ledger)
    ordinal_before = state.last_ingress_ordinal
    CENTRE.compute(state)
    assert state.last_ingress_ordinal == ordinal_before
    assert dataclasses.astuple(state.ledger) == before
    assert state.book is not None
