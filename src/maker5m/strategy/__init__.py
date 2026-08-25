"""Strategy decision primitives. Plane 2, pure. Built in P3 and P4.

See ``docs/ARCHITECTURE_SSOT.md`` §4 and ``docs/INVARIANTS.md`` I04, I05, I12.

P4 assembles them into ``StrategyEngine.decide(state) -> DecisionResult``:
:mod:`~maker5m.strategy.config`, :mod:`~maker5m.strategy.endgame`,
:mod:`~maker5m.strategy.eligibility`, :mod:`~maker5m.strategy.decision`, and
:mod:`~maker5m.strategy.engine`.

P3 delivers the deterministic pricing and sizing primitives it composes:

* :mod:`~maker5m.strategy.upspace` — the ``BUY DOWN @ d == SELL UP @ 1-d`` identity;
* :mod:`~maker5m.strategy.centre` — the replaceable quote centre (O01), with the exact CLOB
  midpoint as the reference candidate;
* :mod:`~maker5m.strategy.quantization` — named tick tie policies (O13);
* :mod:`~maker5m.strategy.prices` — zero-synthetic-spread quote construction;
* :mod:`~maker5m.strategy.baselot` — the replaceable base-lot selector (O03);
* :mod:`~maker5m.strategy.grid` — the 5-share lattice and both unresolved O04 policies.

Pure: no clock reads, no I/O, no logging, no randomness, and no knowledge that a venue
exists. Replay runs this exact code (I20).

**Not here, and not to be added:** anything about a venue. No submission, no post-only
validation, no reconciliation, no cancel/replace, no queue position, no signing — execution
begins at P7. ``decide()`` produces intent and the record explaining it, nothing more.

There is no ``gamma`` and no ``band_skew`` anywhere, and no flattening path: normal-phase
inventory skew is forbidden for this replica and inventory is never reduced merely to reach
zero (I12, I15, Canonical §14, §29.3, §29.4).

**Implemented is not proven.** Both O04 grid policies reproduce their documented worked
examples; ``endgame_tilt = 30`` and ``endgame_band = 5`` are FITTED; the centre source, the
base-lot rule, and the tick tie rule are all OPEN. A green suite says the code does what this
phase specified. It says nothing about whether that matches the target wallet.
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
from maker5m.strategy.config import (
    BAND_HARD_STATUS,
    DEFAULT_BAND_HARD,
    DEFAULT_ENDGAME_BAND,
    DEFAULT_ENDGAME_TILT,
    ENDGAME_BAND_STATUS,
    ENDGAME_TILT_STATUS,
    StrategyConfig,
    default_config,
)
from maker5m.strategy.decision import (
    DecisionEconomics,
    DecisionResult,
    DecisionTelemetry,
    DesiredOrder,
    DesiredOrders,
    EndgameTelemetry,
    economics_of,
)
from maker5m.strategy.eligibility import (
    EligibilityReason,
    EligibilityResult,
    evaluate_eligibility,
)
from maker5m.strategy.endgame import (
    EndgameGate,
    endgame_target,
    evaluate_endgame,
    favourite_from_centre,
)
from maker5m.strategy.engine import QUOTING_PHASES, StrategyEngine
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
    "BAND_HARD_STATUS",
    "BASE_LOT_SELECTION_STATUS",
    "CLOB_MID_STATUS",
    "DEFAULT_BAND_HARD",
    "DEFAULT_ENDGAME_BAND",
    "DEFAULT_ENDGAME_TILT",
    "ENDGAME_BAND_STATUS",
    "ENDGAME_TILT_STATUS",
    "GRID",
    "GRID_POLICY_STATUS",
    "QUOTING_PHASES",
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
    "DecisionEconomics",
    "DecisionResult",
    "DecisionTelemetry",
    "DesiredOrder",
    "DesiredOrders",
    "EligibilityReason",
    "EligibilityResult",
    "EndgameGate",
    "EndgameTelemetry",
    "GridPlan",
    "GridPolicy",
    "GridRounding",
    "QuoteCentre",
    "QuotePrices",
    "RawCentre",
    "StrategyConfig",
    "StrategyEngine",
    "StrategyError",
    "TickRounding",
    "UnsupportedBaseLotError",
    "UpSpaceSide",
    "build_quote_prices",
    "complement",
    "default_config",
    "economics_of",
    "endgame_target",
    "evaluate_eligibility",
    "evaluate_endgame",
    "favourite_from_centre",
    "is_on_grid",
    "plan_grid",
    "quantize_centre",
    "round_to_grid",
    "to_upspace",
    "to_venue",
]
