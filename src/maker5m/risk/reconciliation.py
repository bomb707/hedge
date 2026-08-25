"""Establishing whether our state matches an authoritative source.

Canonical §28.1 names "position state inconsistent" and "cost ledger inconsistent" as kill
switches. Detecting either requires comparing against something authoritative, and the rule
that governs all of this is Detailed §38's closing line: *accuracy is more important than
continuing to trade*.

So nothing here repairs anything. A mismatch produces a typed finding and a halt; it never
overwrites the ledger with whatever the venue currently says. The venue being different is not
evidence that the venue is right — a missed fill and a duplicated fill look identical from the
balance alone, and they have opposite corrections.

Everything here is pure. The authoritative snapshots arrive as arguments, because obtaining
them requires credentials that do not exist before P14.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum

from maker5m.accounting.ledger import LedgerState
from maker5m.domain import Outcome
from maker5m.execution.live_orders import LiveOrder, LiveOrderTable, OrderLifecycle
from maker5m.numeric.units import MoneyUnits, PriceUnits, ShareUnits

__all__ = [
    "CostLedgerCheck",
    "OrderFinding",
    "OrderReconciliation",
    "PositionCheck",
    "VenueExecution",
    "VenueOpenOrder",
    "VenuePosition",
    "check_cost_ledger",
    "check_position",
    "reconcile_orders",
]


@dataclass(frozen=True, slots=True)
class VenuePosition:
    """An authoritative balance snapshot, per token id, in P1 fixed-point units.

    Token ids, not outcome labels: the venue knows about ERC-1155 positions, and mapping one to
    UP or DOWN is our job. Comparing a rounded decimal display would make a mismatch of a few
    share-units invisible, which is exactly the size of mismatch a missed fill produces.
    """

    up_token_id: str
    down_token_id: str
    up_shares: ShareUnits
    down_shares: ShareUnits


@dataclass(frozen=True, slots=True)
class VenueExecution:
    """One authoritative execution from venue trade history."""

    execution_id: str
    outcome: Outcome
    shares: ShareUnits
    cost: MoneyUnits


@dataclass(frozen=True, slots=True)
class VenueOpenOrder:
    """One order the venue currently reports as working."""

    client_order_id: str
    venue_order_id: str
    outcome: Outcome
    price: PriceUnits
    remaining_size: ShareUnits


@dataclass(frozen=True, slots=True)
class PositionCheck:
    """Exact comparison, in share units. No rounding, no tolerance."""

    matches: bool
    up_local: ShareUnits
    up_venue: ShareUnits
    down_local: ShareUnits
    down_venue: ShareUnits

    @property
    def up_difference(self) -> int:
        return int(self.up_venue) - int(self.up_local)

    @property
    def down_difference(self) -> int:
        return int(self.down_venue) - int(self.down_local)

    def summary(self) -> dict[str, object]:
        return {
            "matches": self.matches,
            "up_local": int(self.up_local),
            "up_venue": int(self.up_venue),
            "down_local": int(self.down_local),
            "down_venue": int(self.down_venue),
            "up_difference": self.up_difference,
            "down_difference": self.down_difference,
        }


def check_position(
    ledger: LedgerState, snapshot: VenuePosition, *, up_token_id: str, down_token_id: str
) -> PositionCheck:
    """Compare internal balances against an authoritative snapshot, exactly.

    Token identity is verified rather than assumed: comparing our UP balance against some other
    market's position would be worse than not checking at all.
    """
    if snapshot.up_token_id != up_token_id or snapshot.down_token_id != down_token_id:
        raise ValueError(
            f"position snapshot is for tokens {snapshot.up_token_id}/{snapshot.down_token_id}, "
            f"expected {up_token_id}/{down_token_id}"
        )
    matches = ledger.n_up == snapshot.up_shares and ledger.n_down == snapshot.down_shares
    return PositionCheck(
        matches=matches,
        up_local=ledger.n_up,
        up_venue=snapshot.up_shares,
        down_local=ledger.n_down,
        down_venue=snapshot.down_shares,
    )


@dataclass(frozen=True, slots=True)
class CostLedgerCheck:
    """Whether our cost basis matches the sum of authoritative executions."""

    matches: bool
    up_cost_local: MoneyUnits
    up_cost_venue: MoneyUnits
    down_cost_local: MoneyUnits
    down_cost_venue: MoneyUnits
    missing_execution_ids: tuple[str, ...] = ()
    duplicate_execution_ids: tuple[str, ...] = ()

    def summary(self) -> dict[str, object]:
        return {
            "matches": self.matches,
            "up_cost_local": int(self.up_cost_local),
            "up_cost_venue": int(self.up_cost_venue),
            "down_cost_local": int(self.down_cost_local),
            "down_cost_venue": int(self.down_cost_venue),
            "missing_execution_ids": list(self.missing_execution_ids),
            "duplicate_execution_ids": list(self.duplicate_execution_ids),
        }


def check_cost_ledger(
    ledger: LedgerState,
    executions: Mapping[str, VenueExecution],
    applied_execution_ids: frozenset[str],
) -> CostLedgerCheck:
    """Rebuild cost from authoritative execution history and compare.

    Cost basis is reconstructed by **summing executions**, never as quantity times a displayed
    price. Current balance cannot prove what was paid for it: the same 100 shares can have been
    bought at one price or at twenty, and a reconstruction from the current mid would produce a
    confident, wrong, and unfalsifiable number.

    Missing and duplicate execution ids are reported separately, because they are the two
    diagnoses a mismatch can have and they call for opposite corrections.
    """
    up_cost = 0
    down_cost = 0
    for execution in executions.values():
        if execution.outcome is Outcome.UP:
            up_cost += int(execution.cost)
        else:
            down_cost += int(execution.cost)

    missing = tuple(sorted(set(executions) - applied_execution_ids))
    duplicate = tuple(sorted(applied_execution_ids - set(executions)))
    matches = (
        int(ledger.cost_up) == up_cost
        and int(ledger.cost_down) == down_cost
        and not missing
        and not duplicate
    )
    return CostLedgerCheck(
        matches=matches,
        up_cost_local=ledger.cost_up,
        up_cost_venue=MoneyUnits(up_cost),
        down_cost_local=ledger.cost_down,
        down_cost_venue=MoneyUnits(down_cost),
        missing_execution_ids=missing,
        duplicate_execution_ids=duplicate,
    )


class OrderFinding(Enum):
    """The outcome of comparing one order id across the two views."""

    AGREED = "AGREED"
    """Both sides believe it is working, with matching price and remaining size."""

    FIELDS_DIFFER = "FIELDS_DIFFER"
    """Both believe it is working, but not about the same order."""

    LOCAL_ONLY = "LOCAL_ONLY"
    """We think it rests; the venue does not report it."""

    VENUE_ONLY = "VENUE_ONLY"
    """The venue reports an order we have no record of."""

    LOCAL_UNKNOWN = "LOCAL_UNKNOWN"
    """Our record is UNKNOWN. The venue's answer is the only one that counts."""


@dataclass(frozen=True, slots=True)
class OrderReconciliation:
    """The comparison between our order table and the venue's open orders."""

    findings: Mapping[str, OrderFinding] = field(default_factory=dict)

    @property
    def consistent(self) -> bool:
        """Whether every id agreed. Anything else must be resolved before placing."""
        return all(finding is OrderFinding.AGREED for finding in self.findings.values())

    def with_finding(self, finding: OrderFinding) -> tuple[str, ...]:
        return tuple(sorted(key for key, value in self.findings.items() if value is finding))

    def summary(self) -> dict[str, object]:
        return {
            "consistent": self.consistent,
            "findings": {key: value.value for key, value in sorted(self.findings.items())},
        }


def _occupying(order: LiveOrder) -> bool:
    return order.occupies_side


def reconcile_orders(
    local: LiveOrderTable, venue_open: Mapping[str, VenueOpenOrder]
) -> OrderReconciliation:
    """Compare every order either side believes is working.

    Terminal local orders are skipped: the venue not reporting a filled order is agreement, not
    a discrepancy. Everything else produces a typed finding, and *no* finding is repaired here.
    A ``LOCAL_ONLY`` order might have been cancelled, might have filled, or might be a report
    we have not received yet, and those have different corrections — so this reports the shape
    of the disagreement and leaves resolving it to whoever can establish which happened.
    """
    findings: dict[str, OrderFinding] = {}

    for order in local:
        if order.status is OrderLifecycle.UNKNOWN:
            findings[order.client_order_id] = OrderFinding.LOCAL_UNKNOWN
            continue
        if not _occupying(order):
            continue
        venue = venue_open.get(order.client_order_id)
        if venue is None:
            findings[order.client_order_id] = OrderFinding.LOCAL_ONLY
        elif venue.price != order.price or venue.remaining_size != order.remaining_size:
            findings[order.client_order_id] = OrderFinding.FIELDS_DIFFER
        else:
            findings[order.client_order_id] = OrderFinding.AGREED

    for client_order_id in venue_open:
        if client_order_id not in findings and local.get(client_order_id) is None:
            findings[client_order_id] = OrderFinding.VENUE_ONLY

    return OrderReconciliation(findings=findings)
