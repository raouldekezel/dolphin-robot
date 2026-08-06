"""Regression tests for issue #137 — setup-abort resource cleanup.

Before this fix, ``async_setup_entry`` in ``__init__.py`` swallowed
``LoginError`` and any other unexpected exception and returned ``False``
without unwinding what the coordinator had already wired. Home Assistant
does **not** invoke ``async_unload_entry`` when ``async_setup_entry``
returns ``False`` (or raises anything other than
``ConfigEntryError`` / ``ConfigEntryAuthFailed`` / ``ConfigEntryNotReady``),
so ``entry.async_on_unload(...)`` callbacks — the two dispatcher subs
registered by ``_load_signal_handlers`` (BUG-09), the self-wired
``DataUpdateCoordinator.async_shutdown`` (HA), and the BUG-27 persistent
no-op listener — all leaked.

The fix follows the pattern Home Assistant expects from a custom
integration:

1. clean up the resources the integration owns directly (drop the BUG-27
   listener + terminate AWS via ``coordinator.terminate()``, remove the
   partially initialised coordinator from ``hass.data``). The REST
   session is *not* touched here — ``RestAPI.terminate()`` is not part
   of the coordinator's teardown surface today; expanding it is out of
   scope for #137;
2. **raise** a public config-entry exception (``ConfigEntryAuthFailed``
   for ``LoginError``, ``ConfigEntryError`` for anything else).

Home Assistant then processes the ``entry.async_on_unload`` list itself
through its supported ``ConfigEntryError`` / ``ConfigEntryAuthFailed``
lifecycle branches. The integration never touches HA's private
``_async_process_on_unload`` — that removes the fragile private-API
coupling the #138 review flagged.

Two axes of coverage:

* **Direct helper exercise** — targeted assertions on
  ``_async_cleanup_failed_setup``: listener released, coordinator
  removed from ``hass.data``, strict idempotence (exact call counts on
  a repeat invocation), safety with ``coordinator=None``, safety when
  ``initialize`` never ran.
* **End-to-end via HA's setup path** — ``coordinator.initialize`` fails
  **late** (the REST session and BUG-27 listener already exist), the
  raised ``ConfigEntryError`` reaches HA's setup driver, HA processes
  ``_async_process_on_unload`` itself, and the entry ends up in the
  ``SETUP_ERROR`` state with a fully unwound coordinator.

The E2E test does not go through ``hass.config_entries.async_setup`` at
the loader level: HA's real loader would import
``custom_components.mydolphin_plus`` from a temporary config directory
that has no ``manifest.json``. Instead the test drives HA's supported
setup path a level down — it calls ``entry.async_setup(hass, ...)``
after registering the integration with a hand-built loader
``Integration`` object. That hits the same public ``ConfigEntryError``
→ ``_async_process_on_unload`` branch the reviewer asked for.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

import custom_components.mydolphin_plus as init_module
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
from homeassistant import config_entries, loader
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError
import homeassistant.util.dt as dt_util

# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class FakeClock:
    """Module-local monotonic replacement — same wrapper the BUG-27 E2E uses."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self._t = start

    def monotonic(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


class FakeConfigManager:
    """Bare minimum ConfigManager surface used during the cleanup cycle."""

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
    """Build a real coordinator with neutralised I/O.

    Mirrors what ``async_setup_entry`` would have stored in ``hass.data``
    once the config-manager phase succeeded.
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
    """A ``hass`` with platform forwarding neutralised."""
    monkeypatch.setattr(
        hass.config_entries,
        "async_forward_entry_setups",
        AsyncMock(return_value=None),
    )
    return hass, fake_clock


# ---------------------------------------------------------------------------
# Direct helper — narrow assertions on what _async_cleanup_failed_setup owns
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cleanup_releases_listener_and_removes_from_hass_data(prepared_hass):
    """Helper contract: terminate the coordinator, drop from ``hass.data``.

    Does **not** assert that the scheduled refresh is cancelled — that
    is HA's responsibility via ``_async_process_on_unload`` after the
    integration raises ``ConfigEntryError``. Cancelling the refresh in
    the helper would drift from the ``clean owned resources only,
    let HA drive the lifecycle'' shape the #138 review asked for.
    """
    hass, _clock = prepared_hass
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={}, title="Fake Dolphin")
    entry.add_to_hass(hass)

    coord = _build_coordinator(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coord

    await coord.initialize()
    await hass.async_block_till_done()

    assert coord._no_op_unsub is not None
    assert len(coord._listeners) == 1

    await _async_cleanup_failed_setup(hass, entry, coord)

    assert coord._no_op_unsub is None, "persistent listener must be released"
    assert len(coord._listeners) == 0, "no listener may remain after terminate()"
    assert hass.data.get(DOMAIN, {}).get(entry.entry_id) is None, (
        "coordinator must be removed from hass.data on setup abort"
    )
    coord._aws_client.terminate.assert_awaited_once()


@pytest.mark.asyncio
async def test_cleanup_is_strictly_idempotent(prepared_hass):
    """A second cleanup call is a strict no-op — exact call counts pinned.

    Idempotence guard: the ``hass.data`` slot is popped in the first
    call, so the second call sees ``still_registered = False`` and
    skips the termination step entirely. ``_aws_client.terminate`` is
    therefore called exactly once, not twice.
    """
    hass, _clock = prepared_hass
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={}, title="Fake Dolphin")
    entry.add_to_hass(hass)
    coord = _build_coordinator(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coord
    await coord.initialize()
    await hass.async_block_till_done()

    await _async_cleanup_failed_setup(hass, entry, coord)
    assert coord._aws_client.terminate.await_count == 1

    await _async_cleanup_failed_setup(hass, entry, coord)

    assert coord._aws_client.terminate.await_count == 1, (
        "second cleanup call must not re-terminate the AWS client"
    )
    assert hass.data.get(DOMAIN, {}).get(entry.entry_id) is None


@pytest.mark.asyncio
async def test_cleanup_safe_when_coordinator_is_none(prepared_hass):
    """A failure before coordinator construction never sees a coordinator."""
    hass, _clock = prepared_hass
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={}, title="Fake Dolphin")
    entry.add_to_hass(hass)

    # Deliberately no coordinator, and no hass.data seed.
    await _async_cleanup_failed_setup(hass, entry, None)

    assert hass.data.get(DOMAIN, {}).get(entry.entry_id) is None


@pytest.mark.asyncio
async def test_cleanup_safe_when_initialize_never_ran(prepared_hass):
    """Coordinator constructed but ``initialize`` never called.

    ``_no_op_unsub`` is still ``None``; ``coordinator.terminate()``
    handles that path (the ``_no_op_unsub is not None`` guard on the
    ``terminate()`` side). The dispatchers registered by
    ``_load_signal_handlers`` in ``__init__`` remain in
    ``entry._on_unload`` — the helper leaves them alone; HA drains
    them via its ``ConfigEntryError`` branch after the raise.
    """
    hass, _clock = prepared_hass
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={}, title="Fake Dolphin")
    entry.add_to_hass(hass)
    coord = _build_coordinator(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coord
    assert coord._no_op_unsub is None

    await _async_cleanup_failed_setup(hass, entry, coord)

    assert hass.data.get(DOMAIN, {}).get(entry.entry_id) is None
    coord._aws_client.terminate.assert_awaited_once()


# ---------------------------------------------------------------------------
# End-to-end — HA's supported setup path takes the raise
# ---------------------------------------------------------------------------


def _install_fake_integration(hass: HomeAssistant, monkeypatch) -> None:
    """Register ``mydolphin_plus`` with HA's loader without touching the config dir.

    HA's real loader looks under ``hass.config.path("custom_components")``
    for a ``manifest.json``. In pytest that directory is a temporary path
    without our integration copied into it. Constructing an
    ``Integration`` pointed at the real repo package (so
    ``async_get_platform`` can still import ``config_flow`` and any
    other referenced platform modules) and monkeypatching
    ``loader.async_get_integration`` to return it lets
    ``entry.async_setup`` reach our real ``async_setup_entry`` through
    HA's supported setup driver — including the
    ``ConfigEntryError`` → ``_async_process_on_unload`` branch under
    test.
    """
    import pathlib

    pkg_path = f"custom_components.{DOMAIN}"
    file_path = pathlib.Path(init_module.__file__).parent
    integration = loader.Integration(
        hass,
        pkg_path,
        file_path,
        {
            "domain": DOMAIN,
            "name": "MyDolphin Plus",
            "codeowners": [],
            "requirements": [],
            "dependencies": [],
            "after_dependencies": [],
            "config_flow": True,
            "documentation": "",
            "iot_class": "cloud_push",
            "integration_type": "device",
            "version": "0.0.0-test",
        },
    )
    monkeypatch.setattr(
        loader,
        "async_get_integration",
        AsyncMock(return_value=integration),
    )

    # HA's ConfigEntry.async_setup calls ``integration.async_get_platform(
    # "config_flow")`` before running our ``async_setup_entry``. Left to
    # its own devices, the loader runs that import in the executor and
    # the resulting ``ModuleNotFoundError`` mask the ``ConfigEntryError``
    # branch we want to test (setup would exit as "Import error" before
    # our code ever runs). Pre-populate the shared platform cache so the
    # loader hits the fast path.
    from custom_components.mydolphin_plus import config_flow as _cf

    hass.data[loader.DATA_COMPONENTS][f"{DOMAIN}.config_flow"] = _cf


@pytest.mark.asyncio
async def test_setup_entry_e2e_raises_config_entry_error_and_cleans_up(
    prepared_hass, monkeypatch
):
    """The whole loop through HA: RestAPI raises late, HA lands SETUP_ERROR.

    Timeline:

    1. HA calls our ``async_setup_entry``.
    2. ``ConfigManager.initialize`` (stubbed) succeeds.
    3. Our ``MyDolphinPlusCoordinator`` is constructed → ``_load_signal_handlers``
       registers two dispatcher subs on ``entry._on_unload``, ``__init__``
       creates its ``RestAPI`` / ``AWSClient`` (patched to inert mocks
       whose ``initialize`` raises).
    4. ``coordinator.initialize`` runs to the point where the BUG-27 no-op
       listener is registered, ``async_forward_entry_setups`` is awaited,
       ``async_request_refresh`` schedules a debounced refresh — **then**
       ``_api.initialize()`` raises ``RuntimeError``.
    5. ``async_setup_entry`` catches, ``_async_cleanup_failed_setup`` runs
       (terminates the coordinator + drops from ``hass.data``), and the
       function re-raises ``ConfigEntryError``.
    6. HA catches ``ConfigEntryError``, awaits ``_async_process_on_unload``
       (fires ``async_shutdown`` + dispatcher unsubs + drains tasks), then
       sets state to ``SETUP_ERROR``.

    Post-conditions: state is ``SETUP_ERROR``, coordinator absent from
    ``hass.data``, dispatcher list drained by HA, no retry when time
    advances.
    """
    hass, clock = prepared_hass
    _install_fake_integration(hass, monkeypatch)

    # --- Neutralise the ConfigManager phase ----------------------------
    fake_config_manager = MagicMock()
    fake_config_manager.is_initialized = True
    fake_config_manager.initialize = AsyncMock(return_value=None)

    monkeypatch.setattr(
        init_module,
        "ConfigManager",
        MagicMock(return_value=fake_config_manager),
    )

    # --- Late-raising RestAPI, inert AWSClient -------------------------
    # Swap the classes at the coordinator-module level so
    # MyDolphinPlusCoordinator.__init__ picks up our fakes when it
    # constructs them (rather than replacing coord._api after the fact,
    # which is what the reviewer flagged as an ``abort too early'' test).
    api_captured: dict = {}
    aws_captured: dict = {}

    class _LateRaisingRestAPI:
        status = ConnectivityStatus.NOT_CONNECTED
        data: dict = {}

        def __init__(self, hass_, config_manager_):
            api_captured["instance"] = self
            self._terminate_calls = 0
            self._initialize_calls = 0

        async def initialize(self):
            # Late failure — coord.initialize has already registered the
            # BUG-27 listener and scheduled the refresh by the time we
            # get here.
            self._initialize_calls += 1
            raise RuntimeError("Simulated late REST initialize failure")

        async def update(self):
            return None

        async def terminate(self):
            self._terminate_calls += 1

    class _InertAWSClient:
        status = ConnectivityStatus.NOT_CONNECTED
        data: dict = {}

        def __init__(self, hass_, config_manager_, on_mqtt_update):
            aws_captured["instance"] = self
            self._terminate_calls = 0

        async def initialize(self):
            return None

        async def terminate(self):
            self._terminate_calls += 1

        async def update(self):
            return None

        async def update_api_data(self, _):
            return None

    monkeypatch.setattr(coordinator_module, "RestAPI", _LateRaisingRestAPI)
    monkeypatch.setattr(coordinator_module, "AWSClient", _InertAWSClient)

    # --- Build entry and run HA's supported setup driver ---------------
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        options={},
        title="Fake Dolphin",
    )
    entry.add_to_hass(hass)

    # HA's ConfigEntry.async_setup owns the ``ConfigEntryError`` →
    # ``_async_process_on_unload`` → ``SETUP_ERROR`` branch. Reaching
    # via ``hass.config_entries.async_setup(entry_id)`` would require
    # HA's loader to find the manifest under a temp config dir; the
    # ``_install_fake_integration`` helper primes the loader cache so
    # the same lifecycle runs through ``entry.async_setup`` directly.
    async with entry.setup_lock:
        await entry.async_setup(hass)

    assert entry.state is ConfigEntryState.SETUP_ERROR, (
        f"expected SETUP_ERROR after ConfigEntryError raise — got {entry.state}"
    )
    assert entry.reason and "MyDolphin" in entry.reason

    assert hass.data.get(DOMAIN, {}).get(entry.entry_id) is None, (
        "coordinator must be removed from hass.data"
    )
    # coordinator.terminate() (called by _async_cleanup_failed_setup) drops
    # the BUG-27 listener and terminates the AWS client. It does NOT reach
    # into ``_api.terminate()`` — the pre-existing lifecycle only closes
    # the REST session on entry removal, not on unload/abort. Keeping the
    # narrow assertion here rather than expanding the coordinator's
    # terminate surface as a side effect of #137.
    assert aws_captured["instance"]._terminate_calls == 1, (
        "AWSClient.terminate must be called exactly once (by coordinator.terminate)"
    )
    # HA processes on_unload on the ConfigEntryError branch — the entry's
    # on_unload list must be drained (the dispatcher subs registered by
    # ``_load_signal_handlers`` are among them).
    assert not entry._on_unload, (
        "HA must drain entry.async_on_unload callbacks after ConfigEntryError; "
        "found "
        + repr(entry._on_unload)
    )

    # Advance both clocks; no ghost retry can fire. The reviewer note
    # on the initial revision was correct — asserting only that the
    # coordinator is absent from ``hass.data`` after the advance is
    # tautological (that was already true before the advance). The
    # load-bearing assertion is that ``_api.initialize`` is not called
    # again: the coordinator was shut down by HA's on_unload processing
    # of the self-wired ``DataUpdateCoordinator.async_shutdown``, so
    # no scheduled tick remains to fire ``_maybe_reconnect`` →
    # ``_api.initialize``.
    api_calls_before_advance = api_captured["instance"]._initialize_calls
    clock.advance(120)
    async_fire_time_changed(
        hass,
        dt_util.utcnow() + UPDATE_WS_INTERVAL * 3,
    )
    await hass.async_block_till_done()

    assert api_captured["instance"]._initialize_calls == api_calls_before_advance, (
        "ghost retry after cleanup — _api.initialize was called after "
        "async_setup_entry aborted; HA's on_unload path failed to cancel "
        "the scheduled refresh"
    )
    assert hass.data.get(DOMAIN, {}).get(entry.entry_id) is None


# ---------------------------------------------------------------------------
# Direct exception classification — LoginError → ConfigEntryAuthFailed,
# generic Exception → ConfigEntryError. Verifies the raise path itself
# without going through HA's setup driver.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_error_raises_config_entry_auth_failed_and_cleans_up(
    prepared_hass, monkeypatch
):
    """LoginError must be re-raised as ``ConfigEntryAuthFailed`` so HA opens
    the reauth flow — the previous silent-return behaviour left the entry
    in a stale loaded=False state without any user-visible signal."""
    from custom_components.mydolphin_plus.models.exceptions import LoginError
    from homeassistant.exceptions import ConfigEntryAuthFailed

    hass, _clock = prepared_hass

    # Force a LoginError from ConfigManager.initialize — the cheapest hook.
    fake_config_manager = MagicMock()
    fake_config_manager.is_initialized = False
    fake_config_manager.initialize = AsyncMock(
        side_effect=LoginError("stub refresh token rejected")
    )
    monkeypatch.setattr(
        init_module,
        "ConfigManager",
        MagicMock(return_value=fake_config_manager),
    )

    entry = MockConfigEntry(domain=DOMAIN, data={}, options={}, title="Fake Dolphin")
    entry.add_to_hass(hass)

    with pytest.raises(ConfigEntryAuthFailed):
        await async_setup_entry(hass, entry)

    assert hass.data.get(DOMAIN, {}).get(entry.entry_id) is None


@pytest.mark.asyncio
async def test_unclassified_exception_raises_config_entry_error_and_cleans_up(
    prepared_hass, monkeypatch
):
    """A raise that doesn't match ``LoginError`` (or any other classified
    branch) is re-raised as ``ConfigEntryError`` so HA marks the entry
    ``SETUP_ERROR`` and processes on_unload through its supported
    lifecycle rather than the leaky return-False path."""
    hass, _clock = prepared_hass

    fake_config_manager = MagicMock()
    fake_config_manager.is_initialized = False
    fake_config_manager.initialize = AsyncMock(
        side_effect=RuntimeError("Simulated late failure inside ConfigManager")
    )
    monkeypatch.setattr(
        init_module,
        "ConfigManager",
        MagicMock(return_value=fake_config_manager),
    )

    entry = MockConfigEntry(domain=DOMAIN, data={}, options={}, title="Fake Dolphin")
    entry.add_to_hass(hass)

    with pytest.raises(ConfigEntryError):
        await async_setup_entry(hass, entry)

    assert hass.data.get(DOMAIN, {}).get(entry.entry_id) is None
