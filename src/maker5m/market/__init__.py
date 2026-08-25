"""Authoritative market state, event contracts, and phase machine. Plane 1/2. Built in P2.

See ``docs/ARCHITECTURE_SSOT.md`` sections 3 and 5.

Will hold the five hot-path event types (``SpotTick``, ``BookUpdate``, ``OwnFill``,
``OrderStateEvent``, ``PhaseEvent``), the single-owner mutable ``MarketState``, the frozen
snapshot type published to Plane 3, and the phase machine
``PREARM -> QUOTE -> ENDGAME -> SETTLING -> DONE``. None of that exists yet.

Time is an event field, never an ambient clock read - that is what makes replay exact
(invariant I20).

The only member present today is :class:`Outcome`, which P1 needed. It lives here rather
than in ``accounting`` so the dependency direction of section 8 holds: ``accounting`` may
import ``market``, not the reverse.
"""

from maker5m.market.outcome import Outcome

__all__ = ["Outcome"]
