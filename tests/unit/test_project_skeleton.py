"""Phase 0 skeleton guards: the package tree is real, and nothing trades."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

import maker5m
from maker5m.safety import LIVE_TRADING_DISABLED_REASON, LIVE_TRADING_ENABLED

REPO_ROOT = Path(__file__).resolve().parents[2]

SUBPACKAGES = [
    "accounting",
    "bot",
    "execution",
    "feeds",
    "market",
    "numeric",
    "replay",
    "risk",
    "settlement",
    "strategy",
    "telemetry",
    "ui",
]

REQUIRED_DOCS = [
    "docs/INDEX.md",
    "docs/ARCHITECTURE_SSOT.md",
    "docs/INVARIANTS.md",
    "docs/OPEN_ITEMS.md",
    "docs/DEVELOPMENT_PLAN.md",
    "docs/STATUS.md",
]


@pytest.mark.parametrize("name", SUBPACKAGES)
def test_subpackage_imports_and_is_documented(name: str) -> None:
    module = importlib.import_module(f"maker5m.{name}")
    assert module.__doc__, f"maker5m.{name} must document its responsibility and plane"


def test_package_version_is_present() -> None:
    assert maker5m.__version__


def test_live_trading_is_disabled() -> None:
    """P0 through P13 must never be able to send an order.

    Unlocking this is a P14 decision gated on the Canonical section 35 checklist and
    explicit human authorisation. It is not a knob.
    """
    assert LIVE_TRADING_ENABLED is False
    assert LIVE_TRADING_DISABLED_REASON


@pytest.mark.parametrize("relpath", REQUIRED_DOCS)
def test_required_project_document_exists(relpath: str) -> None:
    path = REPO_ROOT / relpath
    assert path.is_file(), f"missing required project document: {relpath}"
    assert path.read_text(encoding="utf-8").strip(), f"{relpath} is empty"
