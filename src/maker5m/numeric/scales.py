"""Frozen fixed-point scales.

These constants close O10 for the numeric kernel. **Changing them invalidates every
recorded replay journal and every stored ledger**, so they are frozen from P1 onward. See
``docs/OPEN_ITEMS.md`` O10 for the evidence and the remaining P6 validation requirement.

Evidence (Polymarket's official CLOB implementation):

* ``COLLATERAL_TOKEN_DECIMALS = 6`` and ``CONDITIONAL_TOKEN_DECIMALS = 6`` -- collateral and
  conditional-token amounts are atomic integers with six decimal places;
* supported tick sizes are ``0.1``, ``0.01``, ``0.001``, ``0.0001``;
* the order builder quantises submitted order *size* to two decimal places.

The last point is deliberately **not** what these scales encode:

```text
ORDER INPUT QUANTIZATION  !=  AUTHORITATIVE LEDGER PRECISION
```

An order may be *submitted* with size rounded to two decimals, but the resulting position
and collateral movements are settled in 6-decimal atomic units, and the ledger is
authoritative over the venue's atomic amounts -- not over what we asked for. Order-size
quantisation lives in :mod:`maker5m.numeric.ticks` and is never applied to ledger inputs.

Six decimals also represents every documented tick size exactly (``0.0001`` is ``100``
price units) with four decimal digits of headroom.
"""

from typing import Final

__all__ = [
    "MONEY_SCALE",
    "PAR_MONEY",
    "PRICE_SCALE",
    "SCALE_DECIMALS",
    "SHARE_SCALE",
]

SCALE_DECIMALS: Final[int] = 6
"""Decimal places held exactly by every domain type. Matches the venue's atomic units."""

SHARE_SCALE: Final[int] = 10**SCALE_DECIMALS
"""``ShareUnits`` per whole outcome share. ``1 share == 1_000_000 ShareUnits``."""

MONEY_SCALE: Final[int] = 10**SCALE_DECIMALS
"""``MoneyUnits`` per USDC. ``$1.00 == 1_000_000 MoneyUnits``."""

PRICE_SCALE: Final[int] = 10**SCALE_DECIMALS
"""``PriceUnits`` per unit probability. ``probability 1.0 == 1_000_000 PriceUnits``."""

PAR_MONEY: Final[int] = MONEY_SCALE
"""Settlement value of one winning share, in ``MoneyUnits``. One share pays exactly $1."""
