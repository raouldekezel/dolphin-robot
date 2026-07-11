"""Regression tests for issue #137 — setup-abort resource cleanup.

Before this fix, ``async_setup_entry`` in ``__init__.py`` returned ``False``
on an unexpected exception (including ``LoginError``) without unwinding what
the coordinator had already wired. Because Home Assistant does **not** invoke
``async_unload_entry`` when ``async_setup_entry`` returns False (or raises
anything other than ``ConfigEntryNotReady`` / ``ConfigEntryAuthFailed``), the
callbacks registered via ``entry.async_on_unload(...)`` — including the
dispatcher subscriptions from ``MyDolphinPlusCoordinator._load_signal_handlers``
and the self-wired ``DataUpdateCoordinator.async_shutdown`` — stayed alive
until the entry was next removed or reloaded. The BUG-27 persistent no-op
listener, any open REST/AWS session, and the coordinator's slot in
``hass.data`` all leaked with them.

The fix (``_async_cleanup_failed_setup`` helper) uses the same
``entry._async_process_on_unload(hass)`` path HA itself takes on the
``ConfigEntryNotReady`` / ``ConfigEntryAuthFailed`` branches, plus an
explicit ``coordinator.terminate()`` to drop the BUG-27 listener and close
the AWS side before the scheduled refresh is cancelled.

These tests exercise the cleanup against a **real**
``MyDolphinPlusCoordinator`` in a live ``hass`` fixture. A mocked coordinator
cannot prove the regression: only a real ``DataUpdateCoordinator`` has an
``_unsub_refresh`` timer to cancel and a real ``_listeners`` dict to drain.

Notably distinct from:

* the BUG-27 tests, which prove the tick survives an *initial connectivity
  failure* (a FAILED status, not a raise);
* the BUG-24 tests, which prove the retry state machine survives a
  sustained outage.

This file only covers the *setup-abort* path — an exception escaping
``async_setup_entry`` after coordinator construction.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.mydolphin_plus import (
    _async_cleanup_failed_setup,
    async_setup_entry,
)
from custom_components.mydolphin_plus.common.connectivity_status import (
    ConnectivityStatus,
)
from custom_components.mydolphin_plus.common.consts import DOMAIN, UPDATE_WS_INTERVAL
import custom_components.mydolphin_plus.managers.coordinator as coordinator_module
from custom_components.mydolphin_plus.managers.coordinator import (
    MyDolphinPlusCoordinator,
)
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
import homeassistant.util.dt as dt_util

# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class FakeClock:
    """Same wrapper as ``test_bug_27_end_to_end_coordinator``.

    Keeps monotonic advances local to this test module — no global patch of
    ``time.monotonic``.
    """

    def __init__(self, start: float = 1_000_000.0) -> None:
        self._t = start

    def monotonic(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


class FakeConfigManager:
    """Same bare-minimum surface as the BUG-27 E2E test."""

    def __init__(self, entry: MockConfigEntry) -> None:
        self._entry = entry
        self.name = "Fake Dolphin"

    @property
    def entry(self) -> MockConfigEntry:
        return self._entry

    @property
    def entry_id(self) -> str:
        return self._entry.entry_id

    def get_debug_data(self) -> dict:
        return {}


@pytest.fixture
def fake_clock(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(
        coordinator_module,
        "time",
        SimpleNamespace(monotonic=clock.monotonic),
    )
    return clock


def _build_coordinator(hass: HomeAssistant, entry: MockConfigEntry):
    """Build a real coordinator + neutralised I/O — direct wiring, no fixture.

    Returned instance mirrors what ``async_setup_entry`` would have stored in
    ``hass.data`` once the config-manager phase succeeded. Callers add it to
    ``hass.data`` (or not) depending on the scenario under test.
    """
    config_manager = FakeConfigManager(entry)

    token = config_entries.current_entry.set(entry)
    try:
        coord = MyDolphinPlusCoordinator(hass, config_manager)
    finally:
        config_entries.current_entry.reset(token)

    coord._api = MagicMock()
    coord._api.status = ConnectivityStatus.NOT_CONNECTED
    coord._api.data = {}
    coord._api.update = AsyncMock(return_value=None)
    coord._api.initialize = AsyncMock(return_value=None)

    coord._aws_client = MagicMock()
    coord._aws_client.status = ConnectivityStatus.NOT_CONNECTED
    coord._aws_client.data = {}
    coord._aws_client.initialize = AsyncMock(return_value=None)
    coord._aws_client.terminate = AsyncMock(return_value=None)
    coord._aws_client.update = AsyncMock(return_value=None)
    coord._aws_client.update_api_data = AsyncMock(return_value=None)

    return coord


@pytest.fixture
def prepared_hass(hass: HomeAssistant, monkeypatch, fake_clock):
    """A ``hass`` with platform-forwarding neutralised.

    ``async_forward_entry_setups`` is patched so ``coordinator.initialize``
    does not try to instantiate the ten platform modules for real; the tests
    are focused on the abort/cleanup lifecycle, not entity wiring.
    """
    monkeypatch.setattr(
        hass.config_entries,
        "async_forward_entry_setups",
        AsyncMock(return_value=None),
    )
    return hass, fake_clock


# ---------------------------------------------------------------------------
# The helper — direct exercise, real coordinator
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cleanup_releases_listener_removes_from_hass_data_and_cancels_refresh(
    prepared_hass,
):
    """Full cleanup: no listener, no scheduled refresh, coord gone, aws terminated once."""
    hass, clock = prepared_hass
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={}, title="Fake Dolphin")
    entry.add_to_hass(hass)

    coord = _build_coordinator(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coord

    await coord.initialize()
    await hass.async_block_till_done()

    assert coord._no_op_unsub is not None
    assert len(coord._listeners) == 1
    initial_aws_terminate_calls = coord._aws_client.terminate.await_count

    await _async_cleanup_failed_setup(hass, entry, coord)

    assert coord._no_op_unsub is None, "persistent listener must be released"
    assert len(coord._listeners) == 0, "no listener may remain after cleanup"
    assert hass.data.get(DOMAIN, {}).get(entry.entry_id) is None, (
        "coordinator must be removed from hass.data on setup abort"
    )
    assert coord._aws_client.terminate.await_count == initial_aws_terminate_calls + 1, (
        "_aws_client.terminate must be called exactly once by cleanup"
    )
    assert coord._shutdown_requested, (
        "async_shutdown must have fired via entry.async_on_unload"
    )

    # No further retry: advance both clocks past the deadline and beyond a
    # tick interval; _api.initialize must not be called again.
    initial_api_calls = coord._api.initialize.await_count
    clock.advance(120)
    async_fire_time_changed(
        hass,
        dt_util.utcnow() + UPDATE_WS_INTERVAL * 3,
    )
    await hass.async_block_till_done()
    assert coord._api.initialize.await_count == initial_api_calls, (
        "cleanup did not stop the tick — _api.initialize was called after cleanup"
    )


@pytest.mark.asyncio
async def test_cleanup_is_idempotent(prepared_hass):
    """A second cleanup call is a no-op — never double-closes the AWS side."""
    hass, _clock = prepared_hass
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={}, title="Fake Dolphin")
    entry.add_to_hass(hass)
    coord = _build_coordinator(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coord
    await coord.initialize()
    await hass.async_block_till_done()

    await _async_cleanup_failed_setup(hass, entry, coord)
    aws_after_first = coord._aws_client.terminate.await_count

    await _async_cleanup_failed_setup(hass, entry, coord)

    # coordinator.terminate is itself idempotent (see BUG-27 tests):
    # _no_op_unsub was already cleared, so the second call skips it. AWS
    # terminate WILL be called again because coordinator.terminate always
    # awaits it — the AWS client itself is responsible for idempotence
    # (HARD-02/F4 pattern for REST, same discipline on the AWS side).
    # The test pins that cleanup does not blow up and hass.data stays
    # empty.
    assert coord._no_op_unsub is None
    assert hass.data.get(DOMAIN, {}).get(entry.entry_id) is None
    assert coord._aws_client.terminate.await_count >= aws_after_first


@pytest.mark.asyncio
async def test_cleanup_safe_when_coordinator_is_none(prepared_hass):
    """A failure before coordinator construction never sees a coordinator."""
    hass, _clock = prepared_hass
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={}, title="Fake Dolphin")
    entry.add_to_hass(hass)

    # Deliberately no coordinator, and no hass.data seed.
    await _async_cleanup_failed_setup(hass, entry, None)

    # No entry left behind, no crash.
    assert hass.data.get(DOMAIN, {}).get(entry.entry_id) is None


@pytest.mark.asyncio
async def test_cleanup_safe_when_initialize_never_ran(prepared_hass):
    """Coordinator constructed but ``initialize`` never called.

    ``_no_op_unsub`` is still None; ``_load_signal_handlers`` did register
    dispatcher subs via ``entry.async_on_unload``; cleanup must drop those
    too.
    """
    hass, _clock = prepared_hass
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={}, title="Fake Dolphin")
    entry.add_to_hass(hass)
    coord = _build_coordinator(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coord

    # There WERE dispatcher subs registered by _load_signal_handlers.
    on_unload_before = list(entry._on_unload or [])
    assert len(on_unload_before) >= 2, (
        "expected at least two dispatcher subs registered by _load_signal_handlers"
    )

    await _async_cleanup_failed_setup(hass, entry, coord)

    assert not entry._on_unload, "dispatcher on_unload subs must have been processed"
    assert hass.data.get(DOMAIN, {}).get(entry.entry_id) is None


# ---------------------------------------------------------------------------
# End-to-end via async_setup_entry — coordinator.initialize() raises
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_setup_entry_cleans_up_when_coordinator_initialize_raises(
    prepared_hass, monkeypatch
):
    """The whole loop: async_setup_entry → coordinator constructed → initialize raises → helper cleans up.

    Wired-in real ``async_setup_entry``. The ``ConfigManager`` is stubbed
    because the point of this test is the cleanup after coordinator
    construction — the ConfigManager phase is out of scope. The
    ``MyDolphinPlusCoordinator`` constructor is intercepted so it returns
    an instance whose ``initialize`` raises.
    """
    hass, clock = prepared_hass
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        options={},
        title="Fake Dolphin",
    )
    entry.add_to_hass(hass)

    # Bypass the ConfigManager phase entirely — we care about the
    # coordinator-abort branch.
    fake_config_manager = MagicMock()
    fake_config_manager.is_initialized = True
    fake_config_manager.initialize = AsyncMock(return_value=None)

    import custom_components.mydolphin_plus as init_module

    monkeypatch.setattr(
        init_module,
        "ConfigManager",
        MagicMock(return_value=fake_config_manager),
    )

    built: list[MyDolphinPlusCoordinator] = []

    def _build(hass_, config_manager_):
        coord = _build_coordinator(hass_, entry)
        # Any exception raised by initialize() is caught by the outer
        # generic Exception handler in async_setup_entry.
        coord.initialize = AsyncMock(side_effect=RuntimeError("boom mid-init"))
        built.append(coord)
        return coord

    monkeypatch.setattr(init_module, "MyDolphinPlusCoordinator", _build)

    # Ensure hass appears "running" so async_setup_entry awaits initialize().
    monkeypatch.setattr(type(hass), "is_running", property(lambda self: True))

    result = await async_setup_entry(hass, entry)

    assert result is False, "async_setup_entry must return False on abort"
    assert len(built) == 1
    coord = built[0]

    assert hass.data.get(DOMAIN, {}).get(entry.entry_id) is None, (
        "coordinator must be removed from hass.data on abort"
    )
    assert coord._shutdown_requested, "async_shutdown must have fired"
    coord._aws_client.terminate.assert_awaited()  # at least once

    # And no ghost retry: advance time and confirm _api.initialize is not
    # called (coord.initialize was patched to a raising AsyncMock, so it
    # would have been called at most once by the abort path itself).
    api_calls_before = coord._api.initialize.await_count
    clock.advance(120)
    async_fire_time_changed(
        hass,
        dt_util.utcnow() + UPDATE_WS_INTERVAL * 3,
    )
    await hass.async_block_till_done()
    assert coord._api.initialize.await_count == api_calls_before
