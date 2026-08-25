"""Market data adapters. Plane 1. Built in P6. First network access in the project.

See ``docs/ARCHITECTURE_SSOT.md`` section 4.

Polymarket CLOB websocket book feed with sequence tracking and gap detection, Binance spot
websocket, and clock synchronisation. REST is for recovery and reconciliation only, never
the main live path.

External BTC spot must be able to wake the decision path on its own, independently of CLOB
updates (invariant I11).
"""
