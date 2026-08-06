"""Regression tests for BUG-27 — the coordinator must keep ticking after
an initial-connection failure.

``DataUpdateCoordinator`` only reschedules its refresh interval when at
least one listener is registered. Our entities register on
``SIGNAL_DEVICE_READY``, which the coordinator dispatches only on the
first successful ``CONNECTED`` transition. If the initial
``_api.initialize()`` fails
(observed in vivo on 2026-07-11 during a Maytronics ``user-svc.b2c.svc``
outage), the integration never reaches CONNECTED, no entities are
added, and the coordinator's tick never runs → the BUG-24 tick-driven
retry never fires → the integration stays dormant until reload.

Fix: register a no-op listener during ``initialize()`` so the tick
keeps running regardless of connection state. Drop the listener on
``terminate()`` for lifecycle hygiene (HA's base class also releases
it via ``async_on_unload(self.async_shutdown)``, so this is
belt-and-braces).

Test strategy — the reviewer's ``load-bearing`` critique: mocking
``async_add_listener`` and asserting it was called only pins the
implementation detail, not the regression. These tests exercise a
faithful harness where the listener registration mutates a real dict
and unsubs actually remove the entry, so any refactor that leaks or
forgets the listener flunks the tests.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.mydolphin_plus.managers.coordinator import (
    MyDolphinPlusCoordinator,
)


def _stub_coordinator_with_listener_bookkeeping():
    """Faithful listener harness.

    ``async_add_listener`` writes into a real ``listeners`` dict and
    returns a real unsub that removes the entry. That way:

    - the ``initialize`` assertions verify that a listener is actually
      *stored* (not just that a method was called),
    - the ``terminate`` assertions verify the unsub is *invoked*
      (not just that a handle was stashed).

    Attributes needed by the methods under test are pre-set on the
    stub. Everything HA-specific is mocked, but the listener mutation
    is real.
    """
    listeners: dict[object, tuple] = {}

    def fake_add_listener(callback, ctx=None):
        key = object()
        listeners[key] = (callback, ctx)

        def unsub():
            listeners.pop(key, None)

        return unsub

    stub = MagicMock(spec=MyDolphinPlusCoordinator)
    stub.async_add_listener = fake_add_listener
    stub._no_op_unsub = None

    stub._build_data_mapping = MagicMock()
    stub._seed_visible_modes = MagicMock()
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

    stub._aws_client = MagicMock()
    stub._aws_client.terminate = AsyncMock()

    return stub, listeners


# ---------------------------------------------------------------------------
# initialize()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_initialize_leaves_at_least_one_listener_on_the_coordinator():
    """After ``initialize`` returns, the coordinator MUST have at least
    one listener registered. Otherwise ``DataUpdateCoordinator`` will
    not reschedule its refresh loop → the BUG-24 tick-driven retry
    (``_maybe_reconnect``) never runs → the integration stays dormant
    on any initial-connection failure."""
    stub, listeners = _stub_coordinator_with_listener_bookkeeping()

    await MyDolphinPlusCoordinator.initialize(stub)

    assert len(listeners) >= 1, (
        "no listener registered — the DataUpdateCoordinator refresh "
        "loop will not tick, and BUG-24's `_maybe_reconnect` never fires"
    )
    assert stub._no_op_unsub is not None, "unsub handle must be stored"


@pytest.mark.asyncio
async def test_initialize_registers_listener_before_api_init():
    """The listener MUST be registered BEFORE ``_api.initialize()`` runs.
    Otherwise a synchronous failure during login would dispatch the
    FAILED status change → ``_handle_connection_failure`` seeds a
    retry → but no listener at that instant means no refresh schedule
    → the tick never runs → the seeded retry never fires.
    Registering after the ``_api.initialize()`` call reopens the exact
    bug this fix closes."""
    stub, listeners = _stub_coordinator_with_listener_bookkeeping()

    call_order: list[str] = []

    original_add_listener = stub.async_add_listener

    def tracking_add_listener(callback, ctx=None):
        call_order.append("async_add_listener")
        return original_add_listener(callback, ctx)

    stub.async_add_listener = tracking_add_listener

    async def _record_api_init():
        call_order.append("_api.initialize")

    stub._api.initialize = AsyncMock(side_effect=_record_api_init)

    await MyDolphinPlusCoordinator.initialize(stub)

    assert call_order.index("async_add_listener") < call_order.index("_api.initialize")


# ---------------------------------------------------------------------------
# terminate() — release the listener on unload for lifecycle hygiene
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminate_releases_the_no_op_listener():
    """Not a correctness requirement (HA base class self-wires
    ``async_on_unload(self.async_shutdown)`` which cancels the
    scheduled refresh regardless), but hygiene: on unload the no-op
    listener should be released so ``_listeners`` returns to empty
    and the coordinator's post-shutdown state matches its
    pre-``initialize`` state."""
    stub, listeners = _stub_coordinator_with_listener_bookkeeping()

    await MyDolphinPlusCoordinator.initialize(stub)
    assert len(listeners) == 1
    assert stub._no_op_unsub is not None

    await MyDolphinPlusCoordinator.terminate(stub)

    assert len(listeners) == 0, "no-op listener still registered after terminate"
    assert stub._no_op_unsub is None, "unsub handle must be nulled on terminate"
    stub._aws_client.terminate.assert_awaited_once()


@pytest.mark.asyncio
async def test_terminate_is_idempotent_on_uninitialised_coordinator():
    """A coordinator whose ``initialize`` never ran (e.g. crash during
    setup) still exposes ``terminate`` — it must not blow up on the
    ``None`` unsub."""
    stub, listeners = _stub_coordinator_with_listener_bookkeeping()
    # No initialize() call — `_no_op_unsub` stays `None`.

    await MyDolphinPlusCoordinator.terminate(stub)

    assert stub._no_op_unsub is None
    stub._aws_client.terminate.assert_awaited_once()


# ---------------------------------------------------------------------------
# Idempotence — a second `initialize()` must not stack another listener
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_initialize_is_idempotent_on_listener_registration():
    """A second `initialize()` call — defensive, not part of the normal
    lifecycle — must NOT register a second listener. Otherwise the
    previous unsub handle is orphaned in `_no_op_unsub`, `terminate()`
    only releases one, and `_listeners` stays non-empty after unload.

    Suggested during the #131 review as a cheap guard against a class
    of lifecycle bugs that would only manifest under unusual reload /
    replay paths."""
    stub, listeners = _stub_coordinator_with_listener_bookkeeping()

    await MyDolphinPlusCoordinator.initialize(stub)
    first_unsub = stub._no_op_unsub
    assert first_unsub is not None
    assert len(listeners) == 1

    await MyDolphinPlusCoordinator.initialize(stub)

    # Same unsub handle preserved, only one listener in the dict.
    assert stub._no_op_unsub is first_unsub, (
        "second initialize() overwrote the first unsub — the original "
        "listener is now orphaned and will never be released"
    )
    assert len(listeners) == 1, (
        "second initialize() stacked another listener — `_listeners` "
        "will stay non-empty after terminate() drops only the current one"
    )

    # And terminate still cleanly releases the single listener.
    await MyDolphinPlusCoordinator.terminate(stub)
    assert len(listeners) == 0
    assert stub._no_op_unsub is None
