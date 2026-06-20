"""Regression tests for HARD-01.

``_get_power_supply_status_data`` and ``_get_robot_status_data`` used to
call ``.lower()`` on ``self._system_details.power_unit_state`` /
``robot_state`` **before** the ``None if state is None else …`` guard on
the next line. The guard was therefore unreachable: any ``None`` value
raised ``AttributeError`` first, swallowed by the coordinator's outer
``except Exception``, leaving the sensor silently empty.

In the captured shadow payloads (14 sessions) ``pwsState`` / ``robotState``
are always present and string-typed, and both attributes have explicit
defaults at the model layer (``PowerSupplyState.OFF`` /
``RobotState.NOT_CONNECTED``) and at the property layer, so this is a
latent / defensive defect rather than an actively-triggered crash. The
fix simply restores the guard the author placed on purpose, by dropping
the premature ``.lower()``.

The tests below pin both helpers behaviourally on ``None`` and on a
string input.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from homeassistant.const import ATTR_STATE


def test_power_supply_status_handles_none_state():
    from custom_components.mydolphin_plus.managers.coordinator import (
        MyDolphinPlusCoordinator,
    )

    stub = MagicMock(spec=MyDolphinPlusCoordinator)
    stub._system_details = MagicMock()
    stub._system_details.power_unit_state = None

    result = MyDolphinPlusCoordinator._get_power_supply_status_data(stub, None)

    assert result == {ATTR_STATE: None}


def test_robot_status_handles_none_state():
    from custom_components.mydolphin_plus.managers.coordinator import (
        MyDolphinPlusCoordinator,
    )

    stub = MagicMock(spec=MyDolphinPlusCoordinator)
    stub._system_details = MagicMock()
    stub._system_details.robot_state = None

    result = MyDolphinPlusCoordinator._get_robot_status_data(stub, None)

    assert result == {ATTR_STATE: None}


def test_power_supply_status_lowercases_string_state():
    from custom_components.mydolphin_plus.managers.coordinator import (
        MyDolphinPlusCoordinator,
    )

    stub = MagicMock(spec=MyDolphinPlusCoordinator)
    stub._system_details = MagicMock()
    stub._system_details.power_unit_state = "Cleaning"

    result = MyDolphinPlusCoordinator._get_power_supply_status_data(stub, None)

    assert result == {ATTR_STATE: "cleaning"}


def test_robot_status_lowercases_string_state():
    from custom_components.mydolphin_plus.managers.coordinator import (
        MyDolphinPlusCoordinator,
    )

    stub = MagicMock(spec=MyDolphinPlusCoordinator)
    stub._system_details = MagicMock()
    stub._system_details.robot_state = "Idle"

    result = MyDolphinPlusCoordinator._get_robot_status_data(stub, None)

    assert result == {ATTR_STATE: "idle"}
