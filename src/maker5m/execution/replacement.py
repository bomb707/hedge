"""Replacement sequencing, and the staleness rule that makes it safe.

The frozen sources establish *that* an order is replaced when it no longer matches
(Canonical §33), but nothing in them proves the network sequencing. So the policy is
explicit and labelled ``OPERATIONAL`` — an engineering choice, not a claim about the target
wallet.

Default: ``CANCEL_THEN_PLACE``. It avoids temporary duplicate exposure and preserves the
max-two-live-orders model of Canonical §23. ``PLACE_THEN_CANCEL`` is declared so P8/P13 can
measure whether overlap buys queue position, but it is not enabled: it would transiently
exceed the two-order model and double exposure.

**No fixed delay.** After the cancel acknowledgement the replacement is placed immediately.
The only waiting is real network state and the rate limiter when genuinely exhausted
(Canonical §20.1).

**Staleness.** Every pending replacement is bound to the decision generation that created it.
If a newer decision changes the desired order while the cancel is still outstanding, the
remembered target is stale — and placing it on cancel acknowledgement would put an obsolete
price into the book. So the acknowledgement reconciles against *current* desired state, not
against what was remembered.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Final

from maker5m.domain import Outcome, ParameterStatus
from maker5m.execution.prepare import PreparedOrder

__all__ = [
    "REPLACEMENT_POLICY_STATUS",
    "PendingReplacement",
    "ReplacementPolicy",
    "ReplacementTracker",
]


class ReplacementPolicy(Enum):
    """How a replacement is sequenced over the network."""

    CANCEL_THEN_PLACE = "CANCEL_THEN_PLACE"
    """Default. No duplicate exposure; stays inside the two-live-order model."""

    PLACE_THEN_CANCEL = "PLACE_THEN_CANCEL"
    """Declared for future measurement. Not enabled: it doubles exposure transiently."""


REPLACEMENT_POLICY_STATUS: Final = ParameterStatus.OPERATIONAL
"""An execution-sequencing choice. The sources do not establish one."""

DEFAULT_REPLACEMENT_POLICY: Final = ReplacementPolicy.CANCEL_THEN_PLACE


@dataclass(frozen=True, slots=True)
class PendingReplacement:
    """A cancel issued with the intention of placing ``target`` once it is acknowledged."""

    outcome: Outcome
    cancelling_client_order_id: str
    target: PreparedOrder
    decision_generation: int
    """The decision that created this intent. Used to detect staleness."""


@dataclass(slots=True)
class ReplacementTracker:
    """Tracks in-flight replacements and refuses to act on superseded ones."""

    policy: ReplacementPolicy = DEFAULT_REPLACEMENT_POLICY
    pending: dict[Outcome, PendingReplacement] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.policy is not ReplacementPolicy.CANCEL_THEN_PLACE:
            raise NotImplementedError(
                "PLACE_THEN_CANCEL is declared but not enabled: it transiently exceeds the "
                "two-live-order model and doubles exposure. P8/P13 must measure whether the "
                "queue benefit justifies that before it is switched on."
            )

    def record(self, replacement: PendingReplacement) -> None:
        self.pending[replacement.outcome] = replacement

    def take(self, outcome: Outcome, current_generation: int) -> PendingReplacement | None:
        """Consume the pending replacement for ``outcome`` if it is still current.

        Returns ``None`` when nothing is pending **or** when a newer decision has superseded
        it — in which case the caller reconciles against current desired state instead of
        placing an obsolete order.
        """
        replacement = self.pending.pop(outcome, None)
        if replacement is None:
            return None
        if replacement.decision_generation != current_generation:
            return None
        return replacement

    def discard(self, outcome: Outcome) -> None:
        self.pending.pop(outcome, None)

    def is_stale(self, outcome: Outcome, current_generation: int) -> bool:
        replacement = self.pending.get(outcome)
        return replacement is not None and replacement.decision_generation != current_generation
