"""Tick quantization of the quote centre — an explicitly OPEN strategy decision.

The frozen sources say ``round_tick(C)`` (Canonical §8.1, §32) and "Apply Exact Tick
Rounding" (Detailed §10), and give exactly one worked example:

```text
C_raw = 0.6274   ->   C_quote = 0.63
```

That single example rules out FLOOR/TRUNCATE. It says **nothing** about a tie. At
``tick = 0.01`` a raw centre of ``0.625`` can legitimately quote ``0.62`` or ``0.63``
depending on the rule, and which one is chosen changes the price level the bot rests at —
therefore its queue position, which is where this strategy's edge lives (Canonical §10.1).
That is a strategy question, not a formatting detail, so it is recorded as **O13** rather
than settled by whatever Python's ``round`` happens to do.

Nothing here uses the built-in ``round``: banker's rounding must never become strategy
behaviour by accident. Each tie rule is a named, deterministic policy, and the arithmetic is
integer throughout.

A structural note that constrains the default
--------------------------------------------
Canonical §32 rounds *both* sides from the raw centre::

    px_up   = round_to_tick(centre)
    px_down = round_to_tick(1.0 - centre)

At a tie that construction breaks the zero-spread property under ``HALF_UP`` and
``HALF_DOWN`` — for every one of the 100 tie points at ``tick = 0.01``, the two rounded
prices no longer complement each other and a one-tick synthetic spread appears, violating
I05. Under ``HALF_EVEN`` it holds at every tie, because the two integer parts always have
opposite parity and exactly one of them rounds up.

``HALF_EVEN`` is therefore the reference default here: it is the tie rule under which even a
literal reading of Canonical §32 preserves the CONFIRMED zero-spread property. That is a
consistency argument, **not** evidence about the target wallet, so the policy stays labelled
``OPEN``. Independently, :mod:`maker5m.strategy.prices` builds the DOWN price by
complementing the already-quantized centre, which makes zero spread exact under *every*
policy — so the default choice cannot silently become load-bearing.
"""

from enum import Enum
from typing import Final

from maker5m.domain import ParameterStatus
from maker5m.numeric.errors import DomainError
from maker5m.numeric.scales import PRICE_SCALE
from maker5m.numeric.units import PriceUnits
from maker5m.strategy.centre import RawCentre

__all__ = [
    "REFERENCE_TICK_ROUNDING",
    "TICK_ROUNDING_STATUS",
    "TickRounding",
    "quantize_centre",
]


class TickRounding(Enum):
    """Named tie rules for quantizing a raw centre onto the tick grid.

    All three round to the nearest tick and differ only at an exact half-tick. FLOOR is not
    offered: the one worked example in the sources (``0.6274 -> 0.63``) excludes it. CEILING
    is not formally excluded by that example, but is excluded by the sources' own wording
    ("round"); see O13 for what would settle it.
    """

    HALF_EVEN = "HALF_EVEN"
    """Tie goes to the even tick. The reference default — see the module docstring."""

    HALF_UP = "HALF_UP"
    """Tie goes to the higher tick."""

    HALF_DOWN = "HALF_DOWN"
    """Tie goes to the lower tick."""


REFERENCE_TICK_ROUNDING: Final = TickRounding.HALF_EVEN
"""The policy used for early replay. A reference choice, not a proven one."""

TICK_ROUNDING_STATUS: Final = ParameterStatus.OPEN
"""O13. Must not be presented as CONFIRMED until evidence closes it."""


def quantize_centre(
    raw: RawCentre, tick: PriceUnits, rounding: TickRounding = REFERENCE_TICK_ROUNDING
) -> PriceUnits:
    """Snap an exact rational centre onto the tick grid.

    Integer arithmetic end to end: the raw centre is a rational ``numerator/denominator`` in
    ``PriceUnits``, so the tick index is ``numerator / (denominator * tick)`` and the tie test
    is a comparison of ``2 * remainder`` against the divisor. No float, no ``Decimal``, and no
    built-in ``round``.
    """
    if tick <= 0:
        raise DomainError(f"tick must be positive, got {tick}")
    if PRICE_SCALE % tick:
        raise DomainError(f"tick {tick} does not divide the price scale exactly")

    divisor = raw.denominator * tick
    index, remainder = divmod(raw.numerator, divisor)
    doubled = 2 * remainder

    if doubled == divisor:
        # Exact half tick: the tie rule decides, and the rule is a named policy (O13).
        round_up = rounding is TickRounding.HALF_UP or (
            rounding is TickRounding.HALF_EVEN and index % 2 == 1
        )
    else:
        round_up = doubled > divisor

    if round_up:
        index += 1

    price = index * tick
    if not 0 <= price <= PRICE_SCALE:
        raise DomainError(f"quantized centre {price} fell outside [0, 1]")
    return PriceUnits(price)
