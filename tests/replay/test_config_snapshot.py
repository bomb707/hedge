"""A replay must never depend on what this build's defaults happen to be.

This is the property that makes recorded experiments durable. If a journal recorded under
``OBSERVED_ADJACENT`` silently replayed under whatever ``REFERENCE_GRID_POLICY`` becomes, then
every O01/O03/O04/O05/O06/O13 comparison would drift with the code and prove nothing.
"""

from __future__ import annotations

import dataclasses

from maker5m.numeric import parse_share
from maker5m.replay import (
    JournalProvenance,
    decode_journal,
    encode_journal,
    verify_replay,
)
from maker5m.strategy import (
    REFERENCE_GRID_POLICY,
    REFERENCE_GRID_ROUNDING,
    REFERENCE_TICK_ROUNDING,
    BaseLot,
    ConfiguredBaseLotSelector,
    GridPolicy,
    GridRounding,
    TickRounding,
    default_config,
)
from tests.replay.corpus import synthetic_run

NON_DEFAULT = dataclasses.replace(
    default_config(BaseLot.of(25)),
    grid_policy=GridPolicy.OBSERVED_ADJACENT,
    grid_rounding=GridRounding.HALF_UP,
    tick_rounding=TickRounding.HALF_DOWN,
    endgame_tilt=parse_share("20"),
    endgame_band=parse_share("3"),
    band_hard=parse_share("80"),
)


def test_the_fixture_differs_from_every_current_default() -> None:
    """Otherwise this whole module would pass vacuously."""
    assert NON_DEFAULT.grid_policy is not REFERENCE_GRID_POLICY
    assert NON_DEFAULT.grid_rounding is not REFERENCE_GRID_ROUNDING
    assert NON_DEFAULT.tick_rounding is not REFERENCE_TICK_ROUNDING
    assert NON_DEFAULT.base_lot_selector != default_config().base_lot_selector
    assert NON_DEFAULT.endgame_tilt != default_config().endgame_tilt
    assert NON_DEFAULT.endgame_band != default_config().endgame_band
    assert NON_DEFAULT.band_hard != default_config().band_hard


def test_every_behaviour_affecting_choice_is_captured_in_the_header() -> None:
    journal = synthetic_run(NON_DEFAULT).journal
    restored = decode_journal(encode_journal(journal)).header.config
    assert restored == NON_DEFAULT
    assert restored.grid_policy is GridPolicy.OBSERVED_ADJACENT
    assert restored.grid_rounding is GridRounding.HALF_UP
    assert restored.tick_rounding is TickRounding.HALF_DOWN
    assert restored.endgame_tilt == parse_share("20")
    assert restored.endgame_band == parse_share("3")
    assert restored.band_hard == parse_share("80")
    selector = restored.base_lot_selector
    assert isinstance(selector, ConfiguredBaseLotSelector)
    assert selector.base_lot.whole_shares == 25


def test_the_config_snapshot_covers_every_strategy_config_field() -> None:
    """A field added to StrategyConfig without a codec entry would break durability."""
    import json

    journal = synthetic_run(NON_DEFAULT).journal
    header_line = json.loads(encode_journal(journal).decode("utf-8").split("\n")[0])
    encoded = set(header_line["config"])
    declared = {f.name for f in dataclasses.fields(NON_DEFAULT)}
    assert encoded == declared


def test_a_non_default_journal_verifies_against_its_own_recorded_config() -> None:
    journal = decode_journal(encode_journal(synthetic_run(NON_DEFAULT).journal))
    outcome = verify_replay(journal)
    assert outcome.verified
    assert outcome.config == NON_DEFAULT


def test_a_non_default_journal_round_trips_byte_identically() -> None:
    journal = synthetic_run(NON_DEFAULT).journal
    raw = encode_journal(journal)
    assert encode_journal(decode_journal(raw)) == raw


def test_default_and_non_default_journals_disagree() -> None:
    """Proof that the header config, not the ambient default, drove the replay."""
    default_journal = synthetic_run().journal
    other_journal = synthetic_run(NON_DEFAULT).journal
    assert default_journal.header.config != other_journal.header.config
    assert verify_replay(default_journal).decisions != verify_replay(other_journal).decisions


def test_the_provenance_is_recorded_and_survives_a_round_trip() -> None:
    """Synthetic journals must be identifiable as such, forever."""
    journal = decode_journal(encode_journal(synthetic_run().journal))
    assert journal.header.provenance is JournalProvenance.SYNTHETIC
    assert "SYNTHETIC" in encode_journal(journal).decode("utf-8")


def test_no_reconstructed_target_wallet_journal_exists_in_this_repository() -> None:
    """Guard against synthetic data ever being relabelled as target-wallet evidence.

    L1 and the empirical half of L2 remain blocked precisely because nothing here is
    RECONSTRUCTED, LIVE_PAPER, or LIVE.
    """
    for config in (default_config(), NON_DEFAULT):
        assert synthetic_run(config).journal.header.provenance is JournalProvenance.SYNTHETIC
