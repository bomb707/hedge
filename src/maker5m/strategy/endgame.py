"""The ENDGAME regime: favourite direction, target inventory, and the binding gate.

Canonical §15, Detailed §24-26. ENDGAME is explicit and distinct from QUOTE (I13): the
correct favourite residual does **not** emerge automatically from symmetric quoting.

Two things this module deliberately does not do:

* it does not resize or reprice anything. ENDGAME modifies **order eligibility only**
  (A5, Detailed §29). It is never "market buy 30 favourite shares";
* it does not gate on economics. Canonical §17 says the endgame engine should *monitor*
  settlement edges, and Detailed §28 says the dual-token cost must always be *visible* —
  both descriptive. Canonical §32's decision function computes the settlement edges and
  returns them **without** using them in eligibility. Neither source states a threshold rule,
  so none is invented here; the economics are mandatory telemetry instead.

Favourite direction is decided from the **raw** centre
-----------------------------------------------------
Canonical §32 computes the raw centre, derives the quote prices from it, and only then
evaluates ``favourite_up = centre > 0.5``. The comparison is against the unrounded value.

That distinction is load-bearing. With ``tick = 0.01`` a raw centre of ``0.504`` quantizes to
``0.50``; comparing the quantized price would call the favourite DOWN, while the strategy's
own value says UP. Deciding a 30-share terminal residual from a rounding artefact would be a
real behavioural change, so the comparison is exact rational arithmetic on the raw centre.

At exactly ``0.50`` the favourite is DOWN, because Canonical §32 writes ``centre > 0.5``
(``ARCHITECTURE_SSOT`` §10, A1). That tie is economically arbitrary and worth logging.
"""

from dataclasses import dataclass

from maker5m.domain import Outcome
from maker5m.numeric.scales import PRICE_SCALE
from maker5m.numeric.units import ShareUnits
from maker5m.strategy.centre import RawCentre

__all__ = ["EndgameGate", "endgame_target", "evaluate_endgame", "favourite_from_centre"]


def favourite_from_centre(raw: RawCentre) -> Outcome:
    """Which outcome the raw centre currently favours.

    Exact: ``numerator / denominator > PRICE_SCALE / 2`` is tested as
    ``2 * numerator > denominator * PRICE_SCALE``, so there is no division and no float.
    Equality yields DOWN (A1).
    """
    return Outcome.UP if 2 * raw.numerator > raw.denominator * PRICE_SCALE else Outcome.DOWN


def endgame_target(favourite: Outcome, tilt: ShareUnits) -> ShareUnits:
    """The inventory the endgame aims to hold: ``+tilt`` for UP, ``-tilt`` for DOWN."""
    return tilt if favourite is Outcome.UP else ShareUnits(-tilt)


@dataclass(frozen=True, slots=True)
class EndgameGate:
    """The endgame's contribution to eligibility, plus the numbers behind it."""

    favourite: Outcome
    target: ShareUnits
    distance: ShareUnits
    """``I - target_I``. Positive means inventory sits above the target."""

    up_allowed: bool
    down_allowed: bool


def evaluate_endgame(
    inventory: ShareUnits, favourite: Outcome, tilt: ShareUnits, band: ShareUnits
) -> EndgameGate:
    """Apply Canonical §15.2's gate.

    ```text
    distance     = I - target_I
    up_allowed   = distance < +band
    down_allowed = distance > -band
    ```

    Both inequalities are **strict**, so a side is blocked exactly at its boundary. The
    algebra is the source of truth, not the prose: for ``target = +30`` and ``band = 5`` the
    UP side is live while ``I < 35`` and the DOWN side while ``I > 25``.
    """
    target = endgame_target(favourite, tilt)
    distance = ShareUnits(inventory - target)
    return EndgameGate(
        favourite=favourite,
        target=target,
        distance=distance,
        up_allowed=distance < band,
        down_allowed=distance > -band,
    )
