"""Regression tests for the one-shot device-ready dispatch.

The coordinator dispatches ``SIGNAL_DEVICE_READY`` exactly once per
lifetime — on the first successful API CONNECTED — and never again on a
reconnect, so Home Assistant never re-adds the already-registered
entities (each re-add logs a ``does not generate unique IDs`` ERROR).
A config-entry reload builds a fresh coordinator, which starts ready to
dispatch again.

All assertions are observable only (dispatcher spy, ``await_count``,
``hasattr``, real-instance state).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mydolphin_plus.common.connectivity_status import (
    ConnectivityStatus,
)
from custom_components.mydolphin_plus.common.consts import DOMAIN, SIGNAL_DEVICE_READY
import custom_components.mydolphin_plus.managers.coordinator as coordinator_module
from custom_components.mydolphin_plus.managers.coordinator import (
    MyDolphinPlusCoordinator,
)
from custom_components.mydolphin_plus.managers.rest_api import RestAPI
from homeassistant import config_entries

ENTRY_ID = "entry-30"


class _FakeConfigManager:
    """Minimal ConfigManager surface used when building a real coordinator."""

    def __init__(self, entry: MockConfigEntry) -> None:
        self._entry = entry
        self.name = "Fake Dolphin"

    @property
    def entry(self) -> MockConfigEntry:
        return self._entry

    @property
    def entry_id(self) -> str:
        return self._entry.entry_id


def _stub(dispatched: bool = False) -> MagicMock:
    """Coordinator stub for driving ``_on_api_status_changed`` directly.

    The compound-connectivity bookkeeping is neutralised so the tests
    observe only the one-shot dispatch behaviour: ``_is_fully_connected``
    returns True (CONNECTED handler takes the reset branch) and the
    failure helpers are awaitable no-ops.
    """
    stub = MagicMock(spec=MyDolphinPlusCoordinator)
    stub.hass = MagicMock()
    stub._config_manager = MagicMock()
    stub._config_manager.entry_id = ENTRY_ID
    stub._device_ready_dispatched = dispatched
    stub.api_data = {"Product Description": "Fake"}
    stub._aws_client = MagicMock()
    stub._aws_client.update_api_data = AsyncMock()
    stub._aws_client.initialize = AsyncMock()
    stub._is_fully_connected = MagicMock(return_value=True)
    stub._ensure_retry_scheduled = MagicMock()
    stub._reconnection_attempts = 0
    stub._next_retry_at = 0.0
    stub._handle_connection_failure = AsyncMock()
    stub._start_reauth_if_needed = AsyncMock()
    return stub


@pytest.fixture
def ready_signals(monkeypatch):
    """Spy every ``async_dispatcher_send`` emitted from the coordinator."""
    calls: list[tuple] = []

    def _spy(hass, signal, *args):
        calls.append((signal, args))

    monkeypatch.setattr(coordinator_module, "async_dispatcher_send", _spy)
    return calls


def _ready_only(calls: list[tuple]) -> list[tuple]:
    return [c for c in calls if c[0] == SIGNAL_DEVICE_READY]


# ---------------------------------------------------------------------------
# 1. Initial connection dispatches the device-ready signal exactly once.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_initial_connected_dispatches_ready_once(ready_signals):
    stub = _stub(dispatched=False)

    await MyDolphinPlusCoordinator._on_api_status_changed(
        stub, ENTRY_ID, ConnectivityStatus.CONNECTED
    )

    ready = _ready_only(ready_signals)
    assert len(ready) == 1
    assert ready[0][1] == (ENTRY_ID,)
    assert stub._device_ready_dispatched is True
    # The AWS cascade still runs on CONNECTED.
    stub._aws_client.update_api_data.assert_awaited_once()
    stub._aws_client.initialize.assert_awaited_once()


# ---------------------------------------------------------------------------
# 2. CONNECTED -> FAILED -> CONNECTED does not dispatch a second time.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconnect_does_not_redispatch(ready_signals):
    stub = _stub(dispatched=False)

    await MyDolphinPlusCoordinator._on_api_status_changed(
        stub, ENTRY_ID, ConnectivityStatus.CONNECTED
    )
    await MyDolphinPlusCoordinator._on_api_status_changed(
        stub, ENTRY_ID, ConnectivityStatus.FAILED
    )
    await MyDolphinPlusCoordinator._on_api_status_changed(
        stub, ENTRY_ID, ConnectivityStatus.CONNECTED
    )

    assert len(_ready_only(ready_signals)) == 1
    # The disconnected transition did NOT re-arm the latch.
    assert stub._device_ready_dispatched is True
    stub._handle_connection_failure.assert_awaited_once()


# ---------------------------------------------------------------------------
# 3. Initial REST failure then recovery dispatches once on first success.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_initial_failure_then_recovery_dispatches_once(ready_signals):
    stub = _stub(dispatched=False)

    # Initial connection fails: no entities, latch stays disarmed.
    await MyDolphinPlusCoordinator._on_api_status_changed(
        stub, ENTRY_ID, ConnectivityStatus.FAILED
    )
    assert _ready_only(ready_signals) == []
    assert stub._device_ready_dispatched is False

    # First successful connection dispatches exactly once.
    await MyDolphinPlusCoordinator._on_api_status_changed(
        stub, ENTRY_ID, ConnectivityStatus.CONNECTED
    )
    assert len(_ready_only(ready_signals)) == 1
    assert stub._device_ready_dispatched is True


# ---------------------------------------------------------------------------
# 4. AWS-only reconnect re-drives the AWS cascade without re-adding entities.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aws_reconnect_reinits_without_redispatch(ready_signals):
    # Entities were dispatched on the first CONNECTED. An AWS-only outage
    # recovers by re-driving the API cascade back to CONNECTED; the
    # handler must re-init AWS but must NOT re-add entities.
    stub = _stub(dispatched=True)

    await MyDolphinPlusCoordinator._on_api_status_changed(
        stub, ENTRY_ID, ConnectivityStatus.CONNECTED
    )

    assert _ready_only(ready_signals) == []
    stub._aws_client.update_api_data.assert_awaited_once()
    stub._aws_client.initialize.assert_awaited_once()


# ---------------------------------------------------------------------------
# 5. A config-entry reload builds a fresh coordinator with the latch reset.
# ---------------------------------------------------------------------------


def test_latch_class_default_is_false():
    # Class-level default so `MagicMock(spec=...)` stubs expose it and,
    # more importantly, so every freshly constructed coordinator starts
    # un-dispatched.
    assert MyDolphinPlusCoordinator._device_ready_dispatched is False


@pytest.mark.asyncio
async def test_reload_creates_fresh_coordinator_with_latch_reset(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={}, title="Fake Dolphin")
    entry.add_to_hass(hass)
    config_manager = _FakeConfigManager(entry)

    token = config_entries.current_entry.set(entry)
    try:
        coordinator = MyDolphinPlusCoordinator(hass, config_manager)
    finally:
        config_entries.current_entry.reset(token)

    # A reload replaces the coordinator instance; the new one is ready to
    # dispatch again on its first CONNECTED.
    assert coordinator._device_ready_dispatched is False


# ---------------------------------------------------------------------------
# 6. `_authenticate_user()` runs once per `RestAPI.initialize()`.
# ---------------------------------------------------------------------------


def test_restapi_has_no_update_method():
    # The connect path authenticates once via _login(); RestAPI exposes
    # no update() method that would authenticate a second time.
    assert not hasattr(RestAPI, "update")


@pytest.mark.asyncio
async def test_authenticate_user_called_once_per_initialize():
    api = RestAPI(None, MagicMock())
    api._config_manager.refresh_token = "refresh-token"
    api._integration_info = MagicMock()
    api._integration_info.initialize = AsyncMock()
    api._local_async_dispatcher_send = MagicMock()
    api._initialize_session = AsyncMock(return_value=True)
    api._ensure_id_token_valid = AsyncMock(return_value=True)
    api._authenticate_user = AsyncMock(return_value=True)

    async def _refresh():
        api._status = ConnectivityStatus.CONNECTED

    api._refresh_aws_credentials = AsyncMock(side_effect=_refresh)

    await api.initialize()

    assert api._authenticate_user.await_count == 1


# ---------------------------------------------------------------------------
# 7. The periodic tick polls AWS and never calls a REST update().
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tick_does_not_call_rest_update_but_updates_aws():
    stub = MagicMock(spec=MyDolphinPlusCoordinator)
    stub._maybe_reconnect = AsyncMock()
    # `spec=RestAPI` has no `update` attribute, so any stray
    # `_api.update()` in the tick would raise AttributeError.
    stub._api = MagicMock(spec=RestAPI)
    stub._api.status = ConnectivityStatus.CONNECTED
    stub._aws_client = MagicMock()
    stub._aws_client.status = ConnectivityStatus.CONNECTED
    stub._aws_client.update = AsyncMock()
    stub._last_update_ws = 0.0
    stub._set_system_status_details = MagicMock()
    stub._refresh_next_scheduled_data = MagicMock()

    result = await MyDolphinPlusCoordinator._async_update_data(stub)

    assert result == {}
    stub._aws_client.update.assert_awaited_once()
    assert not hasattr(stub._api, "update")
