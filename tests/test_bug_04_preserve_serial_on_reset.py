"""Tests for BUG-04 — preserve serial_number across reset_login_details.

BUG-04: ``ConfigManager.reset_login_details`` used to wipe
``STORAGE_DATA_SERIAL_NUMBER`` to ``None`` along with all other
``TOKEN_PARAMS`` (only ``motor_unit_serial`` was preserved). The
``DeviceInfo.identifiers`` tuple uses ``serial_number``, so during the
window between the reset and a successful reauth, the identifiers became
``(DEFAULT_NAME, None)``. Any reload of the entry in that window orphaned
every entity attached to the device, forcing the user to manually re-link.

The fix preserves both ``motor_unit_serial`` and ``serial_number`` —
they are robot identity, not authentication state.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_bug04_reset_login_details_preserves_serial_number():
    """After reset_login_details, serial_number must still be present.

    A revert that drops STORAGE_DATA_SERIAL_NUMBER from the preserved set
    fails this test.
    """
    from custom_components.mydolphin_plus.common.consts import (
        STORAGE_DATA_ID_TOKEN,
        STORAGE_DATA_MOTOR_UNIT_SERIAL,
        STORAGE_DATA_REFRESH_TOKEN,
        STORAGE_DATA_SERIAL_NUMBER,
    )
    from custom_components.mydolphin_plus.managers.config_manager import ConfigManager

    cm = ConfigManager(MagicMock(), MagicMock())
    cm._save = AsyncMock()
    cm._data = {
        STORAGE_DATA_ID_TOKEN: "id-token-value",
        STORAGE_DATA_REFRESH_TOKEN: "refresh-token-value",
        STORAGE_DATA_SERIAL_NUMBER: "SN-1234",
        STORAGE_DATA_MOTOR_UNIT_SERIAL: "N4720KMV",
    }

    await cm.reset_login_details()

    # Tokens cleared.
    assert cm._data[STORAGE_DATA_ID_TOKEN] is None
    assert cm._data[STORAGE_DATA_REFRESH_TOKEN] is None
    # Both identity fields preserved.
    assert cm._data[STORAGE_DATA_SERIAL_NUMBER] == "SN-1234", (
        "serial_number was wiped — entities would be orphaned"
    )
    assert cm._data[STORAGE_DATA_MOTOR_UNIT_SERIAL] == "N4720KMV", (
        "motor_unit_serial was wiped (regression — was preserved before)"
    )
    cm._save.assert_awaited_once()


@pytest.mark.asyncio
async def test_bug04_reset_login_details_still_clears_real_tokens():
    """Sanity check: tokens that should be cleared are still cleared.

    A bug-04 fix that accidentally added too many keys to the preserved
    set (e.g. id-token) fails this test.
    """
    from custom_components.mydolphin_plus.common.consts import (
        STORAGE_DATA_ID_TOKEN,
        STORAGE_DATA_ID_TOKEN_EXPIRES_AT,
        STORAGE_DATA_LAST_TOKEN_FETCH,
        STORAGE_DATA_REFRESH_TOKEN,
        TOKEN_PARAMS,
    )
    from custom_components.mydolphin_plus.managers.config_manager import ConfigManager

    cm = ConfigManager(MagicMock(), MagicMock())
    cm._save = AsyncMock()
    cm._data = {p: f"value-{p}" for p in TOKEN_PARAMS}

    await cm.reset_login_details()

    must_be_cleared = [
        STORAGE_DATA_ID_TOKEN,
        STORAGE_DATA_REFRESH_TOKEN,
        STORAGE_DATA_ID_TOKEN_EXPIRES_AT,
        STORAGE_DATA_LAST_TOKEN_FETCH,
    ]
    for key in must_be_cleared:
        if key in cm._data:
            assert cm._data[key] is None, (
                f"{key} was preserved but it carries authentication state"
            )


# --- Defense in depth: source-level regression ------------------------------


def _config_manager_source() -> str:
    from custom_components.mydolphin_plus.managers import config_manager as cm_module

    return Path(inspect.getfile(cm_module)).read_text(encoding="utf-8")


def test_bug04_source_preserves_serial_number_on_reset():
    """``reset_login_details`` must preserve serial_number, not just motor_unit_serial.

    A revert that goes back to ``if token_param != STORAGE_DATA_MOTOR_UNIT_SERIAL``
    (only motor_unit_serial preserved) fails this test. Catches both forms of the
    fix: a literal STORAGE_DATA_SERIAL_NUMBER reference in the function body, or
    a class-level set/frozenset that includes it (as in the current fix).
    """
    src = _config_manager_source()

    # The exact revert pattern is forbidden anywhere in the module.
    forbidden_revert = re.search(
        r"if\s+token_param\s*!=\s*STORAGE_DATA_MOTOR_UNIT_SERIAL\s*:",
        src,
    )
    assert forbidden_revert is None, (
        "old pattern reintroduced — only motor_unit_serial would be preserved"
    )

    # The module must mention STORAGE_DATA_SERIAL_NUMBER in a preservation
    # context: either a class-level preserved set, or directly inside the
    # reset_login_details body.
    body_match = re.search(
        r"async def reset_login_details\(self\):.*?await self\._save\(\)",
        src,
        re.DOTALL,
    )
    assert body_match is not None, "reset_login_details body not found"
    body = body_match.group(0)

    preserved_set_match = re.search(
        r"(?:_PRESERVED_ON_RESET|preserved|PRESERVED)[^=]*=[^{]*\{[^}]*STORAGE_DATA_SERIAL_NUMBER",
        src,
        re.DOTALL,
    )

    assert ("STORAGE_DATA_SERIAL_NUMBER" in body) or (preserved_set_match is not None), (
        "STORAGE_DATA_SERIAL_NUMBER is no longer referenced in a preservation "
        "context — it would be wiped on every reset, orphaning entities"
    )
