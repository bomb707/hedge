"""Resolution and redemption. Built in P10.

See ``docs/INVARIANTS.md`` I15 and I16.

Authoritative winner determination with an explicit ambiguous branch, redemption of the
winning token, and realised-PnL reconciliation against the ledger.

Exit is settlement, never conventional selling or hedging. Blocked on O11 (authoritative
resolution source).
"""
