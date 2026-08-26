"""Bounded, non-blocking publication of the things that are not decisions.

Decisions ride P8's observation buffer, which already exists and is already measured. Fills and
risk records do not — a fill happens when the venue says so and a risk record when a signal
arrives, neither of which is a decision cycle — so each gets its own channel with exactly the
same properties: a `deque` with a maximum length, an append that cannot wait, and a drop count
that is arithmetic rather than a check on the hot path.

Why not one channel for everything: the three streams have different orders that matter
independently. Decisions are ordered by ingress ordinal, risk records by `risk_sequence`, and a
fill belongs to the ledger transition it surrounds. Interleaving them into one sequence would
make the storage order the only surviving order, and storage order is not causality.

Nothing here serializes, writes, hashes, or locks. The consumer does all of that.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Final, NamedTuple

from maker5m.accounting.ledger import Fill, LedgerState
from maker5m.market.timebase import TimestampNs
from maker5m.numeric.units import ShareUnits
from maker5m.persistence.records import Liquidity
from maker5m.persistence.schema import FillProvenance

__all__ = [
    "DEFAULT_FILL_CAPACITY",
    "DEFAULT_RISK_CAPACITY",
    "BoundedChannel",
    "FillCapture",
]

DEFAULT_FILL_CAPACITY: Final[int] = 8_192
"""Fills are rare next to decisions — the busiest measured market saw 1,962 trades across the
whole book, of which ours would be a small fraction — so this is generous by design."""

DEFAULT_RISK_CAPACITY: Final[int] = 320_000
"""Risk records are not rare: a real market produced 173,460 of them, roughly one per decision.

Sized like the observation buffer for the same reason. A bounded channel that overflows loses
the *oldest* records, which for risk means losing the prefix — and a risk trace missing its
prefix cannot be verified at all, since the contract is that it starts at zero. So the bound is
set where overflow does not happen in practice, and when it does the loss is counted and the
market is not complete."""


class FillCapture(NamedTuple):
    """One fill and the two ledger states that surrounded it.

    ``before`` and ``after`` are the authoritative states either side of the **single** real
    ``apply_fill``. Nothing downstream re-applies the fill or subtracts it back out: a
    before-state derived by reversing the arithmetic agrees with the ledger by construction and
    could therefore never disagree with it, which is the opposite of evidence.

    This is a *canonical* fill — an actual ledger transition, Canonical §25. It is deliberately
    not the same thing as P8's shadow queue fill, which models a queue slot closing and moves no
    money. Conflating the two would put modelled economics into the durable record.
    """

    fill: Fill
    before: LedgerState
    after: LedgerState
    event_id: str
    ingress_ordinal: int
    timestamp: TimestampNs
    token_id: str
    liquidity: Liquidity
    provenance: FillProvenance
    client_order_id: str | None = None
    venue_order_id: str | None = None
    queue_ahead_before: ShareUnits | None = None
    queue_confidence: str | None = None
    book: Any = None
    spot: Any = None


@dataclass(slots=True)
class BoundedChannel:
    """A drop-oldest ring with exact drop accounting. The producer never waits.

    The same shape as P8's ``ObservationBuffer``, and for the same reasons: ``deque.append`` is
    atomic under CPython and takes no lock, so a Plane-1 caller cannot be blocked by a Plane-3
    consumer holding one. Overflow drops the oldest and the count falls out of arithmetic rather
    than a length check on every append.
    """

    capacity: int
    records: deque[Any] = field(default_factory=deque, repr=False)
    accepted: int = 0
    drained: int = 0

    def __post_init__(self) -> None:
        if self.capacity < 1:
            raise ValueError(f"capacity must be >= 1, got {self.capacity}")
        if self.records.maxlen != self.capacity:
            self.records = deque(self.records, maxlen=self.capacity)

    def publish(self, record: Any) -> None:
        """Record one item. Never blocks, never raises, never allocates a container."""
        self.accepted += 1
        self.records.append(record)

    @property
    def dropped(self) -> int:
        return self.accepted - self.drained - len(self.records)

    def __len__(self) -> int:
        return len(self.records)

    def __iter__(self) -> Iterator[Any]:
        return iter(self.records)
