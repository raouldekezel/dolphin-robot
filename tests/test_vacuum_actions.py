"""Tests for vacuum action semantics."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from custom_components.mydolphin_plus.common.calculated_state import CalculatedState
from custom_components.mydolphin_plus.managers.coordinator import (
    MyDolphinPlusCoordinator,
)
from homeassistant.components.vacuum import VacuumActivity


def _bare_coordinator_with(pause_lambda):
    """Build a bare ``__new__`` coordinator with the minimum surface
    needed by the post-HARD-11 `_vacuum_pause` body.

    HARD-11 added an optimistic overlay and a start-serialization guard
    armed inside the pause path, both of which read state that must exist
    on the instance even when no full setup happened.
    """
    coordinator = MyDolphinPlusCoordinator.__new__(MyDolphinPlusCoordinator)
    coordinator._aws_client = SimpleNamespace(pause=pause_lambda)
    coordinator._system_details = SimpleNamespace(
        vacuum_state=VacuumActivity.CLEANING,
        calculated_state=CalculatedState.CLEANING,
    )
    coordinator._has_real_data = True
    coordinator._optimistic_vacuum_state = None
    coordinator._optimistic_statut = None
    coordinator._optimistic_origin_vacuum_state = None
    coordinator._optimistic_deadline = None
    coordinator._pause_issued_at = None
    coordinator._last_observed_calculated_state = None
    coordinator.async_update_listeners = MagicMock()
    return coordinator


@pytest.mark.asyncio
async def test_vacuum_pause_stops_active_robot():
    """Pause should send the power-off command when the robot is active."""
    calls = {"pause": 0}

    coordinator = _bare_coordinator_with(
        lambda: calls.__setitem__("pause", calls["pause"] + 1)
    )

    await coordinator._vacuum_pause(None, VacuumActivity.CLEANING)

    assert calls["pause"] == 1


@pytest.mark.asyncio
async def test_vacuum_pause_ignores_docked_robot():
    """Pause should not send another power-off command while docked."""
    calls = {"pause": 0}

    coordinator = _bare_coordinator_with(
        lambda: calls.__setitem__("pause", calls["pause"] + 1)
    )

    await coordinator._vacuum_pause(None, VacuumActivity.DOCKED)

    assert calls["pause"] == 0
