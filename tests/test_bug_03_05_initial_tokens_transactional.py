"""Tests for BUG-03 and BUG-05 — transactional cleanup of INITIAL_TOKENS_KEY.

BUG-03: ``async_setup_entry`` used to call ``async_update_entry`` AFTER
``update_tokens`` and the serial-number fetches. Any exception in between
left ``INITIAL_TOKENS_KEY`` in ``entry.data`` on disk. On the next HA
restart, the initial (potentially stale) tokens were replayed over the
freshly refreshed storage, producing the recurring "lost authentication"
symptom.

BUG-05: Same window — refresh tokens transit in cleartext through
``entry.data`` (and therefore ``.storage/core.config_entries`` on disk)
between the end of the config flow and the strip. Resolved by the same
fix: strip the key BEFORE persisting tokens to storage.

The fix moves the ``async_update_entry`` call to BEFORE ``update_tokens``:
strip ``INITIAL_TOKENS_KEY`` first, then proceed. If anything raises
afterwards, the key is already gone from ``entry.data`` and won't be
replayed.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from homeassistant.const import CONF_USERNAME


# Realistic-looking fake initial tokens payload.
FAKE_INITIAL_TOKENS = {
    "id-token": "FAKE_ID_TOKEN",
    "refresh-token": "FAKE_REFRESH_TOKEN",
    "id-token-expires-at": 9999999999,
    "serial-number": "SN-1234",
    "motor-unit-serial": "N4720KMV",
}


def _build_entry_with_initial_tokens():
    """Return a MagicMock entry whose ``data`` looks like a fresh config-flow result."""
    from custom_components.mydolphin_plus.common.consts import INITIAL_TOKENS_KEY

    entry = MagicMock()
    entry.entry_id = "test-entry"
    entry.title = "Test"
    entry.data = {
        CONF_USERNAME: "user@example.com",
        INITIAL_TOKENS_KEY: dict(FAKE_INITIAL_TOKENS),
    }
    return entry


def _hass_with_recording_update_entry(entry):
    """Build a hass mock whose async_update_entry mutates ``entry.data`` in place.

    Matches HA Core semantics: ``async_update_entry`` replaces ``entry.data``
    with the dict passed via ``data=``.
    """
    hass = MagicMock()
    hass.is_running = True

    def _update_entry(target_entry, data=None, **_kwargs):
        if data is not None:
            target_entry.data = dict(data)

    hass.config_entries.async_update_entry = MagicMock(side_effect=_update_entry)
    return hass


@pytest.mark.asyncio
async def test_bug03_initial_tokens_stripped_before_update_tokens(monkeypatch):
    """The strip of INITIAL_TOKENS_KEY must happen BEFORE update_tokens.

    Records the order of operations and asserts the strip wins. A revert that
    moves the strip back after ``update_tokens`` fails this test.
    """
    from custom_components.mydolphin_plus import async_setup_entry
    from custom_components.mydolphin_plus.common.consts import INITIAL_TOKENS_KEY
    from custom_components.mydolphin_plus.managers import config_manager as cm_module

    entry = _build_entry_with_initial_tokens()
    hass = _hass_with_recording_update_entry(entry)

    events: list[str] = []

    # Capture the order in which entry.data is mutated vs update_tokens runs.
    original_update_entry_side_effect = hass.config_entries.async_update_entry.side_effect

    def _record_strip(target_entry, data=None, **kwargs):
        events.append("strip")
        return original_update_entry_side_effect(target_entry, data=data, **kwargs)

    hass.config_entries.async_update_entry.side_effect = _record_strip

    fake_cm = MagicMock()
    fake_cm.initialize = AsyncMock()
    fake_cm.update_tokens = AsyncMock(
        side_effect=lambda *a, **k: events.append("update_tokens")
    )
    fake_cm.update_serial_number = AsyncMock(
        side_effect=lambda *a, **k: events.append("update_serial_number")
    )
    fake_cm.update_motor_unit_serial = AsyncMock(
        side_effect=lambda *a, **k: events.append("update_motor_unit_serial")
    )
    fake_cm.is_initialized = False  # short-circuit coordinator init

    monkeypatch.setattr(
        "custom_components.mydolphin_plus.ConfigManager", lambda *a, **k: fake_cm
    )

    await async_setup_entry(hass, entry)

    assert "strip" in events, "strip never happened"
    assert events.index("strip") < events.index("update_tokens"), (
        f"strip must happen before update_tokens, got events: {events}"
    )
    assert INITIAL_TOKENS_KEY not in entry.data, (
        "INITIAL_TOKENS_KEY still in entry.data after setup"
    )


@pytest.mark.asyncio
async def test_bug03_initial_tokens_stripped_even_if_update_tokens_raises(monkeypatch):
    """If update_tokens raises, INITIAL_TOKENS_KEY must already be gone from entry.data.

    This is the actual failure mode: an exception between update_tokens and the
    old strip left the tokens to be replayed on next restart. Now the strip
    runs first, so even a hard crash inside update_tokens leaves entry.data
    clean.
    """
    from custom_components.mydolphin_plus import async_setup_entry
    from custom_components.mydolphin_plus.common.consts import INITIAL_TOKENS_KEY

    entry = _build_entry_with_initial_tokens()
    hass = _hass_with_recording_update_entry(entry)

    fake_cm = MagicMock()
    fake_cm.initialize = AsyncMock()
    fake_cm.update_tokens = AsyncMock(side_effect=RuntimeError("network blip"))
    fake_cm.is_initialized = False

    monkeypatch.setattr(
        "custom_components.mydolphin_plus.ConfigManager", lambda *a, **k: fake_cm
    )

    # The outer except Exception in async_setup_entry swallows the error.
    await async_setup_entry(hass, entry)

    assert INITIAL_TOKENS_KEY not in entry.data, (
        "tokens were left in entry.data after a mid-setup crash — "
        "they would be replayed on the next restart"
    )


@pytest.mark.asyncio
async def test_bug03_no_strip_when_no_initial_tokens_present(monkeypatch):
    """If entry.data has no INITIAL_TOKENS_KEY, no spurious async_update_entry call.

    Prevents a regression where the strip would run on every setup, churning
    .storage unnecessarily.
    """
    from custom_components.mydolphin_plus import async_setup_entry

    entry = MagicMock()
    entry.entry_id = "test-entry"
    entry.data = {CONF_USERNAME: "user@example.com"}
    hass = _hass_with_recording_update_entry(entry)

    fake_cm = MagicMock()
    fake_cm.initialize = AsyncMock()
    fake_cm.update_tokens = AsyncMock()
    fake_cm.is_initialized = False

    monkeypatch.setattr(
        "custom_components.mydolphin_plus.ConfigManager", lambda *a, **k: fake_cm
    )

    await async_setup_entry(hass, entry)

    hass.config_entries.async_update_entry.assert_not_called()


# --- Defense in depth: source-level regression ------------------------------


def _init_source() -> str:
    from custom_components import mydolphin_plus

    return Path(inspect.getfile(mydolphin_plus)).read_text(encoding="utf-8")


def test_bug03_source_strips_before_update_tokens():
    """The strip must lexically precede the `update_tokens` call in async_setup_entry.

    A revert that moves the strip back after the tokens write fails this test.
    """
    src = _init_source()
    strip_match = re.search(
        r"async_update_entry\s*\(\s*entry\s*,\s*data\s*=",
        src,
    )
    tokens_match = re.search(r"await\s+config_manager\.update_tokens\b", src)
    assert strip_match is not None, "no async_update_entry call found"
    assert tokens_match is not None, "no update_tokens call found"
    assert strip_match.start() < tokens_match.start(), (
        "INITIAL_TOKENS_KEY strip must lexically precede update_tokens"
    )
