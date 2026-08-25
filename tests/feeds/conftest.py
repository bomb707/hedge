"""Loaders for the REAL_PUBLIC_FIXTURE captures."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    assert data["_fixture_kind"] == "REAL_PUBLIC_FIXTURE", (
        f"{name} is not labelled as a real public capture"
    )
    return data
