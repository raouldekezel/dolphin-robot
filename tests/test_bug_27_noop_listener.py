"""Regression test for BUG-27 — the coordinator must keep ticking after
an initial-connection failure.

``DataUpdateCoordinator`` only reschedules its refresh interval when at
least one listener is registered. Our entities register on
``SIGNAL_DEVICE_NEW``, which fires only after a successful
``CONNECTED`` transition. If the initial ``_api.initialize()`` fails
(observed in vivo on 2026-07-11 during a Maytronics ``user-svc.b2c.svc``
outage), the integration never reaches CONNECTED, no entities are
added, and the coordinator's tick never runs → the BUG-24 tick-driven
retry never fires → the integration stays dormant until reload.

Fix: register a no-op listener during ``initialize()`` so the tick
keeps running regardless of connection state.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.mydolphin_plus.managers.coordinator import (
    MyDolphinPlusCoordinator,
)


@pytest.mark.asyncio
async def test_initialize_registers_a_no_op_listener_before_returning():
    """``initialize`` must register a listener with the underlying
    ``DataUpdateCoordinator`` so its refresh loop keeps ticking even
    when no entity has been added yet. Without this, an
    initial-connection failure produces zero retries — verified in
    vivo on 2026-07-11 (11+ minutes of complete silence from
    ``mydolphin_plus`` after a single ``getToken`` failure, while
    every other integration's coordinator kept ticking in the same
    HA)."""
    stub = MagicMock(spec=MyDolphinPlusCoordinator)
    stub._build_data_mapping = MagicMock()
    stub._seed_visible_modes = MagicMock()
    stub.async_add_listener = MagicMock(return_value=MagicMock())
    stub.async_request_refresh = AsyncMock()

    entry = MagicMock()
    entry.entry_id = "test-entry"
    stub.config_manager = MagicMock()
    stub.config_manager.entry = entry

    stub.hass = MagicMock()
    stub.hass.config_entries = MagicMock()
    stub.hass.config_entries.async_forward_entry_setups = AsyncMock()

    stub._api = MagicMock()
    stub._api.initialize = AsyncMock()

    await MyDolphinPlusCoordinator.initialize(stub)

    # Load-bearing assertion — a listener MUST be registered so the
    # DataUpdateCoordinator's refresh loop keeps scheduling itself.
    stub.async_add_listener.assert_called_once()
    # The unsub handle must be stored so `async_shutdown` can drop it.
    assert stub._no_op_unsub is not None


@pytest.mark.asyncio
async def test_initialize_registers_listener_before_api_init():
    """The listener MUST be registered BEFORE ``_api.initialize()`` runs.
    Otherwise a failure during login (BUG-27's exact repro path) would
    dispatch a FAILED status change → ``_handle_connection_failure``
    seeds a retry → but no listener means no reschedule → the tick
    never runs → the retry never fires. Registering after the
    ``_api.initialize()`` call reopens the exact bug this fix closes."""
    stub = MagicMock(spec=MyDolphinPlusCoordinator)
    stub._build_data_mapping = MagicMock()
    stub._seed_visible_modes = MagicMock()
    stub.async_request_refresh = AsyncMock()

    call_order: list[str] = []

    def _record_listener(*_a, **_kw):
        call_order.append("async_add_listener")
        return MagicMock()

    async def _record_api_init():
        call_order.append("_api.initialize")

    stub.async_add_listener = MagicMock(side_effect=_record_listener)

    entry = MagicMock()
    entry.entry_id = "test-entry"
    stub.config_manager = MagicMock()
    stub.config_manager.entry = entry

    stub.hass = MagicMock()
    stub.hass.config_entries = MagicMock()
    stub.hass.config_entries.async_forward_entry_setups = AsyncMock()

    stub._api = MagicMock()
    stub._api.initialize = AsyncMock(side_effect=_record_api_init)

    await MyDolphinPlusCoordinator.initialize(stub)

    assert call_order.index("async_add_listener") < call_order.index("_api.initialize")
