"""The modular fingerprint at scale, and the fact that it cannot arbitrate O04.

Canonical §12.2 makes this a mandatory conformance test: every generated order size must
satisfy

```text
up_size   ≡ (-I) mod 5 shares
down_size ≡ (+I) mod 5 shares
```

"If a generated order fails the fingerprint, the sizing engine is wrong."

The second purpose of this module matters just as much. Both O04 readings satisfy the
fingerprint for **every** inventory, so passing it is not evidence for either one. That is
asserted here explicitly, so the conflict cannot quietly disappear behind a green test suite.

Deterministic seeded generation rather than Hypothesis, matching the P1 decision: P3 adds no
dependency, and a fixed seed keeps any failure reproducible.
"""

from __future__ import annotations

import random

import pytest

from maker5m.numeric import SHARE_SCALE, ShareUnits, parse_share
from maker5m.strategy import GRID, BaseLot, GridPolicy, GridRounding, plan_grid

SEED = 20260825
SAMPLE_SIZE = 100_000
LOTS = tuple(BaseLot.of(w) for w in (15, 20, 25))


def inventories() -> list[ShareUnits]:
    """A deterministic corpus of fractional inventories.

    Spans the observed excursion range (~±100 shares, Canonical §14.1) with sub-unit
    precision, plus deliberate lattice points, near-lattice points, and exact half-lattice
    ties, which are the cases most likely to break a rounding rule.
    """
    rng = random.Random(SEED)
    values: list[ShareUnits] = []
    # Exact lattice points and their immediate neighbourhoods.
    for k in range(-24, 25):
        base = k * GRID
        values.extend(
            ShareUnits(base + delta)
            for delta in (0, 1, -1, GRID // 2, -(GRID // 2), SHARE_SCALE, -SHARE_SCALE)
        )
    # Dense random fractional inventories across the excursion range.
    span = 120 * SHARE_SCALE
    while len(values) < SAMPLE_SIZE:
        values.append(ShareUnits(rng.randint(-span, span)))
    return values


INVENTORIES = inventories()


def test_the_corpus_is_large_and_actually_fractional() -> None:
    assert len(INVENTORIES) >= SAMPLE_SIZE
    assert sum(1 for i in INVENTORIES if i % GRID != 0) > SAMPLE_SIZE // 2
    assert any(i > 0 for i in INVENTORIES)
    assert any(i < 0 for i in INVENTORIES)
    assert any(i % GRID == 0 for i in INVENTORIES)
    assert any(i % GRID == GRID // 2 for i in INVENTORIES)


def test_the_corpus_is_deterministic() -> None:
    assert inventories() == INVENTORIES


@pytest.mark.parametrize("policy", list(GridPolicy))
def test_modular_fingerprint_holds_for_every_inventory(policy: GridPolicy) -> None:
    """Canonical §12.2, asserted as the congruence itself rather than a shortcut."""
    lot = LOTS[0]
    for inventory in INVENTORIES:
        plan = plan_grid(inventory, lot, policy)
        assert (plan.up_size + inventory) % GRID == 0, (
            f"up_size {plan.up_size} is not ≡ -I mod 5 for I={inventory} under {policy}"
        )
        assert (plan.down_size - inventory) % GRID == 0, (
            f"down_size {plan.down_size} is not ≡ +I mod 5 for I={inventory} under {policy}"
        )


@pytest.mark.parametrize("policy", list(GridPolicy))
def test_resulting_inventory_lands_exactly_on_the_lattice(policy: GridPolicy) -> None:
    lot = LOTS[0]
    for inventory in INVENTORIES:
        plan = plan_grid(inventory, lot, policy)
        assert (inventory + plan.up_size) % GRID == 0
        assert (inventory - plan.down_size) % GRID == 0
        assert inventory + plan.up_size == plan.up_target
        assert inventory - plan.down_size == plan.down_target


@pytest.mark.parametrize("policy", list(GridPolicy))
def test_sizes_are_always_strictly_positive(policy: GridPolicy) -> None:
    lot = LOTS[0]
    for inventory in INVENTORIES:
        plan = plan_grid(inventory, lot, policy)
        assert plan.up_size > 0
        assert plan.down_size > 0


def test_fingerprint_holds_for_every_base_lot_and_rounding_policy() -> None:
    """A smaller sweep across the full parameter cross-product."""
    subset = INVENTORIES[:4_000]
    for policy in GridPolicy:
        for lot in LOTS:
            for rounding in GridRounding:
                for inventory in subset:
                    plan = plan_grid(inventory, lot, policy, rounding)
                    assert (plan.up_size + inventory) % GRID == 0
                    assert (plan.down_size - inventory) % GRID == 0


# -- the point of the exercise --------------------------------------------------------------


def test_the_fingerprint_cannot_distinguish_the_two_o04_policies() -> None:
    """Both readings pass for every inventory, so a green fingerprint proves neither.

    This is why O04 has to be closed by replay evidence against reconstructed target-wallet
    size sequences, not by any test that can be written from the documents alone.
    """
    lot = LOTS[0]
    disagreements = 0
    for inventory in INVENTORIES:
        canonical = plan_grid(inventory, lot, GridPolicy.CANONICAL_OFFSET)
        observed = plan_grid(inventory, lot, GridPolicy.OBSERVED_ADJACENT)
        # Both satisfy the fingerprint...
        assert (canonical.down_size - inventory) % GRID == 0
        assert (observed.down_size - inventory) % GRID == 0
        # ...while frequently producing different orders.
        if canonical.down_target != observed.down_target:
            disagreements += 1
    assert disagreements > len(INVENTORIES) // 2, (
        "the two O04 policies should disagree on most inventories; if they now agree, "
        "one of them has been quietly changed"
    )


def test_the_known_worked_example_divergence_is_pinned() -> None:
    """Regression guard for the exact conflict recorded in O04."""
    inventory = parse_share("-28.63")
    lot = BaseLot.of(15)
    canonical = plan_grid(inventory, lot, GridPolicy.CANONICAL_OFFSET)
    observed = plan_grid(inventory, lot, GridPolicy.OBSERVED_ADJACENT)

    assert (canonical.up_target, canonical.up_size) == (parse_share("-15"), parse_share("13.63"))
    assert (observed.up_target, observed.up_size) == (parse_share("-15"), parse_share("13.63"))
    assert (canonical.down_target, canonical.down_size) == (
        parse_share("-45"),
        parse_share("16.37"),
    )
    assert (observed.down_target, observed.down_size) == (
        parse_share("-30"),
        parse_share("1.37"),
    )
    assert canonical.down_size != observed.down_size
