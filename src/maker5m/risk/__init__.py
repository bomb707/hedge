"""Risk, health, and recovery. Plane 1, plus the pure hard band in Plane 2. Built in P9.

See ``docs/INVARIANTS.md`` I17 and Canonical section 28.1.

The one-sided ``band_hard`` inventory limit, feed staleness, sequence gaps, clock drift,
order-state uncertainty, ledger consistency, and the kill switch.

Every control here stops new risk; none of them changes the economic strategy. No halt path
may ever flatten, sell, or hedge inventory (invariant I15).
"""
