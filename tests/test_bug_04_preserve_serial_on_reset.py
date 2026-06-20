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
