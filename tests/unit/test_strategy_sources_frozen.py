"""The two supplied strategy documents are inputs, not project files.

They must survive byte-for-byte. This test is the mechanical enforcement of that rule
(``docs/INDEX.md`` section 1.2). A failure means a frozen source was modified: revert it.
Do not update ``CHECKSUMS.sha256`` unless the user has supplied a genuinely new revision of
the source document.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

STRATEGY_DIR = Path(__file__).resolve().parents[2] / "docs" / "strategy"
CHECKSUM_FILE = STRATEGY_DIR / "CHECKSUMS.sha256"

CANONICAL = "Polymarket_5m_Maker_Bot_Canonical_Strategy_Spec.md"
DETAILED = "Polymarket_5m_Maker_Bot_Detailed_Strategy_By_Step_and_Role.md"


def _pinned() -> dict[str, str]:
    pinned: dict[str, str] = {}
    for line in CHECKSUM_FILE.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, name = line.split(maxsplit=1)
        pinned[name.strip()] = digest
    return pinned


def test_checksum_file_pins_both_sources() -> None:
    assert set(_pinned()) == {CANONICAL, DETAILED}


@pytest.mark.parametrize("name", [CANONICAL, DETAILED])
def test_source_matches_pinned_digest(name: str) -> None:
    path = STRATEGY_DIR / name
    assert path.is_file(), f"frozen strategy source is missing: {path}"
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    assert actual == _pinned()[name], (
        f"{name} no longer matches its pinned digest. It is a frozen input document and "
        f"must not be edited, reformatted, or corrected. Revert the change."
    )
