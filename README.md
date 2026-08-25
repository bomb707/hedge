# Polymarket BTC 5-Minute Maker Bot

Production build of the post-only, zero-spread, 5-share-lattice maker strategy for
Polymarket `btc-updown-5m-*` markets.

> **LIVE TRADING: DISABLED.** No execution path, feeds, credentials, or signing exist in
> this repository. See [`docs/STATUS.md`](docs/STATUS.md).

## Start here

| | |
|---|---|
| What is where, and which document wins | [`docs/INDEX.md`](docs/INDEX.md) |
| Where the project is right now | [`docs/STATUS.md`](docs/STATUS.md) |
| Rules that may not be broken | [`docs/INVARIANTS.md`](docs/INVARIANTS.md) |
| Structure, planes, numeric contract | [`docs/ARCHITECTURE_SSOT.md`](docs/ARCHITECTURE_SSOT.md) |
| Phases and their acceptance gates | [`docs/DEVELOPMENT_PLAN.md`](docs/DEVELOPMENT_PLAN.md) |
| What is genuinely unresolved | [`docs/OPEN_ITEMS.md`](docs/OPEN_ITEMS.md) |

The two documents under [`docs/strategy/`](docs/strategy/) are **frozen inputs**. They are
preserved byte-for-byte, pinned by checksum, and must never be edited. The Canonical
Strategy Spec is the strategy source of truth and wins every conflict.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy
.venv/bin/pytest
```

Python 3.12+. No runtime dependencies — each is added by the phase that genuinely needs it.

## The one rule

**Replication correctness first. Latency and precision second. Optimization only after both
are proven.** Do not reinterpret, simplify, or improve the strategy while implementing it,
and do not resolve an `OPEN` parameter by assumption.
