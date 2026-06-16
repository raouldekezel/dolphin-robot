"""Regression tests for MAP-05.

Three entity descriptions declared ``translation_key=`` but had no matching
key in any of the shipped translation files (``strings.json`` and
``translations/{en,fr,it}.json``):

* ``sensor.cycle_time`` (``Cycle Time``)
* ``sensor.battery`` (``Battery``)
* ``sensor.temperature`` (``Temperature``) — gated by
  ``supported_robot=RobotFamily.M700`` so the defect was invisible on
  S-series hosts but structurally identical.

When the translation key is missing Home Assistant falls back to the
hard-coded English ``name=`` attribute regardless of the user locale, so a
``language: fr``/``it`` install would see ``Cycle Time`` / ``Battery`` /
``Temperature`` instead of their localised labels — same shape as BUG-15
on the per-mode ``cycle_time_<mode>`` entities.

The check below walks the *real* ``ENTITY_DESCRIPTIONS`` list, so any future
description that declares a ``translation_key=`` and a non-empty ``name=``
without its companion translation trips the suite with no parallel list to
maintain. Descriptions that ship ``name=""`` (the ``has_entity_name``
device-name fallback used by ``vacuum.vacuum`` and ``remote.remote``) are
correctly skipped — for those entities HA never reads ``entity.<domain>.
<key>.name`` and the absence is intentional, not a defect.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from custom_components.mydolphin_plus.common.entity_descriptions import (
    ENTITY_DESCRIPTIONS,
)

COMPONENT_ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "mydolphin_plus"

TRANSLATION_FILES = (
    COMPONENT_ROOT / "strings.json",
    COMPONENT_ROOT / "translations" / "en.json",
    COMPONENT_ROOT / "translations" / "fr.json",
    COMPONENT_ROOT / "translations" / "it.json",
)

# Each named description with a translation_key must have its label in every
# shipped file. ``name=""`` entities (vacuum.vacuum, remote.remote) rely on
# the device-name fallback and have no translation key to look up.
TRANSLATED_DESCRIPTIONS = [
    desc
    for desc in ENTITY_DESCRIPTIONS
    if desc.translation_key and desc.name
]


def _load(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


@pytest.mark.parametrize("path", TRANSLATION_FILES, ids=lambda p: p.name)
@pytest.mark.parametrize(
    "description",
    TRANSLATED_DESCRIPTIONS,
    ids=lambda d: f"{d.platform.value}.{d.translation_key}",
)
def test_named_translation_key_has_label(path: Path, description) -> None:
    """Every translated description must have ``entity.<domain>.<key>.name``."""
    domain = description.platform.value
    key = description.translation_key

    data = _load(path)
    entries = data.get("entity", {}).get(domain, {})
    assert key in entries, (
        f"{path.name} is missing entity.{domain}.{key} "
        f"(declared in entity_descriptions.py as name={description.name!r})"
    )
    label = entries[key].get("name")
    assert isinstance(label, str) and label.strip(), (
        f"{path.name}:entity.{domain}.{key}.name must be a non-empty string"
    )
