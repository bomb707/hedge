"""Static guard for Plane 2 purity (``docs/ARCHITECTURE_SSOT.md`` section 2.1, invariant I20).

Strategy and accounting code must be a pure function of ``(state, event)``. Two properties
make deterministic replay possible, and both are checkable statically:

* no ambient clock reads - time arrives as a field on the event;
* no I/O or networking - Plane 2 does not know a venue exists.

This is enforced by import analysis rather than by convention, because a single stray
``time.time()`` inside ``decide()`` silently destroys replay determinism and would not fail
any behavioural test.

The guard is permanent. It becomes load-bearing at P2 and P3; running it against the empty
P0 skeleton simply means it starts green and stays green.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "maker5m"

PLANE2_PACKAGES = ["numeric", "market", "strategy", "accounting"]

FORBIDDEN_ROOTS = frozenset(
    {
        # ambient time
        "time",
        "datetime",
        "calendar",
        # I/O and networking
        "socket",
        "ssl",
        "asyncio",
        "selectors",
        "http",
        "urllib",
        "requests",
        "httpx",
        "aiohttp",
        "websockets",
        "websocket",
        # persistence
        "sqlite3",
        "logging",
        # nondeterminism
        "random",
        "secrets",
        "uuid",
    }
)


def _python_files() -> list[Path]:
    return sorted(p for pkg in PLANE2_PACKAGES for p in (SRC / pkg).rglob("*.py"))


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".", 1)[0])
    return roots


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: str(p.name))
def test_plane2_module_imports_nothing_impure(path: Path) -> None:
    offenders = sorted(_imported_roots(path) & FORBIDDEN_ROOTS)
    assert not offenders, (
        f"{path.relative_to(SRC.parent.parent)} imports {offenders}. Plane 2 must stay pure "
        f"and deterministic: time is an event field, and I/O belongs to Plane 1 or 3. "
        f"See docs/ARCHITECTURE_SSOT.md section 2.1 and invariant I20."
    )


def test_guard_covers_every_plane2_package() -> None:
    for pkg in PLANE2_PACKAGES:
        assert (SRC / pkg).is_dir(), f"Plane 2 package missing from the tree: {pkg}"
