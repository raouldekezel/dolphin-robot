"""Tests for BUG-10 — preserve traceback on _async_update_data failures.

The previous ``raise UpdateFailed(f"...{err}")`` discarded the original
exception's traceback and never called ``_LOGGER.exception``. The fix uses
``_LOGGER.exception`` and ``raise UpdateFailed(...) from err`` so HA's
DataUpdateCoordinator records the full stack and the chained cause.
"""

from __future__ import annotations

import inspect
import logging
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from homeassistant.helpers.update_coordinator import UpdateFailed


@pytest.mark.asyncio
async def test_bug10_update_failed_preserves_traceback_via_logger_exception(caplog):
    """An exception in the body must be logged with traceback AND chained on UpdateFailed."""
    from custom_components.mydolphin_plus.managers import coordinator as coord_module
    from custom_components.mydolphin_plus.managers.coordinator import (
        MyDolphinPlusCoordinator,
    )

    stub = MagicMock(spec=MyDolphinPlusCoordinator)
    stub._api = MagicMock()
    stub._api.status = "something-that-is-not-CONNECTED"

    # Force the inner body to raise — easiest via _api.status access:
    # the body uses self._api.status == ConnectivityStatus.CONNECTED. We
    # short-circuit by patching the body, which is more precise: we monkey
    # the property check via a tiny side effect that raises.
    boom = RuntimeError("simulated downstream failure")

    def _raise(*_args, **_kwargs):
        raise boom

    # The simplest target inside the try block is _set_system_status_details,
    # but we want to hit the except cleanly regardless of which line raises.
    # Patching _api.status to a property that raises causes the comparison to
    # raise — straight into the except.
    with patch.object(
        type(stub._api), "status", new=property(_raise), create=True
    ), caplog.at_level(logging.ERROR, logger=coord_module.__name__):
        with pytest.raises(UpdateFailed) as exc_info:
            await MyDolphinPlusCoordinator._async_update_data(stub)

    # The UpdateFailed must chain the original exception (PEP 3134).
    assert exc_info.value.__cause__ is boom, (
        "UpdateFailed must use 'raise ... from err' to preserve cause"
    )
    # And the failure log must carry exc_info (full traceback).
    exception_records = [
        r for r in caplog.records if r.levelno == logging.ERROR and r.exc_info
    ]
    assert exception_records, (
        "expected at least one ERROR record with exc_info (from _LOGGER.exception)"
    )
    assert "Error communicating with API" in exception_records[0].getMessage()


def test_bug10_source_uses_raise_from_and_logger_exception():
    """Source-level: forbid the original 'raise UpdateFailed(...)' without 'from'.

    Catches a revert to the silent variant.
    """
    from custom_components.mydolphin_plus.managers import coordinator as coord_module

    src = Path(inspect.getfile(coord_module)).read_text(encoding="utf-8")

    # Find the _async_update_data function body.
    match = re.search(
        r"async def _async_update_data\(self\):.*?(?=\n    (?:async )?def )",
        src,
        re.DOTALL,
    )
    assert match is not None, "_async_update_data not found"
    body = match.group(0)

    # Old anti-pattern: 'raise UpdateFailed(...)' without ' from '.
    bare_raises = re.findall(r"raise\s+UpdateFailed\s*\([^)]*\)(?!\s+from\s)", body)
    assert not bare_raises, (
        f"forbidden bare 'raise UpdateFailed(...)' (must use 'from err'): {bare_raises}"
    )

    # And _LOGGER.exception must be called inside the except.
    assert "_LOGGER.exception" in body, (
        "_async_update_data must log via _LOGGER.exception to preserve the traceback"
    )
