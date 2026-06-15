"""Regression tests for BUG-15.

PR #50 (FEAT-01) added the ``stairs`` cleaning mode and its state translations
but forgot ``entity.number.cycle_time_stairs.name`` in ``strings.json`` and in
the three shipped ``translations/*.json`` files. As a result, the per-mode
cycle-time number entity created for ``stairs`` fell back to the hard-coded
English ``Cycle Time stairs`` label, both for the UI label and the slug derived
on a fresh install.

The parity check below iterates the real ``CleanModes`` enum (the same source
``entity_descriptions.py`` walks to declare the number entities), so any future
mode added to the enum without its companion translation is caught with no
parallel list to maintain.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from custom_components.mydolphin_plus.common.clean_modes import CleanModes

COMPONENT_ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "mydolphin_plus"

TRANSLATION_FILES = (
    COMPONENT_ROOT / "strings.json",
    COMPONENT_ROOT / "translations" / "en.json",
    COMPONENT_ROOT / "translations" / "fr.json",
    COMPONENT_ROOT / "translations" / "it.json",
)


def _load(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


@pytest.mark.parametrize("path", TRANSLATION_FILES, ids=lambda p: p.name)
@pytest.mark.parametrize("mode", [m.value for m in CleanModes])
def test_cycle_time_translation_present(path: Path, mode: str) -> None:
    """Every CleanModes member must have a ``cycle_time_<mode>.name`` translation."""
    data = _load(path)
    number_entries = data.get("entity", {}).get("number", {})
    key = f"cycle_time_{mode}"
    assert key in number_entries, f"{path.name} is missing {key!r}"
    name = number_entries[key].get("name")
    assert isinstance(name, str) and name.strip(), (
        f"{path.name}:{key}.name must be a non-empty string"
    )
