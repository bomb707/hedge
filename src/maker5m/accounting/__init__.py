"""Dual-token ledger and settlement economics. Plane 2, pure. Built in P1.

See ``docs/INVARIANTS.md`` I01-I03.

Maintains ``n_up``, ``n_down``, ``cost_up``, ``cost_down``, ``fees``, and the deliberately
separate estimated and realised rebate figures, exposing ``total_cost``, ``net_inventory``,
``pnl_if_up``, and ``pnl_if_down`` as live first-class state. The Term1/Term2 view is an
exact rational decomposition kept off the hot path.

Holding more of the eventual winner does not imply profit. The only valid profitability test
is winner shares minus the total cost of BOTH outcome tokens, after fees and rebates.
"""

from maker5m.accounting.decomposition import (
    TermDecomposition,
    average_price,
    decompose,
)
from maker5m.accounting.ledger import Fill, LedgerState, RebateMode
from maker5m.accounting.settlement import SettlementResult, settle

__all__ = [
    "Fill",
    "LedgerState",
    "RebateMode",
    "SettlementResult",
    "TermDecomposition",
    "average_price",
    "decompose",
    "settle",
]
