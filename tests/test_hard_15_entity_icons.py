"""Regression tests for HARD-15 — differentiated entity icons.

Before this change, the seven per-mode ``number.cycle_time_<mode>`` entities
shared HA's blank default and were visually indistinguishable in the device
page, dashboards, automations and the entity picker. A handful of diagnostic
sensors were in the same boat, and two error sensors carried icons that
better fit other entities (``robot-vacuum-variant`` on ``robot_error`` should
belong to the Model sensor; ``water-boiler`` on the power-supply error is
just wrong).

This regression suite pins:

1. Every ``cycle_time_<mode>`` description carries an icon, and the icons
   are pairwise distinct — so the operator can tell them apart at a glance.
   Parametrised over ``CleanModes`` so adding a mode to the enum without an
   icon fails the suite (no parallel list to maintain).
2. The two error-sensor swaps that free ``mdi:robot-vacuum-variant`` for
   the actual Model sensor and drop the boiler glyph from the PWS error.
3. Distinct icons across the closely-related pairs the audit called out
   (``robot_error`` vs ``robot_type``, ``robot_status`` vs ``robot_type``).

The dynamic icons emitted at runtime by the coordinator via ``ATTR_ICON``
(cycle_time / cycle_time_left hour glyph, filter_status FILTER_BAG_ICONS,
led_mode ICON_LED_MODES) are intentionally not touched — they already
provide differentiated state-aware glyphs.
"""

from __future__ import annotations

import pytest

from custom_components.mydolphin_plus.common.clean_modes import (
    CleanModes,
    get_clean_mode_cycle_time_key,
)
from custom_components.mydolphin_plus.common.entity_descriptions import (
    ENTITY_DESCRIPTIONS,
)


def _by_key(key: str):
    matches = [d for d in ENTITY_DESCRIPTIONS if d.key == key]
    assert len(matches) == 1, f"expected exactly one description for {key!r}, got {len(matches)}"
    return matches[0]


@pytest.mark.parametrize("mode", [m for m in CleanModes], ids=lambda m: m.value)
def test_cycle_time_number_has_icon(mode: CleanModes) -> None:
    """Every ``cycle_time_<mode>`` description must ship with a non-empty ``mdi:`` icon."""
    desc = _by_key(get_clean_mode_cycle_time_key(mode))
    assert isinstance(desc.icon, str) and desc.icon.startswith("mdi:"), (
        f"cycle_time_{mode.value} icon must be a non-empty mdi glyph, got {desc.icon!r}"
    )


def test_cycle_time_icons_are_pairwise_distinct() -> None:
    """The seven per-mode cycle-time icons must all differ.

    A duplicate defeats the purpose of the audit — the operator would still
    see two identical glyphs in the picker.
    """
    icons = [
        _by_key(get_clean_mode_cycle_time_key(mode)).icon for mode in CleanModes
    ]
    assert len(icons) == len(set(icons)), (
        f"cycle_time icons must be pairwise distinct, got {icons}"
    )


def test_robot_error_and_robot_type_icons_differ() -> None:
    """``robot_error`` must not shadow ``robot_type``.

    The pre-HARD-15 state used ``mdi:robot-vacuum-variant`` for
    ``robot_error`` and left ``robot_type`` (the Model sensor) without an
    icon. The audit swap frees ``robot-vacuum-variant`` for the Model
    sensor and moves the error to ``mdi:robot-dead``.
    """
    robot_error = _by_key("robot_error")
    robot_type = _by_key("robot_type")
    assert robot_error.icon and robot_type.icon
    assert robot_error.icon != robot_type.icon


def test_pws_error_is_not_a_water_boiler() -> None:
    """``power_supply_error`` must not use the boiler glyph.

    The power supply is not a boiler; the audit moves it to a
    power-plug glyph so the icon actually matches the entity.
    """
    pws_error = _by_key("power_supply_error")
    assert pws_error.icon
    assert pws_error.icon != "mdi:water-boiler"


def test_robot_status_and_robot_type_icons_differ() -> None:
    """``robot_status`` (phase) and ``robot_type`` (Model) must not share a glyph.

    Both talk about the robot but at different levels; identical icons
    make the device page harder to scan.
    """
    robot_status = _by_key("robot_status")
    robot_type = _by_key("robot_type")
    assert robot_status.icon and robot_type.icon
    assert robot_status.icon != robot_type.icon
