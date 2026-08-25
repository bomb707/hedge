"""Observability. Plane 3. Built in P8 and P11.

See ``docs/INVARIANTS.md`` I19 and Canonical sections 25 and 26.

Decision and fill logs, latency and queue metrics, PnL metrics, and the replay recorder.

Fed by a bounded non-blocking queue. On overflow it drops and counts drops rather than
applying back-pressure: a dropped record is an observability incident, a blocked hot loop
is a trading incident.
"""
