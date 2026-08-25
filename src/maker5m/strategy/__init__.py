"""Strategy decision primitives. Plane 2, pure. Built in P3 and P4.

See ``docs/ARCHITECTURE_SSOT.md`` §4 and ``docs/INVARIANTS.md`` I04, I05, I12.

P3 delivers the deterministic pricing and sizing primitives:

* :mod:`~maker5m.strategy.upspace` — the ``BUY DOWN @ d == SELL UP @ 1-d`` identity;
* :mod:`~maker5m.strategy.centre` — the replaceable quote centre (O01), with the exact CLOB
  midpoint as the reference candidate;
* :mod:`~maker5m.strategy.quantization` — named tick tie policies (O13);
* :mod:`~maker5m.strategy.prices` — zero-synthetic-spread quote construction;
* :mod:`~maker5m.strategy.baselot` — the replaceable base-lot selector (O03);
* :mod:`~maker5m.strategy.grid` — the 5-share lattice and both unresolved O04 policies.

Pure: no clock reads, no I/O, no logging, no randomness, and no knowledge that a venue
exists. Replay runs this exact code (I20).

**Not here, and not to be added in P3:** the endgame regime, favourite targeting, the endgame
gate, ``band_hard``, order eligibility, and the assembled ``decide()`` — all P4. There is also
no ``gamma`` and no ``band_skew`` anywhere: normal-phase inventory skew is forbidden for this
replica (I12, Canonical §14, §29.3).

**Implemented is not proven.** Both O04 grid policies reproduce their documented worked
examples, but which one the target wallet used remains OPEN until replay evidence decides.
"""

from maker5m.strategy.baselot import (
    BASE_LOT_SELECTION_STATUS,
    SUPPORTED_BASE_LOTS,
    BaseLot,
    BaseLotSelector,
    ConfiguredBaseLotSelector,
)
from maker5m.strategy.centre import (
    CLOB_MID_STATUS,
    CentreResult,
    CentreSource,
    CentreUnavailable,
    ClobMidCentre,
    QuoteCentre,
    RawCentre,
)
from maker5m.strategy.errors import StrategyError, UnsupportedBaseLotError
from maker5m.strategy.grid import (
    GRID,
    GRID_POLICY_STATUS,
    REFERENCE_GRID_POLICY,
    REFERENCE_GRID_ROUNDING,
    GridPlan,
    GridPolicy,
    GridRounding,
    is_on_grid,
    plan_grid,
    round_to_grid,
)
from maker5m.strategy.prices import QuotePrices, build_quote_prices
from maker5m.strategy.quantization import (
    REFERENCE_TICK_ROUNDING,
    TICK_ROUNDING_STATUS,
    TickRounding,
    quantize_centre,
)
from maker5m.strategy.upspace import UpSpaceSide, complement, to_upspace, to_venue

__all__ = [
    "BASE_LOT_SELECTION_STATUS",
    "CLOB_MID_STATUS",
    "GRID",
    "GRID_POLICY_STATUS",
    "REFERENCE_GRID_POLICY",
    "REFERENCE_GRID_ROUNDING",
    "REFERENCE_TICK_ROUNDING",
    "SUPPORTED_BASE_LOTS",
    "TICK_ROUNDING_STATUS",
    "BaseLot",
    "BaseLotSelector",
    "CentreResult",
    "CentreSource",
    "CentreUnavailable",
    "ClobMidCentre",
    "ConfiguredBaseLotSelector",
    "GridPlan",
    "GridPolicy",
    "GridRounding",
    "QuoteCentre",
    "QuotePrices",
    "RawCentre",
    "StrategyError",
    "TickRounding",
    "UnsupportedBaseLotError",
    "UpSpaceSide",
    "build_quote_prices",
    "complement",
    "is_on_grid",
    "plan_grid",
    "quantize_centre",
    "round_to_grid",
    "to_upspace",
    "to_venue",
]
