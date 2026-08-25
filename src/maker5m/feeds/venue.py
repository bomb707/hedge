"""Observed venue market rules — deliberately *not* strategy state.

The replica quotes on a ``0.01`` grid because the frozen strategy evidence says so
(Canonical §8.2, CONFIRMED). The venue separately publishes its own currently legal
order-price increment, on ``book`` messages and via ``tick_size_change``.

These are different concepts and are kept apart on purpose. If the venue announced ``0.001``
tomorrow, that would not mean the target wallet quoted on a tenth-cent grid — it would mean
the venue *permits* finer prices. Silently rewriting ``MarketDefinition.tick`` from a venue
announcement would change the strategy on the strength of transport metadata, which is exactly
the class of change Canonical §37 forbids.

So venue rules live here, in the feed/execution-facing layer, outside deterministic Plane 2
state. P7 needs them to check that a strategy intent is legal to submit. P6's obligation is
only to record them faithfully and never discard a ``tick_size_change``.
"""

from dataclasses import dataclass, field

from maker5m.market.timebase import TimestampNs
from maker5m.numeric.units import PriceUnits, ShareUnits

__all__ = ["VenueMarketRules", "VenueRulesTracker"]


@dataclass(frozen=True, slots=True)
class VenueMarketRules:
    """What the venue currently permits for one market."""

    min_tick_size: PriceUnits | None
    min_order_size: ShareUnits | None
    observed_at: TimestampNs | None = None
    source: str = ""


@dataclass(slots=True)
class VenueRulesTracker:
    """Latest observed rules per token, plus the full history of announced tick changes."""

    rules: VenueMarketRules = field(default_factory=lambda: VenueMarketRules(None, None, None, ""))
    tick_changes: list[tuple[TimestampNs, str, PriceUnits | None, PriceUnits]] = field(
        default_factory=list
    )

    def observe_rules(self, rules: VenueMarketRules) -> None:
        self.rules = rules

    def observe_tick_change(
        self,
        at: TimestampNs,
        asset_id: str,
        old_tick: PriceUnits | None,
        new_tick: PriceUnits,
    ) -> None:
        """Record an announced venue tick change. Never discarded, never applied to strategy."""
        self.tick_changes.append((at, asset_id, old_tick, new_tick))
        self.rules = VenueMarketRules(
            min_tick_size=new_tick,
            min_order_size=self.rules.min_order_size,
            observed_at=at,
            source="tick_size_change",
        )

    @property
    def tick_change_count(self) -> int:
        return len(self.tick_changes)
