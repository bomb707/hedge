"""Base lot L: confirmed values, OPEN selection rule (O03)."""

from __future__ import annotations

import dataclasses

import pytest

from maker5m.domain import ParameterStatus
from maker5m.numeric import SHARE_SCALE, ShareUnits, parse_share
from maker5m.strategy import (
    BASE_LOT_SELECTION_STATUS,
    SUPPORTED_BASE_LOTS,
    BaseLot,
    BaseLotSelector,
    ConfiguredBaseLotSelector,
    UnsupportedBaseLotError,
)
from tests.unit.builders import initial_state


def test_the_confirmed_observed_values() -> None:
    assert SUPPORTED_BASE_LOTS == (15, 20, 25)


@pytest.mark.parametrize("whole", [15, 20, 25])
def test_each_observed_value_is_accepted(whole: int) -> None:
    lot = BaseLot.of(whole)
    assert lot.whole_shares == whole
    assert lot.shares == whole * SHARE_SCALE


@pytest.mark.parametrize("whole", [0, 1, 10, 14, 16, 21, 24, 26, 30, 100, -15])
def test_an_unobserved_value_is_rejected(whole: int) -> None:
    """Encoding a value the reconstruction never saw would invent strategy behaviour."""
    with pytest.raises(UnsupportedBaseLotError):
        BaseLot.of(whole)


def test_a_fractional_base_lot_is_rejected() -> None:
    with pytest.raises(UnsupportedBaseLotError):
        BaseLot(parse_share("15.5"))


def test_the_selection_rule_is_labelled_open() -> None:
    """O03: the values are confirmed, the rule choosing between them is not."""
    assert BASE_LOT_SELECTION_STATUS is ParameterStatus.OPEN


def test_configured_selector_returns_what_it_was_configured_with() -> None:
    for whole in SUPPORTED_BASE_LOTS:
        selector = ConfiguredBaseLotSelector(BaseLot.of(whole))
        assert selector.select(initial_state()).whole_shares == whole


def test_configured_selector_satisfies_the_protocol_and_exposes_its_status() -> None:
    selector: BaseLotSelector = ConfiguredBaseLotSelector(BaseLot.of(20))
    assert selector.status is ParameterStatus.OPEN


def test_the_selector_does_not_infer_l_from_market_conditions() -> None:
    """Inferring L here would close O03 by assumption. The selector must be inert."""
    selector = ConfiguredBaseLotSelector(BaseLot.of(25))
    empty = initial_state()
    assert selector.select(empty) == selector.select(empty)
    assert selector.select(empty).whole_shares == 25


def test_base_lot_is_immutable() -> None:
    lot = BaseLot.of(15)
    with pytest.raises(dataclasses.FrozenInstanceError):
        lot.shares = ShareUnits(1)  # type: ignore[misc]
