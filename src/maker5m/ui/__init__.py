"""Operator interface. Plane 3. Built in P12.

Read-only views over published immutable snapshots, plus a narrow control channel that
enqueues control events into the ordered stream rather than mutating trading state.

Holds no lock reachable from Plane 1 or 2. Killing the UI must not stop trading
(invariant I19).
"""
