"""Fixed-point numeric kernel. Plane 2. Built in P1.

Contract: ``docs/ARCHITECTURE_SSOT.md`` section 6. Scales are frozen -- see
:mod:`maker5m.numeric.scales` and ``docs/OPEN_ITEMS.md`` O10.

Provides ``ShareUnits``, ``MoneyUnits``, and ``PriceUnits`` as distinct integer domain
types, exact parsing from decimal strings, the named cross-domain conversions, and the tick
grid. A venue value that is not exactly representable raises rather than rounding
(invariants I01, I03, I09, I20).

This package imports nothing else from the project.
"""

from maker5m.numeric.errors import (
    DomainError,
    InexactError,
    NotRepresentableError,
    NumericError,
    ParseError,
)
from maker5m.numeric.scales import (
    MONEY_SCALE,
    PAR_MONEY,
    PRICE_SCALE,
    SCALE_DECIMALS,
    SHARE_SCALE,
)
from maker5m.numeric.ticks import (
    SUPPORTED_TICK_SIZES,
    TICK_0_0001,
    TICK_0_001,
    TICK_0_0025,
    TICK_0_005,
    TICK_0_01,
    TICK_0_1,
    VENUE_ORDER_SIZE_DECIMALS,
    is_price_aligned,
    is_supported_tick,
    price_to_ticks,
    quantize_order_size,
    ticks_to_price,
)
from maker5m.numeric.units import (
    ZERO_MONEY,
    ZERO_SHARES,
    MoneyUnits,
    PriceUnits,
    Rounding,
    RoundingMode,
    ShareUnits,
    format_money,
    format_price,
    format_share,
    money_from_whole,
    notional_cost,
    parse_fixed_point,
    parse_money,
    parse_price,
    parse_share,
    price_from_whole,
    share_from_whole,
    shares_at_par,
    to_display_float,
)

__all__ = [
    "MONEY_SCALE",
    "PAR_MONEY",
    "PRICE_SCALE",
    "SCALE_DECIMALS",
    "SHARE_SCALE",
    "SUPPORTED_TICK_SIZES",
    "TICK_0_0001",
    "TICK_0_001",
    "TICK_0_0025",
    "TICK_0_005",
    "TICK_0_01",
    "TICK_0_1",
    "VENUE_ORDER_SIZE_DECIMALS",
    "ZERO_MONEY",
    "ZERO_SHARES",
    "DomainError",
    "InexactError",
    "MoneyUnits",
    "NotRepresentableError",
    "NumericError",
    "ParseError",
    "PriceUnits",
    "Rounding",
    "RoundingMode",
    "ShareUnits",
    "format_money",
    "format_price",
    "format_share",
    "is_price_aligned",
    "is_supported_tick",
    "money_from_whole",
    "notional_cost",
    "parse_fixed_point",
    "parse_money",
    "parse_price",
    "parse_share",
    "price_from_whole",
    "price_to_ticks",
    "quantize_order_size",
    "share_from_whole",
    "shares_at_par",
    "ticks_to_price",
    "to_display_float",
]
