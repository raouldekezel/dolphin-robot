"""Tests for FEAT-01 — add `stairs` (Full Coverage) cleaning mode.

FEAT-01: the Dolphin S2000 (and other S-series robots) exposes a
firmware mode `stairs` that the Maytronics app surfaces as
« Couverture complète » (FR) / "Full Coverage" (EN) / "Copertura completa"
(IT). Before this change `CleanModes` had no `STAIRS` member, the firmware
string landed in the sensor untranslated, and no per-mode cycle-time
entity was generated for it.

This MVP adds `STAIRS` unconditionally for every robot. The catalog
intersection / user-driven hiding discussion is deferred to a follow-up
FEAT (see PR #35 thread). The shadow reader at
`coordinator.py:519` already passes unknown modes through as raw strings,
so no read-side tolerance code is required.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from custom_components.mydolphin_plus.common.clean_modes import (
    CLEAN_MODES_CYCLE_TIME,
    CleanModes,
)
from custom_components.mydolphin_plus.common.entity_descriptions import (
    ENTITY_DESCRIPTIONS,
)


def test_stairs_in_enum_and_cycle_time():
    """`STAIRS` member exists with value `"stairs"` and a 150-min default.

    150 min matches the Maytronics app's default selection for Full
    Coverage on the Dolphin S2000 (operator-verified, see doc IT
    « Session 2026-06-13 »). The user can override per-robot via
    ``number.<robot>_cycle_time_stairs``.

    A regression that renames the member or forgets the
    ``CLEAN_MODES_CYCLE_TIME`` entry fails this test.
    """
    assert CleanModes.STAIRS.value == "stairs"
    assert CLEAN_MODES_CYCLE_TIME[CleanModes.STAIRS] == 150


def test_stairs_cycle_time_entity_description_generated():
    """The per-mode loop generates exactly one `cycle_time_stairs` description.

    The integration iterates ``list(CleanModes)`` to append a
    ``MyDolphinPlusNumberEntityDescription`` per mode
    (``entity_descriptions.py``). A regression that filters out
    `STAIRS` from that loop fails this test.
    """
    stairs_descriptions = [
        d for d in ENTITY_DESCRIPTIONS if d.key == "cycle_time_stairs"
    ]
    assert len(stairs_descriptions) == 1


@pytest.mark.parametrize(
    "filename, expected",
    [
        ("strings.json", "Full Coverage"),
        ("translations/en.json", "Full Coverage"),
        ("translations/fr.json", "Couverture complète"),
        ("translations/it.json", "Copertura completa"),
    ],
)
def test_stairs_label_in_translations(filename, expected):
    """Each shipped locale carries the correct Full Coverage label.

    The label must appear in BOTH surfaces:
    - ``entity.sensor.clean_mode.state.stairs`` — the
      ``sensor.<robot>_clean_mode`` text
    - ``entity.vacuum.vacuum.state_attributes.fan_speed.state.stairs``
      — the dropdown label in the vacuum entity card

    FR is empirically grounded (the operator's MyDolphin Plus app
    displays « Couverture complète » for this mode). EN and IT match
    Maytronics' line-wide marketing terminology, confirmed by the
    operator in PR #35 comments.
    """
    path = (
        Path(__file__).parent.parent
        / "custom_components"
        / "mydolphin_plus"
        / filename
    )
    data = json.loads(path.read_text(encoding="utf-8"))
    sensor_label = data["entity"]["sensor"]["clean_mode"]["state"]["stairs"]
    vacuum_label = (
        data["entity"]["vacuum"]["vacuum"]["state_attributes"]["fan_speed"][
            "state"
        ]["stairs"]
    )
    assert sensor_label == expected, (
        f"sensor.clean_mode.state.stairs in {filename!r} = {sensor_label!r}, "
        f"expected {expected!r}"
    )
    assert vacuum_label == expected, (
        f"vacuum.fan_speed.state.stairs in {filename!r} = {vacuum_label!r}, "
        f"expected {expected!r}"
    )
