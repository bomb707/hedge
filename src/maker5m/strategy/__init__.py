"""Strategy decision engine. Plane 2, pure. Built in P3 and P4.

See ``docs/ARCHITECTURE_SSOT.md`` section 4 and ``docs/INVARIANTS.md`` I04, I05, I12-I14.

``decide(state) -> DesiredOrders`` is the single place the strategy exists. Contains the
Up-space translation, zero-spread quoting, the 5-share grid sizer, the base-lot selector,
the quote-centre model, and the endgame controller.

Pure: no clock reads, no I/O, no logging, no knowledge that a venue exists. Replay runs
this exact code.

Open items that must stay configurable here: O01 quote centre, O02 sigma, O03 base lot,
O04 grid-target policy (the frozen sources conflict - both readings must be selectable),
O05 endgame tilt, O06 endgame gate.
"""
