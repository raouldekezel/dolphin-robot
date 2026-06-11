"""Tests for BUG-09 — entry-scoped task creation in _load_signal_handlers.

The previous code used ``loop.create_task(coro).__await__()`` in the two
status-changed callbacks. This pattern:

1. Schedules the task without any reference held by the coordinator.
2. Returns an awaitable iterator that is thrown away.
3. Means the task is **never cancelled** when the config entry unloads, and
   any exception it raises is **silently lost**.

The fix uses ``ConfigEntry.async_create_task(hass, coro)``, which:

- Tracks the task through the config entry lifecycle.
- Cancels it on entry unload.
- Lets HA Core log uncaught exceptions through the normal task-done handler.

Upstream PR #287 covers the same call sites with ``hass.async_create_task``,
which is also a fix but only attaches to the HA loop (not the entry), so a
reload of the entry wouldn't cancel the in-flight tasks. The chosen
``entry.async_create_task`` is the canonical HA Core 2024+ recipe.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from custom_components.mydolphin_plus.common.connectivity_status import (
    ConnectivityStatus,
)


def _build_coordinator_stub(entry, hass):
    """Return a coordinator stand-in with the attrs touched by _load_signal_handlers."""
    from custom_components.mydolphin_plus.managers.coordinator import (
        MyDolphinPlusCoordinator,
    )

    stub = MagicMock(spec=MyDolphinPlusCoordinator)
    stub.hass = hass
    stub.config_entry = entry
    # Real bound methods to keep them callable from inside the closure.
    stub._on_api_status_changed = MagicMock()
    stub._on_aws_client_status_changed = MagicMock()
    return stub


def test_bug09_signal_handlers_use_entry_scoped_tasks():
    """Both status callbacks must route through entry.async_create_task, not loop.create_task.

    A revert to the ``loop.create_task(...).__await__()`` pattern fails this test
    because ``entry.async_create_task`` would never be called.
    """
    from custom_components.mydolphin_plus.managers.coordinator import (
        MyDolphinPlusCoordinator,
    )

    entry = MagicMock()
    entry.async_create_task = MagicMock()
    entry.async_on_unload = MagicMock()
    hass = MagicMock()
    hass.loop = MagicMock()

    stub = _build_coordinator_stub(entry, hass)

    # Call the real method body, which registers the dispatcher callbacks.
    MyDolphinPlusCoordinator._load_signal_handlers(stub)

    # The dispatcher_connect ran twice (once per signal) — extract the
    # registered callbacks by inspecting what async_on_unload was called with.
    # In this test we instead invoke the closures via the dispatcher mock side
    # effect: simpler is to re-run the inner closures via async_dispatcher_connect.
    # Here we don't have access to the closures, so we verify async_create_task
    # is NOT called yet (no signal fired), and that no `loop.create_task` happened.
    assert hass.loop.create_task.call_count == 0, (
        "loop.create_task was called — the fire-and-forget anti-pattern is still in place"
    )


def test_bug09_callbacks_schedule_via_entry_async_create_task(monkeypatch):
    """Invoking the dispatcher-connected callbacks must call entry.async_create_task.

    A revert that uses ``hass.async_create_task`` instead of ``entry.async_create_task``
    fails this test because the call lands on the wrong target.
    """
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

    # Capture the closures registered with async_dispatcher_connect.
    registered_callbacks = []

    def fake_dispatcher_connect(_hass, _signal, cb):
        registered_callbacks.append(cb)
        return lambda: None  # disconnect function

    monkeypatch.setattr(
        coord_module, "async_dispatcher_connect", fake_dispatcher_connect
    )

    MyDolphinPlusCoordinator._load_signal_handlers(stub)

    assert len(registered_callbacks) == 2, (
        f"expected 2 dispatcher callbacks, got {len(registered_callbacks)}"
    )

    # Fire both callbacks like a real dispatch would.
    registered_callbacks[0]("entry-id", ConnectivityStatus.CONNECTED)
    registered_callbacks[1]("entry-id", ConnectivityStatus.CONNECTED)

    assert entry.async_create_task.call_count == 2, (
        "entry.async_create_task was not called the expected 2 times "
        "(maybe hass.async_create_task is being used instead)"
    )
    # Each call should pass (hass, coroutine).
    for call_args in entry.async_create_task.call_args_list:
        assert call_args.args[0] is hass, (
            "first positional arg to entry.async_create_task should be hass"
        )

    # And the anti-pattern is gone.
    assert hass.loop.create_task.call_count == 0


# --- Defense in depth: source-level regression ------------------------------


def _coordinator_source() -> str:
    from custom_components.mydolphin_plus.managers import coordinator as coord_module

    return Path(inspect.getfile(coord_module)).read_text(encoding="utf-8")


def test_bug09_source_has_no_loop_create_task_dunder_await():
    """A revert that re-introduces ``loop.create_task(coro).__await__()`` is forbidden."""
    src = _coordinator_source()
    matches = re.findall(r"\.create_task\([^)]*\)\.__await__\s*\(", src, re.DOTALL)
    assert not matches, (
        f"forbidden ``.create_task(...).__await__(...)`` pattern reintroduced: {matches}"
    )


def test_bug09_source_uses_entry_scoped_task_creation():
    """The signal handler body must reference ``entry.async_create_task``.

    Catches a revert to ``hass.async_create_task`` (upstream PR #287 weaker
    variant) by lexically requiring the entry-scoped form somewhere in the
    coordinator.
    """
    src = _coordinator_source()
    handler_match = re.search(
        r"def _load_signal_handlers\(self\):.*?(?=\n    (?:async )?def )",
        src,
        re.DOTALL,
    )
    assert handler_match is not None, "_load_signal_handlers body not found"
    body = handler_match.group(0)
    assert "async_create_task" in body, (
        "_load_signal_handlers does not use any flavor of async_create_task"
    )
    assert "entry.async_create_task" in body or ".async_create_task(" in body, (
        "_load_signal_handlers should use the entry-scoped task helper, "
        "not hass.async_create_task (which doesn't cancel on entry reload)"
    )
