"""Tests for FEAT-05 — replace the read-only ``sensor.{robot}_clean_mode``
with a writable ``select.{robot}_clean_mode``.

The migration collapses the read-only ``clean_mode`` sensor and the
``vacuum.set_fan_speed`` write surface into one Lovelace-friendly entity
that both shows and sets the cleaning mode. The breaking domain swap
(sensor → select) is intentional and accepted in #92; this test pins the
post-fix shape so a regression cannot silently re-introduce either the
removed sensor or a dangling, unwritable select.

The select reuses the BUG-13-correct write path (``_set_cleaning_mode``)
unchanged, so the docked-vs-running routing is already locked by
``test_bug_13_decouple_mode_pick.py``. The tests below only exercise
what FEAT-05 itself adds:

1. The entity description for ``clean_mode`` is now a
   ``MyDolphinPlusSelectEntityDescription`` with the seven
   ``CleanModes`` options.
2. No ``MyDolphinPlusSensorEntityDescription`` for ``clean_mode`` remains.
3. ``coordinator._get_clean_mode_data`` returns ``ATTR_ACTIONS`` mapping
   ``SERVICE_SELECT_OPTION`` to the existing ``_set_cleaning_mode``.
4. ``ATTR_STATE`` still reflects ``cycleInfo.cleaningMode.mode``.
5. Each shipped locale ships the ``Clean Mode`` block under
   ``entity.select`` and no longer under ``entity.sensor`` — operator-
   visible side of the breaking change.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from custom_components.mydolphin_plus.common.clean_modes import CleanModes
from custom_components.mydolphin_plus.common.consts import (
    ATTR_ACTIONS,
    DATA_CYCLE_INFO_CLEANING_MODE,
    DATA_KEY_CLEAN_MODE,
    DATA_SECTION_CYCLE_INFO,
)
from custom_components.mydolphin_plus.common.entity_descriptions import (
    ENTITY_DESCRIPTIONS,
    MyDolphinPlusSelectEntityDescription,
    MyDolphinPlusSensorEntityDescription,
)
from custom_components.mydolphin_plus.managers.coordinator import (
    MyDolphinPlusCoordinator,
)
from homeassistant.const import ATTR_MODE, ATTR_STATE, SERVICE_SELECT_OPTION
from homeassistant.util import slugify

CLEAN_MODE_KEY = slugify(DATA_KEY_CLEAN_MODE)

COMPONENT_ROOT = (
    Path(__file__).resolve().parent.parent / "custom_components" / "mydolphin_plus"
)
TRANSLATION_FILES = (
    COMPONENT_ROOT / "strings.json",
    COMPONENT_ROOT / "translations" / "en.json",
    COMPONENT_ROOT / "translations" / "fr.json",
    COMPONENT_ROOT / "translations" / "it.json",
)


# ---------------------------------------------------------------------------
# Entity description shape
# ---------------------------------------------------------------------------


def _clean_mode_descriptions():
    return [d for d in ENTITY_DESCRIPTIONS if d.key == CLEAN_MODE_KEY]


def test_clean_mode_is_a_select_description():
    descriptions = _clean_mode_descriptions()
    assert len(descriptions) == 1, (
        "Expected exactly one entity description with key 'clean_mode'; "
        f"got {len(descriptions)}."
    )
    assert isinstance(descriptions[0], MyDolphinPlusSelectEntityDescription)


def test_no_sensor_description_remains_for_clean_mode():
    """The breaking sensor → select swap means no ``MyDolphinPlusSensorEntityDescription``
    for ``clean_mode`` may slip back in (e.g. via a stray copy-paste in a future
    sensor split). HACS reinstalls would otherwise resurrect the orphaned
    ``sensor.{robot}_clean_mode`` from a previous install's registry."""
    sensor_clean_modes = [
        d
        for d in ENTITY_DESCRIPTIONS
        if d.key == CLEAN_MODE_KEY
        and isinstance(d, MyDolphinPlusSensorEntityDescription)
    ]
    assert sensor_clean_modes == []


def test_clean_mode_select_options_match_clean_modes_enum():
    """The select's options must be the full ``CleanModes`` set as strings so
    every firmware mode the integration knows about is selectable from the
    dashboard. ``next_scheduled_mode`` (FEAT-04) uses the same source — a
    drift between the two surfaces would be an operator-visible UX bug."""
    description = _clean_mode_descriptions()[0]
    assert description.options == [str(mode) for mode in CleanModes]
    assert len(description.options) == 7


def test_clean_mode_translation_key_preserved():
    """The translation key must stay ``clean_mode`` so the moved-but-otherwise-
    unchanged ``entity.select.clean_mode`` block continues to resolve. A
    drift here would leave the select labelled with the raw firmware
    strings."""
    description = _clean_mode_descriptions()[0]
    assert description.translation_key == CLEAN_MODE_KEY
    assert description.name == DATA_KEY_CLEAN_MODE


# ---------------------------------------------------------------------------
# Coordinator wiring — ATTR_ACTIONS dispatch
# ---------------------------------------------------------------------------


def _coordinator_with_mode(mode: str | None) -> MagicMock:
    """Stub a coordinator exposing only what ``_get_clean_mode_data`` reads."""
    stub = MagicMock(spec=MyDolphinPlusCoordinator)
    # BUG-13 (write-on-commit) — the getter prefers the staged pick when
    # set; clear it so these tests still exercise the firmware-reported
    # fallback path they were written for.
    stub._desired_clean_mode = None
    if mode is None:
        stub.aws_data = {}
    else:
        stub.aws_data = {
            DATA_SECTION_CYCLE_INFO: {
                DATA_CYCLE_INFO_CLEANING_MODE: {ATTR_MODE: mode},
            },
        }
    stub._set_cleaning_mode = MagicMock(name="_set_cleaning_mode")
    return stub


def test_get_clean_mode_data_state_reflects_current_mode():
    stub = _coordinator_with_mode(CleanModes.STAIRS.value)

    result = MyDolphinPlusCoordinator._get_clean_mode_data(stub, SimpleNamespace())

    assert result[ATTR_STATE] == CleanModes.STAIRS.value


def test_get_clean_mode_data_defaults_to_regular_when_shadow_missing():
    """No ``cycleInfo`` in the shadow yet (e.g. pre-first-shadow window or
    a freshly added device) → fall back to ``CleanModes.REGULAR`` so the
    select displays a valid option instead of an empty placeholder."""
    stub = _coordinator_with_mode(None)

    result = MyDolphinPlusCoordinator._get_clean_mode_data(stub, SimpleNamespace())

    assert result[ATTR_STATE] == CleanModes.REGULAR


def test_get_clean_mode_data_exposes_select_option_action():
    """The select's ``async_select_option`` reaches the coordinator via
    ``async_execute_device_action(SERVICE_SELECT_OPTION, option)`` →
    ``ATTR_ACTIONS[SERVICE_SELECT_OPTION]``. Mapping it to
    ``_set_cleaning_mode`` (and not to a fresh setter) is what gives the
    select the BUG-13 docked/running split for free."""
    stub = _coordinator_with_mode(CleanModes.REGULAR.value)

    result = MyDolphinPlusCoordinator._get_clean_mode_data(stub, SimpleNamespace())

    actions = result[ATTR_ACTIONS]
    assert SERVICE_SELECT_OPTION in actions
    assert actions[SERVICE_SELECT_OPTION] is stub._set_cleaning_mode


# ---------------------------------------------------------------------------
# Translations — clean_mode block moved from sensor to select
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", TRANSLATION_FILES, ids=lambda p: p.name)
def test_clean_mode_block_now_under_entity_select(path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    select_entries = data.get("entity", {}).get("select", {})
    assert "clean_mode" in select_entries, (
        f"{path.name} is missing entity.select.clean_mode"
    )

    block = select_entries["clean_mode"]
    assert isinstance(block.get("name"), str) and block["name"].strip()

    state_labels = block.get("state", {})
    expected_keys = {mode.value for mode in CleanModes}
    assert expected_keys.issubset(state_labels.keys()), (
        f"{path.name}: entity.select.clean_mode.state is missing "
        f"{expected_keys - state_labels.keys()}"
    )


@pytest.mark.parametrize("path", TRANSLATION_FILES, ids=lambda p: p.name)
def test_clean_mode_block_removed_from_entity_sensor(path: Path) -> None:
    """The breaking move (sensor → select) must be complete on the translation
    side too. A residual ``entity.sensor.clean_mode`` block would silently
    keep the old sensor labelled if a future regression brought the sensor
    back, defeating the migration's "one entity" simplicity goal."""
    data = json.loads(path.read_text(encoding="utf-8"))
    sensor_entries = data.get("entity", {}).get("sensor", {})
    assert "clean_mode" not in sensor_entries, (
        f"{path.name}: entity.sensor.clean_mode must be removed (moved to "
        "entity.select.clean_mode by FEAT-05)"
    )
