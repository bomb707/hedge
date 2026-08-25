"""Order execution and reconciliation. Plane 1, with a pure Plane 2 diff. Built in P7.

See ``docs/INVARIANTS.md`` I06-I10.

Live order table, post-only guard (local and venue-level), the minimal-action reconciler,
cancel/replace, token-bucket rate limiting, and queue tracking.

An unchanged valid order is KEPT so queue priority survives. There is no fixed requote
delay. An intentional taker fill is an execution bug, not an acceptable outcome.
"""
