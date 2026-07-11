"""End-to-end regression test for BUG-27 — the persistent no-op listener.

The existing ``test_bug_27_noop_listener.py`` suite proves that
``initialize`` registers a listener and ``terminate`` releases it, but
it does so against a ``MagicMock(spec=…)`` stub with a hand-rolled
listener dict. That pins the two-line contract in
``coordinator.initialize`` and no more; the reviewer's ``load-bearing``
critique on #131 was that a mocked coordinator cannot prove the actual
regression — a ``DataUpdateCoordinator`` that only reschedules its tick
when at least one listener is present. Reordering ``async_add_listener``
after ``_api.initialize()``, forgetting the guard, or subtly breaking
``_maybe_reconnect`` are all bugs the mock cannot detect.

This module runs the whole chain against a **real**
``MyDolphinPlusCoordinator`` in a live ``hass`` fixture. It proves:

* an initial ``_api.initialize()`` failure leaves the coordinator
  scheduled (persistent listener → ``_schedule_refresh`` is armed);
* the BUG-24 retry deadline is seeded through the normal dispatcher
  path (``_handle_connection_failure`` on FAILED);
* the periodic tick fires ``_maybe_reconnect`` — which fires a second
  ``_api.initialize()`` — as soon as HA's clock crosses
  ``UPDATE_WS_INTERVAL`` and the monotonic clock crosses the retry
  deadline;
* ``terminate`` + ``async_shutdown`` drop the listener, cancel the
  scheduled refresh, and prevent any further reconnect;
* ``initialize`` twice does not stack two persistent listeners.

The monotonic clock used by ``_maybe_reconnect`` /
``_schedule_next_retry`` is replaced module-locally by swapping the
``time`` binding in ``coordinator`` for a ``FakeClock`` — no global
patch of ``time.monotonic`` across the test session, per the guidance
on the parent ticket. Home Assistant's own clock is advanced with
``async_fire_time_changed``; the two clocks are kept in step
explicitly so the tick sees a past-deadline monotonic ``now``.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.mydolphin_plus.common.connectivity_status import (
    ConnectivityStatus,
)
from custom_components.mydolphin_plus.common.consts import (
    DOMAIN,
    SIGNAL_API_STATUS,
    UPDATE_WS_INTERVAL,
)
import custom_components.mydolphin_plus.managers.coordinator as coordinator_module
from custom_components.mydolphin_plus.managers.coordinator import (
    MyDolphinPlusCoordinator,
)
from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
import homeassistant.util.dt as dt_util

# ---------------------------------------------------------------------------
# Test harness — real coordinator + fake clock + minimal stubs
# ---------------------------------------------------------------------------


class FakeClock:
    """Module-local replacement for the monotonic clock used by the coordinator.

    The BUG-24 retry state machine uses ``time.monotonic()`` at three sites:
    ``_schedule_next_retry`` (bump deadline), ``_maybe_reconnect`` (fire when
    ``now >= _next_retry_at``), and ``_async_update_data``'s call into
    ``_maybe_reconnect``. Replacing the module's ``time`` binding with a
    ``SimpleNamespace(monotonic=self.monotonic)`` isolates the fake to this
    test — the standard library ``time`` module remains untouched, so
    unrelated code (aiohttp keepalives, HA's own scheduling helpers) still
    reads a real clock. This is the ``small clock wrapper'' path called out
    on the parent ticket, kept in test-land instead of leaking into
    production DI.
    """

    def __init__(self, start: float = 1_000_000.0) -> None:
        self._t = start

    def monotonic(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


class FakeConfigManager:
    """Bare minimum ConfigManager surface used during the E2E cycle.

    Real ``ConfigManager.initialize`` loads storage, translations, and the
    Fernet-encrypted config data — none of which is exercised by
    ``coordinator.initialize`` beyond the ``entry`` / ``entry_id`` /
    ``name`` accessors and ``entry.options``. Stubbing this out avoids a
    real config-flow round-trip in the test harness while keeping the
    coordinator paths under test truly real.
    """

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
    """Swap the coordinator module's ``time`` reference for the fake clock.

    Restored automatically by ``monkeypatch`` teardown.
    """
    clock = FakeClock()
    monkeypatch.setattr(
        coordinator_module,
        "time",
        SimpleNamespace(monotonic=clock.monotonic),
    )
    return clock


@pytest.fixture
async def wired_coordinator(hass: HomeAssistant, monkeypatch, fake_clock):
    """Build a real ``MyDolphinPlusCoordinator`` with neutralised I/O.

    Returned tuple: ``(coordinator, entry, fake_clock)``.

    * ``_api`` and ``_aws_client`` are replaced with ``MagicMock``s so no
      network I/O happens; their ``.status`` starts at ``NOT_CONNECTED``.
    * ``async_forward_entry_setups`` is neutralised — the platforms are
      irrelevant to the tick / retry chain under test.
    * ``async_start_reauth`` is neutralised on the entry so an accidental
      EXPIRED_TOKEN dispatch would not try to open a config-flow.
    """
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={}, title="Fake Dolphin")
    entry.add_to_hass(hass)

    config_manager = FakeConfigManager(entry)

    # DataUpdateCoordinator resolves `self.config_entry` from the
    # `current_entry` ContextVar when the caller does not pass an explicit
    # `config_entry=`. Coordinator.__init__ does not pass one, so we set
    # the context here for construction and restore it right after.
    token = config_entries.current_entry.set(entry)
    try:
        coord = MyDolphinPlusCoordinator(hass, config_manager)
    finally:
        config_entries.current_entry.reset(token)

    coord._api = MagicMock()
    coord._api.status = ConnectivityStatus.NOT_CONNECTED
    coord._api.data = {}
    coord._api.update = AsyncMock(return_value=None)

    coord._aws_client = MagicMock()
    coord._aws_client.status = ConnectivityStatus.NOT_CONNECTED
    coord._aws_client.data = {}
    coord._aws_client.initialize = AsyncMock(return_value=None)
    coord._aws_client.terminate = AsyncMock(return_value=None)
    coord._aws_client.update = AsyncMock(return_value=None)
    coord._aws_client.update_api_data = AsyncMock(return_value=None)

    monkeypatch.setattr(
        hass.config_entries,
        "async_forward_entry_setups",
        AsyncMock(return_value=None),
    )
    # Belt-and-braces: an unexpected EXPIRED_TOKEN dispatch would otherwise
    # try to open a reauth config-flow, which isn't wired here.
    monkeypatch.setattr(
        MockConfigEntry,
        "async_start_reauth",
        lambda self, hass: None,
    )

    return coord, entry, fake_clock


def _dispatch_api_status(hass: HomeAssistant, entry_id: str, status: ConnectivityStatus):
    async_dispatcher_send(hass, SIGNAL_API_STATUS, entry_id, status)


# ---------------------------------------------------------------------------
# The end-to-end regression
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persistent_listener_drives_retry_after_initial_failure(
    hass: HomeAssistant,
    wired_coordinator,
):
    """The regression BUG-27 exists to prevent.

    Pre-fix: initial ``_api.initialize()`` fails → status goes FAILED →
    no CONNECTED transition → no entities registered → no listeners →
    ``DataUpdateCoordinator`` does not reschedule its tick → the BUG-24
    tick-driven retry never fires → the integration is dormant until
    manual reload.

    Post-fix: the persistent no-op listener registered in
    ``coordinator.initialize`` keeps the tick alive. This test drives
    the whole chain with a real ``DataUpdateCoordinator`` and asserts
    that a second ``_api.initialize()`` fires on the following tick
    once the BUG-24 backoff has elapsed.
    """
    coord, entry, clock = wired_coordinator

    @callback
    def _failing_first_init():
        coord._api.status = ConnectivityStatus.FAILED
        _dispatch_api_status(hass, entry.entry_id, ConnectivityStatus.FAILED)

    coord._api.initialize = AsyncMock(side_effect=_failing_first_init)

    # -----------------------------------------------------------------
    # Step 1 — coordinator.initialize runs; the no-op listener is
    # registered; the first _api.initialize fires and fails FAILED.
    # -----------------------------------------------------------------
    await coord.initialize()
    await hass.async_block_till_done()

    assert coord._no_op_unsub is not None, "persistent listener not registered"
    assert len(coord._listeners) == 1, (
        "expected exactly one listener (the no-op) — "
        f"got {len(coord._listeners)}"
    )
    assert coord._api.initialize.await_count == 1

    # -----------------------------------------------------------------
    # Step 2 — the FAILED dispatch has run _handle_connection_failure,
    # which seeded _next_retry_at through the idempotent
    # _ensure_retry_scheduled helper. The deadline is in the future
    # (fake_clock + 60 s for attempt #1 per the 1 → 2 → 4 → 8 → 15 sequence).
    # -----------------------------------------------------------------
    assert coord._next_retry_at > clock.monotonic(), (
        "retry deadline was not seeded by the FAILED dispatch — "
        "the tick-driven retry would never fire"
    )
    seeded_deadline = coord._next_retry_at

    # -----------------------------------------------------------------
    # Step 3 — advance BOTH clocks past the deadline and past
    # UPDATE_WS_INTERVAL. The two-clock advance is deliberate:
    #   * fake_clock (monotonic) drives _maybe_reconnect's now-vs-deadline
    #     comparison;
    #   * HA's own clock advance triggers the scheduled tick that would
    #     have been silent pre-BUG-27 for lack of listeners.
    # -----------------------------------------------------------------
    clock.advance(120)  # well beyond the 60 s attempt-1 backoff
    async_fire_time_changed(
        hass,
        dt_util.utcnow() + UPDATE_WS_INTERVAL + timedelta(seconds=5),
    )
    await hass.async_block_till_done()

    # -----------------------------------------------------------------
    # Step 4 — the second _api.initialize must have fired via the
    # tick → _maybe_reconnect → _api.initialize() chain.
    # -----------------------------------------------------------------
    assert coord._api.initialize.await_count >= 2, (
        f"_api.initialize was not retried after the deadline — "
        f"await_count={coord._api.initialize.await_count}, "
        f"seeded_deadline={seeded_deadline}, "
        f"clock.now={clock.monotonic()}, "
        f"listeners={len(coord._listeners)}"
    )

    retry_calls_before_shutdown = coord._api.initialize.await_count

    # -----------------------------------------------------------------
    # Step 5 — shutdown. terminate() drops the persistent listener +
    # terminates the AWS client. async_shutdown() cancels the scheduled
    # refresh and closes the debouncer.
    # -----------------------------------------------------------------
    await coord.terminate()
    await coord.async_shutdown()
    await hass.async_block_till_done()

    assert coord._no_op_unsub is None, "unsub handle must be nulled on terminate"
    assert len(coord._listeners) == 0, (
        "listener was not released on terminate — will keep ticking after unload"
    )
    coord._aws_client.terminate.assert_awaited()

    # -----------------------------------------------------------------
    # Step 6 — advance both clocks again; no further tick, no further
    # retry. This is the load-bearing lifecycle assertion: an unloaded
    # coordinator MUST NOT keep firing _api.initialize.
    # -----------------------------------------------------------------
    clock.advance(120)
    async_fire_time_changed(
        hass,
        dt_util.utcnow() + UPDATE_WS_INTERVAL * 3,
    )
    await hass.async_block_till_done()

    assert coord._api.initialize.await_count == retry_calls_before_shutdown, (
        f"_api.initialize was called after shutdown — "
        f"before={retry_calls_before_shutdown}, "
        f"after={coord._api.initialize.await_count}"
    )


# ---------------------------------------------------------------------------
# Idempotence — a second initialize() must not stack a second persistent
# listener. The guard at coordinator.py:400 (`if self._no_op_unsub is None`)
# is what protects against a defensive-double-initialize case; without it a
# reload would leak listeners and terminate() would only release the last one.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_double_initialize_does_not_stack_listeners(
    hass: HomeAssistant,
    wired_coordinator,
):
    """Two consecutive ``initialize()`` calls keep the ``_listeners`` count
    at one and preserve the original unsub handle. Otherwise ``terminate``
    would release only the current listener and orphan the first."""
    coord, entry, _clock = wired_coordinator

    coord._api.initialize = AsyncMock(return_value=None)

    await coord.initialize()
    await hass.async_block_till_done()
    first_unsub = coord._no_op_unsub
    assert first_unsub is not None
    assert len(coord._listeners) == 1

    await coord.initialize()
    await hass.async_block_till_done()

    assert coord._no_op_unsub is first_unsub, (
        "second initialize() overwrote the first unsub — the original "
        "listener is now orphaned"
    )
    assert len(coord._listeners) == 1, (
        "second initialize() stacked another listener — "
        "_listeners will stay non-empty after terminate()"
    )

    await coord.terminate()
    await coord.async_shutdown()
    await hass.async_block_till_done()

    assert coord._no_op_unsub is None
    assert len(coord._listeners) == 0


# ---------------------------------------------------------------------------
# Terminate hygiene — the persistent listener must be released BEFORE the
# AWS client is terminated. On a slow AWS.terminate() network hop, a stale
# listener could otherwise keep the tick alive for a few extra seconds
# after unload.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminate_releases_listener_before_awaiting_aws(
    hass: HomeAssistant,
    wired_coordinator,
):
    """Listener is dropped synchronously before the AWS-client await point."""
    coord, entry, _clock = wired_coordinator

    coord._api.initialize = AsyncMock(return_value=None)

    await coord.initialize()
    await hass.async_block_till_done()
    assert len(coord._listeners) == 1

    aws_snapshot: dict = {}

    async def _slow_aws_terminate():
        aws_snapshot["listeners_at_entry"] = len(coord._listeners)
        aws_snapshot["unsub_at_entry"] = coord._no_op_unsub

    coord._aws_client.terminate = AsyncMock(side_effect=_slow_aws_terminate)

    await coord.terminate()
    await coord.async_shutdown()
    await hass.async_block_till_done()

    assert aws_snapshot["listeners_at_entry"] == 0, (
        "AWS termination was awaited while the persistent listener was "
        "still registered — the tick could still fire during the await"
    )
    assert aws_snapshot["unsub_at_entry"] is None
