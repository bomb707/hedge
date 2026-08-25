"""Fixed-point numeric kernel. Plane 2. Built in P1.

Target contract: ``docs/ARCHITECTURE_SSOT.md`` section 6.

Provides ``PriceTicks``, ``ShareUnits``, and ``MoneyUnits`` as distinct integer domain
types that must not be implicitly interchangeable, plus the scaling policy, the single
explicit rounding boundary, and the exactness guard (a venue value that is not exactly
representable is a hard error, never a silent round).

Binary floating point is not acceptable here: ``0.01`` is not representable, accumulation
over partial fills drifts, and order-reconciliation equality would stop being exact
(invariants I01, I03, I09, I20).

Blocked on O10 (venue precision) before the scales may be frozen.

This package imports nothing else from the project.
"""
