"""Tests for BUG-09 — entry-scoped task creation in _load_signal_handlers.

The pre-fix code used ``loop.create_task(coro).__await__()`` in both
status-changed callbacks. That pattern:

1. Schedules the task without any reference held by the coordinator.
2. Returns an awaitable iterator that is thrown away.
3. Leaves the task uncancelled at config entry unload, and any exception
   it raises is silently lost.

The fix uses ``ConfigEntry.async_create_task(hass, coro)``, which ties
the task to the entry lifecycle (cancelled on reload) and routes
uncaught exceptions through HA Core's normal task-done handler.

Upstream PR sh00t2kill#287 covers the same call sites with
``hass.async_create_task``, which is also a fix but only attaches to
the HA loop (not the entry) — a reload of the entry would not cancel
in-flight tasks. ``entry.async_create_task`` is the canonical HA Core
2024+ recipe for entry-scoped dispatcher callbacks.

Tests are behavioural only, per CHORE-02 (#77, PR #80) — no
``inspect.getsource`` / source greps.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.mydolphin_plus.common.connectivity_status import (
    ConnectivityStatus,
)


def _build_coordinator_stub(entry, hass):
    """Coordinator stand-in carrying only what ``_load_signal_handlers`` touches."""
    from custom_components.mydolphin_plus.managers.coordinator import (
        MyDolphinPlusCoordinator,
    )

    stub = MagicMock(spec=MyDolphinPlusCoordinator)
    stub.hass = hass
    stub.config_entry = entry
    stub._on_api_status_changed = MagicMock(name="_on_api_status_changed")
    stub._on_aws_client_status_changed = MagicMock(name="_on_aws_client_status_changed")
    return stub


def test_bug09_registers_two_dispatcher_callbacks():
    """`_load_signal_handlers` must wire both status signals — one for the
    REST API connectivity, one for the AWS client. The fix preserves this
    surface; a regression that loses one would silently disconnect that
    half of the reauth/reconnect plumbing."""
    import custom_components.mydolphin_plus.managers.coordinator as coord_module
    from custom_components.mydolphin_plus.managers.coordinator import (
        MyDolphinPlusCoordinator,
    )

    entry = MagicMock()
    entry.async_create_task = MagicMock()
    entry.async_on_unload = MagicMock()
    hass = MagicMock()
    hass.loop = MagicMock()

    stub = _build_coordinator_stub(entry, hass)

    registered = []

    def fake_dispatcher_connect(_hass, _signal, cb):
        registered.append(cb)
        return lambda: None

    original = coord_module.async_dispatcher_connect
    coord_module.async_dispatcher_connect = fake_dispatcher_connect
    try:
        MyDolphinPlusCoordinator._load_signal_handlers(stub)
    finally:
        coord_module.async_dispatcher_connect = original

    assert len(registered) == 2, (
        f"expected 2 dispatcher callbacks (api + aws), got {len(registered)}"
    )
    # The registration itself must never fire-and-forget — `loop.create_task`
    # may only be called when a dispatch happens later, never during the wire-up.
    assert hass.loop.create_task.call_count == 0


def test_bug09_callbacks_schedule_through_entry_async_create_task():
    """When the dispatched signal fires, both callbacks must schedule their
    coroutine via ``entry.async_create_task(hass, coro)`` — entry-scoped.

    A revert to ``loop.create_task(coro).__await__()`` would leave
    ``entry.async_create_task.call_count == 0`` and bump
    ``hass.loop.create_task.call_count``.

    A regression to ``hass.async_create_task(coro)`` (upstream PR #287's
    weaker variant, no per-entry cancellation) would also leave
    ``entry.async_create_task.call_count == 0`` and would instead bump
    ``hass.async_create_task``."""
    import custom_components.mydolphin_plus.managers.coordinator as coord_module
    from custom_components.mydolphin_plus.managers.coordinator import (
        MyDolphinPlusCoordinator,
    )

    entry = MagicMock()
    entry.async_create_task = MagicMock()
    entry.async_on_unload = MagicMock()
    hass = MagicMock()
    hass.loop = MagicMock()
    hass.async_create_task = MagicMock()

    stub = _build_coordinator_stub(entry, hass)

    registered = []

    def fake_dispatcher_connect(_hass, _signal, cb):
        registered.append(cb)
        return lambda: None

    original = coord_module.async_dispatcher_connect
    coord_module.async_dispatcher_connect = fake_dispatcher_connect
    try:
        MyDolphinPlusCoordinator._load_signal_handlers(stub)
    finally:
        coord_module.async_dispatcher_connect = original

    # Fire both callbacks like a live dispatch would.
    registered[0]("entry-id", ConnectivityStatus.CONNECTED)
    registered[1]("entry-id", ConnectivityStatus.CONNECTED)

    assert entry.async_create_task.call_count == 2, (
        "entry.async_create_task should be called exactly twice "
        "(once per dispatched callback)"
    )
    # Each invocation must pass hass as the first positional arg (HA API contract).
    for call_args in entry.async_create_task.call_args_list:
        assert call_args.args[0] is hass

    # Neither the dropped anti-pattern nor the upstream-weaker variant must fire.
    assert hass.loop.create_task.call_count == 0
    assert hass.async_create_task.call_count == 0


def test_bug09_dispatcher_disconnects_registered_for_unload():
    """`async_dispatcher_connect` returns a disconnect callable; the fix
    must hand it to `entry.async_on_unload` so the dispatcher subscription
    is torn down on entry reload. A regression that forgot to register
    the disconnect would leak stale subscriptions across reloads."""
    import custom_components.mydolphin_plus.managers.coordinator as coord_module
    from custom_components.mydolphin_plus.managers.coordinator import (
        MyDolphinPlusCoordinator,
    )

    entry = MagicMock()
    entry.async_create_task = MagicMock()
    entry.async_on_unload = MagicMock()
    hass = MagicMock()

    stub = _build_coordinator_stub(entry, hass)

    disconnect_api = MagicMock(name="disconnect_api")
    disconnect_aws = MagicMock(name="disconnect_aws")
    returned_disconnects = iter([disconnect_api, disconnect_aws])

    def fake_dispatcher_connect(_hass, _signal, _cb):
        return next(returned_disconnects)

    original = coord_module.async_dispatcher_connect
    coord_module.async_dispatcher_connect = fake_dispatcher_connect
    try:
        MyDolphinPlusCoordinator._load_signal_handlers(stub)
    finally:
        coord_module.async_dispatcher_connect = original

    assert entry.async_on_unload.call_count == 2
    handed = [call.args[0] for call in entry.async_on_unload.call_args_list]
    assert disconnect_api in handed
    assert disconnect_aws in handed
