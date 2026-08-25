"""Deterministic replay. Offline. Built in P5.

See ``docs/ARCHITECTURE_SSOT.md`` section 7 and invariant I20.

Journal format, recorder, replay harness, and the parameter-sweep runner used to close open
items. Replay drives the same Plane 2 code object as production; a second implementation of
any strategy rule here, or a branch keyed on "am I replaying", is a defect.
"""
