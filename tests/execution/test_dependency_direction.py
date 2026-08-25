"""The deterministic core must not depend on execution.

Strategy, market, accounting, numeric, and domain form Plane 2. Execution sits above them and
knows about a venue; letting the dependency run the other way would put transport concerns
inside the code P5 replays, and a strategy that could see execution state would stop being a
pure function of the event stream (I20).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import maker5m

SRC = Path(maker5m.__file__).parent
CORE_PACKAGES = ("numeric", "market", "strategy", "accounting")
CORE_MODULES = ("domain.py", "safety.py")


def core_sources() -> list[Path]:
    packaged = [p for pkg in CORE_PACKAGES for p in (SRC / pkg).rglob("*.py")]
    return sorted(packaged + [SRC / name for name in CORE_MODULES])


@pytest.mark.parametrize("path", core_sources(), ids=lambda p: p.name)
def test_no_core_module_imports_execution(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert "maker5m.execution" not in text, (
        f"{path.name} references maker5m.execution; the deterministic core must not depend "
        f"on the venue-facing layer."
    )
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert not node.module.startswith("maker5m.execution")
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("maker5m.execution")


def test_no_core_module_imports_the_sdk() -> None:
    """The SDK lives behind one adapter and nowhere else.

    Checked as imports, not text: core modules legitimately *mention* the SDK in comments that
    record which venue contract they were verified against.
    """
    for path in core_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("polymarket")
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("polymarket")


def test_the_sdk_is_imported_in_at_most_one_module() -> None:
    importers = []
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            if any(n.startswith("polymarket") for n in names):
                importers.append(path.name)
    assert len(set(importers)) <= 1, f"the SDK is imported in several places: {set(importers)}"


def test_execution_may_depend_on_the_core() -> None:
    """The permitted direction, asserted so the boundary is documented both ways."""
    text = (SRC / "execution" / "prepare.py").read_text(encoding="utf-8")
    assert "maker5m.strategy.decision" in text
    assert "maker5m.numeric" in text
