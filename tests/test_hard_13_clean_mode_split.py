"""Tests for HARD-13 (#106) — split the single FEAT-05
``select.{robot}_clean_mode`` back into two distinct entities:

* ``sensor.{robot}_clean_mode`` — read-only, mirrors the firmware-reported
  ``cycleInfo.cleaningMode.mode`` (the field the pre-FEAT-05 sensor used).
* ``select.{robot}_desired_clean_mode`` — writable, surfaces the staged
  pick that ``_set_cleaning_mode`` stores in ``_desired_clean_mode``.

FEAT-05 (#92, PR #93) had collapsed the two surfaces into one on the
explicit assumption that picking a mode acts on the robot, so the picked
and running values converged almost immediately. BUG-13 (#47, PR #100) and
HARD-12 (#104, PR #105) invalidated that assumption: a pick now only
stages, never writes. The picked and running modes legitimately differ
while a cycle is running, between a docked pick and the next Run, and
across an operator pick that never gets committed.

The HARD-13 split therefore pins, in order:

1. Entity description shape — the ``clean_mode`` key is back to a
   ``MyDolphinPlusSensorEntityDescription``, and a new
   ``MyDolphinPlusSelectEntityDescription`` is registered under
   ``desired_clean_mode``. The restored sensor is intentionally a plain
   string sensor (no ``device_class=SensorDeviceClass.ENUM``, no closed
   ``options=[...]``) so any mode the firmware reports outside
   ``CleanModes`` (FEAT-01 / FEAT-03 tolerance) passes through as raw
   text instead of rendering ``invalid`` against a closed enum. The
   writable select keeps its closed ``options`` — a pick list must only
   surface modes the integration knows how to commit.
2. Coordinator dispatch — ``_build_data_mapping`` wires the sensor key
   to ``_get_clean_mode_data`` and the select key to
   ``_get_desired_clean_mode_data``.
3. Getter contracts — the sensor reads reported only and exposes no
   action; the select keeps the pre-HARD-13 staged-with-reported-fallback
   logic verbatim and the ``SERVICE_SELECT_OPTION → _set_cleaning_mode``
   dispatch the FEAT-05 select had.
4. Independence — a stale ``_desired_clean_mode`` (left over from a pick
   that has not been committed by a Run) must not leak into the sensor's
   state. Anything that violates this would re-merge the two ideas
   FEAT-05 fused.
5. Translations — across every shipped locale, the ``clean_mode`` block
   moves back under ``entity.sensor`` and a new ``desired_clean_mode``
   block lands under ``entity.select`` with the same seven state labels.
   The FEAT-05 ``entity.select.clean_mode`` block is gone, so a future
   regression that re-introduced the select cannot quietly re-label it
   from the leftover translation.
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
    DATA_KEY_DESIRED_CLEAN_MODE,
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
DESIRED_CLEAN_MODE_KEY = slugify(DATA_KEY_DESIRED_CLEAN_MODE)

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


def _descriptions_with_key(key):
    return [d for d in ENTITY_DESCRIPTIONS if d.key == key]


def test_clean_mode_is_back_to_a_sensor_description():
    descriptions = _descriptions_with_key(CLEAN_MODE_KEY)
    assert len(descriptions) == 1, (
        "Expected exactly one entity description with key 'clean_mode'; "
        f"got {len(descriptions)}."
    )
    assert isinstance(descriptions[0], MyDolphinPlusSensorEntityDescription)


def test_clean_mode_sensor_stays_tolerant_of_unknown_modes():
    """The restored sensor must be a plain string sensor — no
    ``SensorDeviceClass.ENUM`` and no closed ``options=[...]``. A mode the
    firmware reports outside ``CleanModes`` (the FEAT-01 / FEAT-03 raw
    passthrough at ``coordinator.py``) would otherwise render as ``invalid``
    against the closed enum on the dashboard."""
    description = _descriptions_with_key(CLEAN_MODE_KEY)[0]
    assert description.device_class is None, (
        f"sensor.clean_mode must stay enum-free, got device_class="
        f"{description.device_class!r}"
    )
    assert description.options is None, (
        f"sensor.clean_mode must not pin a closed options list, got "
        f"options={description.options!r}"
    )


def test_no_select_description_remains_for_clean_mode():
    """The FEAT-05 swap is reverted: no
    ``MyDolphinPlusSelectEntityDescription`` for ``clean_mode`` may remain.
    A residual would re-create a writable surface that bypasses the new
    ``desired_clean_mode`` entity and confuse the registry on operator
    installs."""
    leftovers = [
        d
        for d in ENTITY_DESCRIPTIONS
        if d.key == CLEAN_MODE_KEY
        and isinstance(d, MyDolphinPlusSelectEntityDescription)
    ]
    assert leftovers == []


def test_desired_clean_mode_is_a_select_description():
    descriptions = _descriptions_with_key(DESIRED_CLEAN_MODE_KEY)
    assert len(descriptions) == 1, (
        "Expected exactly one entity description with key "
        f"'desired_clean_mode'; got {len(descriptions)}."
    )
    assert isinstance(descriptions[0], MyDolphinPlusSelectEntityDescription)


def test_desired_clean_mode_select_options_match_clean_modes_enum():
    """The picker is a closed pick list — operators must only be offered
    modes the integration knows how to commit at Run. ``next_scheduled_mode``
    (FEAT-04) uses the same source; drift would be an operator-visible UX
    bug."""
    description = _descriptions_with_key(DESIRED_CLEAN_MODE_KEY)[0]
    assert description.options == [str(mode) for mode in CleanModes]
    assert len(description.options) == 7


def test_no_sensor_description_for_desired_clean_mode():
    """The staged pick lives in the select only. A sensor on the same key
    would just shadow the select and re-introduce the two-near-identical-
    twins problem that FEAT-05 originally tried to solve."""
    leftovers = [
        d
        for d in ENTITY_DESCRIPTIONS
        if d.key == DESIRED_CLEAN_MODE_KEY
        and isinstance(d, MyDolphinPlusSensorEntityDescription)
    ]
    assert leftovers == []


def test_translation_keys_preserved():
    sensor = _descriptions_with_key(CLEAN_MODE_KEY)[0]
    select = _descriptions_with_key(DESIRED_CLEAN_MODE_KEY)[0]
    assert sensor.translation_key == CLEAN_MODE_KEY
    assert sensor.name == DATA_KEY_CLEAN_MODE
    assert select.translation_key == DESIRED_CLEAN_MODE_KEY
    assert select.name == DATA_KEY_DESIRED_CLEAN_MODE


# ---------------------------------------------------------------------------
# Coordinator dispatch — _build_data_mapping wires both getters
# ---------------------------------------------------------------------------


def test_build_data_mapping_routes_each_key_to_its_getter():
    """The two ideas must dispatch to two distinct getters; sharing one
    would re-collapse them."""
    stub = MagicMock(spec=MyDolphinPlusCoordinator)
    stub._data_mapping = {}

    MyDolphinPlusCoordinator._build_data_mapping(stub)

    assert stub._data_mapping[CLEAN_MODE_KEY] is stub._get_clean_mode_data
    assert (
        stub._data_mapping[DESIRED_CLEAN_MODE_KEY]
        is stub._get_desired_clean_mode_data
    )
    assert (
        stub._data_mapping[CLEAN_MODE_KEY]
        is not stub._data_mapping[DESIRED_CLEAN_MODE_KEY]
    )


# ---------------------------------------------------------------------------
# Sensor getter — reported-only, no action, ignores _desired_clean_mode
# ---------------------------------------------------------------------------


def _coordinator(*, reported=None, desired=None):
    stub = MagicMock(spec=MyDolphinPlusCoordinator)
    stub._desired_clean_mode = desired
    if reported is None:
        stub.aws_data = {}
    else:
        stub.aws_data = {
            DATA_SECTION_CYCLE_INFO: {
                DATA_CYCLE_INFO_CLEANING_MODE: {ATTR_MODE: reported},
            },
        }
    stub._set_cleaning_mode = MagicMock(name="_set_cleaning_mode")
    return stub


def test_sensor_state_mirrors_reported_mode():
    stub = _coordinator(reported=CleanModes.STAIRS.value)

    result = MyDolphinPlusCoordinator._get_clean_mode_data(stub, SimpleNamespace())

    assert result[ATTR_STATE] == CleanModes.STAIRS.value


def test_sensor_state_is_none_when_no_shadow_yet():
    """BUG-16 keeps the entity ``available=False`` until the first
    ``systemState`` lands, so the ``None`` should not surface in practice
    — but the contract is honest reporting, not a lying default."""
    stub = _coordinator(reported=None)

    result = MyDolphinPlusCoordinator._get_clean_mode_data(stub, SimpleNamespace())

    assert result[ATTR_STATE] is None


def test_sensor_exposes_no_action():
    """The sensor is read-only; even if a future regression wired
    ``async_select_option`` to it, the absence of ``ATTR_ACTIONS`` would
    surface the bug at dispatch time."""
    stub = _coordinator(reported=CleanModes.REGULAR.value)

    result = MyDolphinPlusCoordinator._get_clean_mode_data(stub, SimpleNamespace())

    assert ATTR_ACTIONS not in result


def test_sensor_ignores_staged_desired_value():
    """A pick stages ``_desired_clean_mode`` but never moves
    ``cycleInfo.cleaningMode.mode``. The sensor MUST show what the robot
    is currently running, not the operator's pending intent — otherwise
    the FEAT-05 confusion (two ideas, one entity) returns through the
    back door."""
    stub = _coordinator(reported=CleanModes.REGULAR.value, desired=CleanModes.STAIRS.value)

    result = MyDolphinPlusCoordinator._get_clean_mode_data(stub, SimpleNamespace())

    assert result[ATTR_STATE] == CleanModes.REGULAR.value


# ---------------------------------------------------------------------------
# Select getter — staged-with-reported-fallback + ATTR_ACTIONS dispatch
# ---------------------------------------------------------------------------


def test_desired_select_state_prefers_staged_pick():
    """The pre-HARD-13 logic that the FEAT-05 select used moves to
    ``_get_desired_clean_mode_data`` verbatim: when the operator has
    staged a pick, the select shows that staged value regardless of what
    the firmware is currently reporting."""
    stub = _coordinator(reported=CleanModes.REGULAR.value, desired=CleanModes.STAIRS.value)

    result = MyDolphinPlusCoordinator._get_desired_clean_mode_data(
        stub, SimpleNamespace()
    )

    assert result[ATTR_STATE] == CleanModes.STAIRS.value


def test_desired_select_falls_back_to_reported_when_nothing_staged():
    """First refresh after init (``_desired_clean_mode`` not yet seeded
    by ``_reconcile_desired_clean_mode``) falls back to the firmware-
    reported value so the select shows the live mode instead of an empty
    placeholder."""
    stub = _coordinator(reported=CleanModes.STAIRS.value, desired=None)

    result = MyDolphinPlusCoordinator._get_desired_clean_mode_data(
        stub, SimpleNamespace()
    )

    assert result[ATTR_STATE] == CleanModes.STAIRS.value


def test_desired_select_falls_back_to_regular_when_shadow_missing():
    """No staged pick and no shadow yet → REGULAR placeholder so the
    select always renders a valid option. The closed ``options`` list on
    the description would otherwise reject ``None`` from the dashboard."""
    stub = _coordinator(reported=None, desired=None)

    result = MyDolphinPlusCoordinator._get_desired_clean_mode_data(
        stub, SimpleNamespace()
    )

    assert result[ATTR_STATE] == CleanModes.REGULAR


def test_desired_select_exposes_select_option_action():
    """``async_select_option`` on the select reaches the coordinator via
    ``async_execute_device_action(SERVICE_SELECT_OPTION, option)`` →
    ``ATTR_ACTIONS[SERVICE_SELECT_OPTION]``. Routing it to
    ``_set_cleaning_mode`` is what gives the picker the BUG-13 / HARD-12
    write-on-commit semantics for free."""
    stub = _coordinator(reported=CleanModes.REGULAR.value)

    result = MyDolphinPlusCoordinator._get_desired_clean_mode_data(
        stub, SimpleNamespace()
    )

    actions = result[ATTR_ACTIONS]
    assert SERVICE_SELECT_OPTION in actions
    assert actions[SERVICE_SELECT_OPTION] is stub._set_cleaning_mode


# ---------------------------------------------------------------------------
# Translations — clean_mode back under sensor, desired_clean_mode under select
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", TRANSLATION_FILES, ids=lambda p: p.name)
def test_clean_mode_block_restored_under_entity_sensor(path: Path) -> None:
    """The pre-FEAT-05 block (name + 7 state labels) is back under
    ``entity.sensor.clean_mode``. Reusing the same translation key as
    pre-FEAT-05 means an operator install whose registry still holds the
    old ``sensor.{robot}_clean_mode`` row re-binds to it (the computed
    ``unique_id`` is identical)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    sensor_entries = data.get("entity", {}).get("sensor", {})
    assert "clean_mode" in sensor_entries, (
        f"{path.name}: entity.sensor.clean_mode is missing"
    )

    block = sensor_entries["clean_mode"]
    assert isinstance(block.get("name"), str) and block["name"].strip()

    state_labels = block.get("state", {})
    expected_keys = {mode.value for mode in CleanModes}
    assert expected_keys.issubset(state_labels.keys()), (
        f"{path.name}: entity.sensor.clean_mode.state is missing "
        f"{expected_keys - state_labels.keys()}"
    )


@pytest.mark.parametrize("path", TRANSLATION_FILES, ids=lambda p: p.name)
def test_desired_clean_mode_block_present_under_entity_select(path: Path) -> None:
    """The new picker ships its own block in every locale; reusing the
    sensor's state labels keeps the visible mode names consistent across
    the two surfaces."""
    data = json.loads(path.read_text(encoding="utf-8"))
    select_entries = data.get("entity", {}).get("select", {})
    assert "desired_clean_mode" in select_entries, (
        f"{path.name}: entity.select.desired_clean_mode is missing"
    )

    block = select_entries["desired_clean_mode"]
    assert isinstance(block.get("name"), str) and block["name"].strip()

    state_labels = block.get("state", {})
    expected_keys = {mode.value for mode in CleanModes}
    assert expected_keys.issubset(state_labels.keys()), (
        f"{path.name}: entity.select.desired_clean_mode.state is missing "
        f"{expected_keys - state_labels.keys()}"
    )


@pytest.mark.parametrize("path", TRANSLATION_FILES, ids=lambda p: p.name)
def test_clean_mode_block_removed_from_entity_select(path: Path) -> None:
    """The FEAT-05 ``entity.select.clean_mode`` block must be gone in every
    shipped locale. A stale block would silently re-label the surface if
    a future regression re-introduced the FEAT-05 select."""
    data = json.loads(path.read_text(encoding="utf-8"))
    select_entries = data.get("entity", {}).get("select", {})
    assert "clean_mode" not in select_entries, (
        f"{path.name}: entity.select.clean_mode must be removed (moved to "
        "entity.sensor.clean_mode by HARD-13)"
    )
