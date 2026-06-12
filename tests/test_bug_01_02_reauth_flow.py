"""Tests for BUG-01 and BUG-02 — reauth flow correctness.

BUG-01: ``MyDolphinPlusCoordinator._start_reauth_if_needed`` used to call
``await entry.async_start_reauth(self.hass)``. ``async_start_reauth`` is
synchronous in HA Core (returns ``None``), so the ``await`` raised
``TypeError: object NoneType can't be used in 'await' expression`` which was
swallowed by the surrounding ``except``. The reauth flow itself was still
scheduled (the synchronous call ran before the await), but the side effect
of setting ``_reauth_in_progress = True`` immediately after never ran,
which fed BUG-02.

BUG-02: The ``_reauth_in_progress`` flag was only reset on a
``CONNECTED`` transition. A user dismissing the HA reauth flow without
completing it would leave the flag pinned ``True`` for the rest of the
process lifetime, locking the integration in a permanent retry loop with
no way to re-open the flow. The fix removes the flag entirely: HA Core's
``async_start_reauth`` is already idempotent on its own ``source=reauth``
flow, so we don't need our own guard.

Together these two bugs produced the recurring "lost authentication" symptom
observed on 2026-06-10.
"""

from __future__ import annotations

import inspect
import logging
from pathlib import Path
import re
from unittest.mock import MagicMock

import pytest


def _build_coordinator_stub_with_entry(entry, hass=None):
    """Return a minimal stand-in suitable for calling ``_start_reauth_if_needed``.

    We don't construct a real ``MyDolphinPlusCoordinator`` because its
    constructor wires up dispatchers, debouncers and a full coordinator
    pipeline. The method under test only touches ``self.config_manager.entry``
    and ``self.hass``.
    """
    from custom_components.mydolphin_plus.managers.coordinator import (
        MyDolphinPlusCoordinator,
    )

    stub = MagicMock(spec=MyDolphinPlusCoordinator)
    stub.config_manager = MagicMock()
    stub.config_manager.entry = entry
    stub.hass = hass or MagicMock()
    return stub


@pytest.mark.asyncio
async def test_bug01_start_reauth_does_not_await_synchronous_method(caplog):
    """A synchronous ``async_start_reauth`` (= HA Core reality) must not raise.

    Reverting the BUG-01 fix (re-adding ``await``) would call ``await None``,
    raise ``TypeError`` and log "Failed to start Home Assistant
    reauthentication flow". This test fails on that revert.
    """
    from custom_components.mydolphin_plus.managers import coordinator as coord_module
    from custom_components.mydolphin_plus.managers.coordinator import (
        MyDolphinPlusCoordinator,
    )

    entry = MagicMock()
    # Match HA Core: synchronous method returning None.
    entry.async_start_reauth = MagicMock(return_value=None)
    stub = _build_coordinator_stub_with_entry(entry)

    with caplog.at_level(logging.DEBUG, logger=coord_module.__name__):
        await MyDolphinPlusCoordinator._start_reauth_if_needed(stub)

    entry.async_start_reauth.assert_called_once_with(stub.hass)
    assert not any(
        "Failed to start Home Assistant reauthentication flow" in r.getMessage()
        for r in caplog.records
    ), "the failure branch should not have been entered"
    assert any(
        "Started Home Assistant reauthentication flow" in r.getMessage()
        for r in caplog.records
    ), "the success warning should have been emitted"


@pytest.mark.asyncio
async def test_bug02_repeated_calls_each_trigger_async_start_reauth(caplog):
    """No internal ``_reauth_in_progress`` guard: each call must reach HA Core.

    Reverting BUG-02 (re-introducing the flag) would short-circuit the 2nd
    call, leaving the integration locked if the user dismisses the first
    flow. This test fails on that revert.
    """
    from custom_components.mydolphin_plus.managers.coordinator import (
        MyDolphinPlusCoordinator,
    )

    entry = MagicMock()
    entry.async_start_reauth = MagicMock(return_value=None)
    stub = _build_coordinator_stub_with_entry(entry)

    await MyDolphinPlusCoordinator._start_reauth_if_needed(stub)
    await MyDolphinPlusCoordinator._start_reauth_if_needed(stub)
    await MyDolphinPlusCoordinator._start_reauth_if_needed(stub)

    # Idempotence is HA Core's responsibility, not ours.
    assert entry.async_start_reauth.call_count == 3


@pytest.mark.asyncio
async def test_no_entry_is_handled_silently():
    """If ``config_manager.entry`` is None, the call is a no-op (no crash)."""
    from custom_components.mydolphin_plus.managers.coordinator import (
        MyDolphinPlusCoordinator,
    )

    stub = _build_coordinator_stub_with_entry(entry=None)
    # Must not raise.
    await MyDolphinPlusCoordinator._start_reauth_if_needed(stub)


@pytest.mark.asyncio
async def test_async_start_reauth_exception_is_logged_with_traceback(caplog):
    """If ``async_start_reauth`` raises, log via ``_LOGGER.exception``.

    Captures the full traceback (``record.exc_info is not None``) instead of
    just the str(exc) the original code emitted via ``_LOGGER.error``.
    """
    from custom_components.mydolphin_plus.managers import coordinator as coord_module
    from custom_components.mydolphin_plus.managers.coordinator import (
        MyDolphinPlusCoordinator,
    )

    entry = MagicMock()
    boom = RuntimeError("boom")
    entry.async_start_reauth = MagicMock(side_effect=boom)
    stub = _build_coordinator_stub_with_entry(entry)

    with caplog.at_level(logging.DEBUG, logger=coord_module.__name__):
        await MyDolphinPlusCoordinator._start_reauth_if_needed(stub)

    failure_records = [
        r
        for r in caplog.records
        if "Failed to start Home Assistant reauthentication flow" in r.getMessage()
    ]
    assert len(failure_records) == 1, "expected exactly one failure log line"
    assert failure_records[0].levelno == logging.ERROR
    assert failure_records[0].exc_info is not None, (
        "should use _LOGGER.exception to keep the traceback"
    )


# --- Defense in depth: source-level regression checks ----------------------


def _coordinator_source() -> str:
    from custom_components.mydolphin_plus.managers import coordinator as coord_module

    return Path(inspect.getfile(coord_module)).read_text(encoding="utf-8")


def test_bug01_source_has_no_await_async_start_reauth():
    """A revert that re-adds ``await entry.async_start_reauth`` is forbidden."""
    src = _coordinator_source()
    matches = re.findall(r"await\s+\w+\.async_start_reauth\s*\(", src)
    assert not matches, f"forbidden `await async_start_reauth(...)`: {matches}"


def test_bug02_source_has_no_reauth_in_progress_flag():
    """A revert that re-introduces ``_reauth_in_progress`` as a flag is forbidden."""
    src = _coordinator_source()
    # Strip block comments / docstrings to allow historical mentions there.
    # Conservative: only flag attribute assignments and bare reads.
    forbidden_patterns = [
        re.compile(r"self\._reauth_in_progress\s*=", re.MULTILINE),
        re.compile(r"if\s+self\._reauth_in_progress\b"),
    ]
    found = [p.pattern for p in forbidden_patterns if p.search(src)]
    assert not found, (
        f"forbidden `_reauth_in_progress` usage reintroduced: {found}"
    )
