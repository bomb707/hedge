"""Dual-token ledger and settlement economics. Plane 2, pure. Built in P1 and P2.

See ``docs/INVARIANTS.md`` I01-I03.

Maintains ``n_up``, ``n_down``, ``cost_up``, ``cost_down``, ``fees``, ``estimated_rebate``,
``total_cost``, ``pnl_if_up``, and ``pnl_if_down`` as live first-class state, plus the
Term1/Term2 analytic view which must reconcile exactly with the settlement accounting.

Holding more of the eventual winner does not imply profit. The only valid profitability
test is winner shares minus the total cost of BOTH outcome tokens, after fees and rebates.
"""
