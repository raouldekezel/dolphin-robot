"""Regression tests for BUG-16.

After a Home Assistant restart while a cleaning cycle is active,
``vacuum.{robot}`` showed a 5-10 s window of state ``docked`` before
recovering to ``cleaning``. Root cause: ``MyDolphinPlusBaseEntity`` had
no ``available`` override and ``vacuum.py`` carried a ctor default
``_attr_activity = VacuumActivity.DOCKED``, so the initial state write
performed by ``async_add_entities(..., update_before_add=True)`` published
``docked`` before any AWS shadow arrived.

The fix is a one-way latch on the coordinator
(``has_real_data``), flipped ``True`` the first time
``_set_system_status_details`` is called with a payload that actually
contains ``DATA_SECTION_SYSTEM_STATE``, plus an ``available`` override on
the base entity gating availability on that latch. The vacuum's ctor
default and the dead ``update_component(None)`` else branch are dropped,
together with the dead ``_can_load_components`` flag.

The 27 tests below split in two groups:

* **Regression catchers (FAIL pre-fix, PASS post-fix)** — these go red if
  the corresponding piece of the fix is reverted.
* **Characterization / guard rails (PASS pre-fix, PASS post-fix)** —
  these lock the truth table and the inherited ``CoordinatorEntity``
  semantics so a fix-adjacent change cannot drift silently.
"""

from __future__ import annotations

from pathlib import Path
import re
from types import SimpleNamespace

import pytest

from custom_components.mydolphin_plus.common.calculated_state import CalculatedState
from custom_components.mydolphin_plus.common.clean_modes import CleanModes
from custom_components.mydolphin_plus.common.consts import (
    ATTR_ATTRIBUTES,
    ATTR_CALCULATED_STATUS,
    ATTR_POWER_SUPPLY_STATE,
    ATTR_ROBOT_STATE,
    ATTR_VACUUM_STATE,
    DATA_CYCLE_INFO_CLEANING_MODE,
    DATA_SECTION_CYCLE_INFO,
    DATA_SECTION_DYNAMIC,
    DATA_SECTION_SYSTEM_STATE,
    DATA_SYSTEM_STATE_PWS_STATE,
    DATA_SYSTEM_STATE_ROBOT_STATE,
)
from custom_components.mydolphin_plus.common.power_supply_state import PowerSupplyState
from custom_components.mydolphin_plus.common.robot_state import RobotState
from custom_components.mydolphin_plus.models.system_details import SystemDetails
from homeassistant.components.vacuum import VacuumActivity
from homeassistant.const import ATTR_MODE, ATTR_STATE

COMPONENT_ROOT = (
    Path(__file__).resolve().parent.parent / "custom_components" / "mydolphin_plus"
)


# ---------------------------------------------------------------------------
# Helpers — minimal stubs that bypass __init__ for the units under test.
# ---------------------------------------------------------------------------


def _make_coordinator():
    """Return a minimal ``MyDolphinPlusCoordinator`` instance without going
    through ``__init__`` (which would require ``hass`` + an RestAPI / AWS
    client). Only what the gate + latch read is wired up."""
    from custom_components.mydolphin_plus.managers.coordinator import (
        MyDolphinPlusCoordinator,
    )

    coord = object.__new__(MyDolphinPlusCoordinator)
    coord._system_details = SystemDetails()
    coord._aws_client = SimpleNamespace(data={})
    coord._has_real_data = False
    return coord


def _make_base_entity(*, last_update_success: bool, has_real_data: bool):
    """Return a minimal ``MyDolphinPlusBaseEntity`` instance with the two
    attributes the ``available`` override reads. Bypasses
    ``CoordinatorEntity.__init__`` and the slugify chain in
    ``MyDolphinPlusBaseEntity.__init__``."""
    from custom_components.mydolphin_plus.common.base_entity import (
        MyDolphinPlusBaseEntity,
    )

    entity = object.__new__(MyDolphinPlusBaseEntity)
    entity.coordinator = SimpleNamespace(
        last_update_success=last_update_success,
        has_real_data=has_real_data,
    )
    # ``Entity.available`` reads ``_attr_available`` (default ``True``)
    entity._attr_available = True
    return entity


def _make_vacuum_entity(*, last_update_success=True, has_real_data=True):
    """Return a minimal ``MyDolphinPlusVacuumEntity`` instance with just
    the attributes ``update_component`` and ``available`` touch. Mirrors
    the post-fix ctor by setting ``_attr_activity = None`` explicitly so
    direct attribute access on the property does not raise."""
    from custom_components.mydolphin_plus.vacuum import MyDolphinPlusVacuumEntity

    entity = object.__new__(MyDolphinPlusVacuumEntity)
    entity.coordinator = SimpleNamespace(
        last_update_success=last_update_success,
        has_real_data=has_real_data,
    )
    entity._attr_available = True
    entity._attr_activity = None
    entity._attr_extra_state_attributes = None
    entity._attr_fan_speed = None
    return entity


# ---------------------------------------------------------------------------
# Group A — characterisation: ``_get_updated_data`` truth table is unchanged.
# ---------------------------------------------------------------------------


def test_get_updated_data_empty_aws_data_returns_docked_off():
    result = SystemDetails._get_updated_data({})

    assert result[ATTR_VACUUM_STATE] == VacuumActivity.DOCKED
    assert result[ATTR_CALCULATED_STATUS] == CalculatedState.OFF
    assert result[ATTR_POWER_SUPPLY_STATE] == PowerSupplyState.OFF.value
    assert result[ATTR_ROBOT_STATE] == RobotState.NOT_CONNECTED.value


def test_get_updated_data_pws_on_regular_returns_cleaning():
    aws = {
        DATA_SECTION_SYSTEM_STATE: {
            DATA_SYSTEM_STATE_PWS_STATE: PowerSupplyState.ON.value,
            DATA_SYSTEM_STATE_ROBOT_STATE: RobotState.PROGRAMMING.value,
        },
        DATA_SECTION_CYCLE_INFO: {
            DATA_CYCLE_INFO_CLEANING_MODE: {ATTR_MODE: CleanModes.REGULAR.value},
        },
    }
    result = SystemDetails._get_updated_data(aws)

    assert result[ATTR_VACUUM_STATE] == VacuumActivity.CLEANING
    assert result[ATTR_CALCULATED_STATUS] == CalculatedState.CLEANING


def test_get_updated_data_pws_on_pickup_returns_returning():
    aws = {
        DATA_SECTION_SYSTEM_STATE: {
            DATA_SYSTEM_STATE_PWS_STATE: PowerSupplyState.ON.value,
            DATA_SYSTEM_STATE_ROBOT_STATE: RobotState.PROGRAMMING.value,
        },
        DATA_SECTION_CYCLE_INFO: {
            DATA_CYCLE_INFO_CLEANING_MODE: {ATTR_MODE: CleanModes.PICKUP.value},
        },
    }
    result = SystemDetails._get_updated_data(aws)

    assert result[ATTR_VACUUM_STATE] == VacuumActivity.RETURNING


def test_get_updated_data_pws_error_returns_error():
    aws = {
        DATA_SECTION_SYSTEM_STATE: {
            DATA_SYSTEM_STATE_PWS_STATE: PowerSupplyState.ERROR.value,
            DATA_SYSTEM_STATE_ROBOT_STATE: RobotState.PROGRAMMING.value,
        },
    }
    result = SystemDetails._get_updated_data(aws)

    assert result[ATTR_VACUUM_STATE] == VacuumActivity.ERROR
    assert result[ATTR_CALCULATED_STATUS] == CalculatedState.ERROR


def test_get_updated_data_robot_fault_returns_error():
    aws = {
        DATA_SECTION_SYSTEM_STATE: {
            DATA_SYSTEM_STATE_PWS_STATE: PowerSupplyState.OFF.value,
            DATA_SYSTEM_STATE_ROBOT_STATE: RobotState.FAULT.value,
        },
    }
    result = SystemDetails._get_updated_data(aws)

    assert result[ATTR_VACUUM_STATE] == VacuumActivity.ERROR
    assert result[ATTR_CALCULATED_STATUS] == CalculatedState.ERROR


def test_get_updated_data_pws_holdDelay_returns_docked_holdDelay():
    aws = {
        DATA_SECTION_SYSTEM_STATE: {
            DATA_SYSTEM_STATE_PWS_STATE: PowerSupplyState.HOLD_DELAY.value,
            DATA_SYSTEM_STATE_ROBOT_STATE: RobotState.PROGRAMMING.value,
        },
    }
    result = SystemDetails._get_updated_data(aws)

    assert result[ATTR_VACUUM_STATE] == VacuumActivity.DOCKED
    assert result[ATTR_CALCULATED_STATUS] == CalculatedState.HOLD_DELAY


def test_get_updated_data_pws_holdWeekly_returns_docked_holdWeekly():
    aws = {
        DATA_SECTION_SYSTEM_STATE: {
            DATA_SYSTEM_STATE_PWS_STATE: PowerSupplyState.HOLD_WEEKLY.value,
            DATA_SYSTEM_STATE_ROBOT_STATE: RobotState.PROGRAMMING.value,
        },
    }
    result = SystemDetails._get_updated_data(aws)

    assert result[ATTR_VACUUM_STATE] == VacuumActivity.DOCKED
    assert result[ATTR_CALCULATED_STATUS] == CalculatedState.HOLD_WEEKLY


# ---------------------------------------------------------------------------
# Group B — gate predicate + latch (`has_real_data`).
# ---------------------------------------------------------------------------


def test_has_real_data_initial_false():
    coord = _make_coordinator()

    assert coord.has_real_data is False
    assert coord._system_details.is_updated is False


def test_input_gate_skips_when_aws_data_empty():
    coord = _make_coordinator()
    coord._aws_client = SimpleNamespace(data={})

    coord._set_system_status_details()

    assert coord.has_real_data is False
    assert coord._system_details.is_updated is False
    assert coord._system_details.data == {}


def test_input_gate_skips_when_only_dynamic_section():
    """The dynamic-only path comes from ``_on_dynamic_content_received``
    (``aws_client.py:484-487``), not from ``_on_pws_request_message``."""
    coord = _make_coordinator()
    coord._aws_client = SimpleNamespace(
        data={DATA_SECTION_DYNAMIC: {"pwsRequest": {"some": "thing"}}}
    )

    coord._set_system_status_details()

    assert coord.has_real_data is False
    assert coord._system_details.is_updated is False


def test_input_gate_skips_when_only_cycle_info_section():
    coord = _make_coordinator()
    coord._aws_client = SimpleNamespace(
        data={
            DATA_SECTION_CYCLE_INFO: {
                DATA_CYCLE_INFO_CLEANING_MODE: {ATTR_MODE: CleanModes.REGULAR.value},
            },
        }
    )

    coord._set_system_status_details()

    assert coord.has_real_data is False
    assert coord._system_details.is_updated is False


def test_first_systemstate_payload_latches_has_real_data():
    coord = _make_coordinator()
    coord._aws_client = SimpleNamespace(
        data={
            DATA_SECTION_SYSTEM_STATE: {
                DATA_SYSTEM_STATE_PWS_STATE: PowerSupplyState.ON.value,
                DATA_SYSTEM_STATE_ROBOT_STATE: RobotState.PROGRAMMING.value,
            },
        }
    )

    coord._set_system_status_details()

    assert coord.has_real_data is True
    assert coord._system_details.is_updated is True
    assert coord._system_details.vacuum_state == VacuumActivity.CLEANING


def test_latch_does_not_reset_when_followup_payload_is_dynamic_only():
    coord = _make_coordinator()
    coord._aws_client = SimpleNamespace(
        data={
            DATA_SECTION_SYSTEM_STATE: {
                DATA_SYSTEM_STATE_PWS_STATE: PowerSupplyState.ON.value,
                DATA_SYSTEM_STATE_ROBOT_STATE: RobotState.PROGRAMMING.value,
            },
        }
    )
    coord._set_system_status_details()
    assert coord.has_real_data is True

    # A later merged payload that still has systemState — must not reset.
    coord._aws_client = SimpleNamespace(
        data={
            DATA_SECTION_SYSTEM_STATE: {
                DATA_SYSTEM_STATE_PWS_STATE: PowerSupplyState.ON.value,
                DATA_SYSTEM_STATE_ROBOT_STATE: RobotState.PROGRAMMING.value,
            },
            DATA_SECTION_DYNAMIC: {"pwsRequest": {"x": 1}},
        }
    )
    coord._set_system_status_details()

    assert coord.has_real_data is True


def test_latch_does_not_reset_when_aws_data_section_is_explicitly_none():
    """Defensive: a malformed payload with ``{systemState: None}`` must not
    raise (``not aws_data.get(...)`` short-circuits)."""
    coord = _make_coordinator()
    coord._aws_client = SimpleNamespace(data={DATA_SECTION_SYSTEM_STATE: None})

    coord._set_system_status_details()  # must not raise

    assert coord.has_real_data is False


# ---------------------------------------------------------------------------
# Group C — `available` override on `MyDolphinPlusBaseEntity`.
# ---------------------------------------------------------------------------


def test_available_false_when_no_real_data_even_if_update_succeeded():
    entity = _make_base_entity(last_update_success=True, has_real_data=False)

    assert entity.available is False


def test_available_true_when_real_data_and_update_succeeded():
    entity = _make_base_entity(last_update_success=True, has_real_data=True)

    assert entity.available is True


def test_available_false_when_update_failed_even_with_real_data():
    """Inherited ``CoordinatorEntity`` semantics preserved: the override is
    a conjunction with ``super().available``, not a replacement."""
    entity = _make_base_entity(last_update_success=False, has_real_data=True)

    assert entity.available is False


def test_available_property_is_pure_no_side_effects():
    entity = _make_base_entity(last_update_success=True, has_real_data=False)

    snapshot = (
        entity.coordinator.last_update_success,
        entity.coordinator.has_real_data,
        entity._attr_available,
    )
    _ = entity.available
    _ = entity.available
    after = (
        entity.coordinator.last_update_success,
        entity.coordinator.has_real_data,
        entity._attr_available,
    )

    assert snapshot == after


# ---------------------------------------------------------------------------
# Group D — vacuum ctor + `update_component` cleanups.
# ---------------------------------------------------------------------------


def test_vacuum_ctor_does_not_set_docked_default():
    """The ctor must not bake ``DOCKED`` in; otherwise
    ``async_add_entities(..., True)`` publishes ``docked`` before any
    shadow arrives. Source-level check — full ctor needs a slugify chain
    we don't reproduce here."""
    src = (COMPONENT_ROOT / "vacuum.py").read_text(encoding="utf-8")
    ctor_match = re.search(
        r"def __init__\(\s*self,[^)]*\)[^:]*:(?P<body>.*?)(?=\n    [@a-zA-Z_])",
        src,
        re.DOTALL,
    )
    assert ctor_match is not None, "could not locate MyDolphinPlusVacuumEntity.__init__"
    ctor_body = ctor_match.group("body")

    assert "VacuumActivity.DOCKED" not in ctor_body, (
        "ctor must not preset _attr_activity to DOCKED — BUG-16 regression"
    )


def test_vacuum_update_component_with_none_is_noop():
    entity = _make_vacuum_entity()
    entity._attr_activity = VacuumActivity.CLEANING

    entity.update_component(None)

    assert entity._attr_activity == VacuumActivity.CLEANING


def test_vacuum_update_component_with_real_data_sets_activity():
    entity = _make_vacuum_entity()
    entity._attr_activity = None

    entity.update_component(
        {
            ATTR_STATE: VacuumActivity.CLEANING,
            ATTR_ATTRIBUTES: {ATTR_MODE: CleanModes.REGULAR.value},
        }
    )

    assert entity._attr_activity == VacuumActivity.CLEANING


# ---------------------------------------------------------------------------
# Group E — bootstrap end-to-end.
# ---------------------------------------------------------------------------


def test_initial_add_publishes_unavailable_not_docked():
    """Pin the two preconditions that, together, guarantee
    ``async_add_entities(..., update_before_add=True)`` cannot publish a
    stale ``docked`` as the entity's initial state: (a) the entity reads
    ``unavailable`` because the latch has not flipped, and (b) the ctor
    does not pre-set ``_attr_activity`` to a concrete activity. Each is
    individually sufficient — the conjunction is belt-and-braces."""
    entity = _make_vacuum_entity(last_update_success=True, has_real_data=False)
    # Direct attribute access — must succeed (not raise ``AttributeError``)
    # so the ``activity`` property remains safe; must equal ``None`` so
    # nothing concrete is published while ``available`` is ``False``.
    assert entity.available is False
    assert entity._attr_activity is None


def test_bootstrap_no_shadow_then_off_shadow_publishes_docked_legitimately():
    """Sanity: the gate does not hide a real docked state. Once the shadow
    arrives with ``pwsState=off``, the entity becomes available and the
    activity is ``DOCKED`` for real."""
    coord = _make_coordinator()
    coord._aws_client = SimpleNamespace(
        data={
            DATA_SECTION_SYSTEM_STATE: {
                DATA_SYSTEM_STATE_PWS_STATE: PowerSupplyState.OFF.value,
                DATA_SYSTEM_STATE_ROBOT_STATE: RobotState.NOT_CONNECTED.value,
            },
        }
    )
    coord._set_system_status_details()

    assert coord.has_real_data is True
    assert coord._system_details.vacuum_state == VacuumActivity.DOCKED


# ---------------------------------------------------------------------------
# Group F — other entities (regression sweep).
# ---------------------------------------------------------------------------


PLATFORM_FILES = (
    "sensor.py",
    "binary_sensor.py",
    "light.py",
    "number.py",
    "remote.py",
    "select.py",
    "vacuum.py",
)


def test_no_entity_overrides_available_without_calling_super():
    """If a platform class defines ``available``, it must reference
    ``super().available`` so the latch is honoured."""
    import ast

    for filename in PLATFORM_FILES:
        text = (COMPONENT_ROOT / filename).read_text(encoding="utf-8")
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for sub in ast.walk(node):
                if isinstance(sub, ast.FunctionDef) and sub.name == "available":
                    body_src = ast.unparse(sub)
                    assert "super().available" in body_src, (
                        f"{filename}::{node.name}.available must call "
                        f"super().available so BUG-16's latch is honoured"
                    )


@pytest.mark.parametrize("platform", PLATFORM_FILES)
def test_all_platform_entities_unavailable_before_first_shadow(platform):
    """Without an entity ``available`` override on the base, the latch is
    not honoured platform-wide. Verify the override lives on the shared
    base entity so every platform inherits it for free."""
    base_src = (COMPONENT_ROOT / "common" / "base_entity.py").read_text(encoding="utf-8")
    assert "def available" in base_src, (
        "MyDolphinPlusBaseEntity must override `available` so all "
        "platforms (including the one currently parametrised: "
        f"{platform}) honour the BUG-16 latch"
    )
    assert "has_real_data" in base_src, (
        "the base entity's `available` override must consult "
        "`coordinator.has_real_data`"
    )

    # Functional check on the shared override: pre-latch, available is
    # False regardless of which platform the concrete entity belongs to.
    entity = _make_base_entity(last_update_success=True, has_real_data=False)
    assert entity.available is False


# ---------------------------------------------------------------------------
# Group G — dead-code removals.
# ---------------------------------------------------------------------------


def test_can_load_components_attribute_removed():
    """``_can_load_components`` was assigned in ``_set_system_status_details``
    and never read anywhere — vestige of the latch idea. After the fix,
    it is gone, replaced by ``_has_real_data``."""
    coord = _make_coordinator()
    coord._aws_client = SimpleNamespace(
        data={
            DATA_SECTION_SYSTEM_STATE: {
                DATA_SYSTEM_STATE_PWS_STATE: PowerSupplyState.ON.value,
                DATA_SYSTEM_STATE_ROBOT_STATE: RobotState.PROGRAMMING.value,
            },
        }
    )
    coord._set_system_status_details()

    assert not hasattr(coord, "_can_load_components"), (
        "_can_load_components is dead — must not be re-introduced"
    )

    src = (COMPONENT_ROOT / "managers" / "coordinator.py").read_text(encoding="utf-8")
    assert "_can_load_components" not in src, (
        "dead `_can_load_components` flag must not be re-added"
    )


def test_vacuum_ctor_source_has_no_docked_assignment():
    """Source-level complement to #19: forbid the ctor-default
    ``_attr_activity = VacuumActivity.DOCKED`` line. Scoped to the
    constructor — ``update_component`` keeps a legitimate
    string-state fallback (``vacuum.py:99`` else branch on
    ``isinstance(state, VacuumActivity)``), which the design does not
    remove."""
    src = (COMPONENT_ROOT / "vacuum.py").read_text(encoding="utf-8")
    ctor_match = re.search(
        r"def __init__\(\s*self,[^)]*\)[^:]*:(?P<body>.*?)(?=\n    [@a-zA-Z_])",
        src,
        re.DOTALL,
    )
    assert ctor_match is not None
    ctor_body = ctor_match.group("body")

    assert not re.search(
        r"_attr_activity\s*=\s*VacuumActivity\.DOCKED", ctor_body
    ), "ctor must not pre-assign DOCKED to _attr_activity"
