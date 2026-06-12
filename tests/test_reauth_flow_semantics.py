"""Tests for reauthentication flow semantics."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.mydolphin_plus.common.consts import (
    CONF_OTP,
    INITIAL_TOKENS_KEY,
    STORAGE_DATA_ID_TOKEN,
    STORAGE_DATA_REFRESH_TOKEN,
)
import custom_components.mydolphin_plus.managers.flow_manager as flow_manager_module
from custom_components.mydolphin_plus.managers.flow_manager import (
    _FLOW_STATE_ATTR,
    IntegrationFlowManager,
)
from homeassistant.config_entries import SOURCE_REAUTH


class DummyFlowHandler:
    """Minimal flow handler for reauth tests."""

    def __init__(self):
        self.source = SOURCE_REAUTH
        self._entry = object()
        self.reauth_updates = None

    def _get_reauth_entry(self):
        return self._entry

    def async_update_reload_and_abort(self, entry, data_updates=None):
        self.reauth_updates = {"entry": entry, "data_updates": data_updates}
        return {"type": "abort", "reason": "reauth_successful"}


@pytest.mark.asyncio
async def test_flow_manager_reauth_otp_updates_existing_entry(monkeypatch):
    """Reauth OTP flow should update/reload existing entry instead of create."""
    flow_handler = DummyFlowHandler()
    setattr(
        flow_handler,
        _FLOW_STATE_ATTR,
        {
            "title": "My Dolphin",
            "email": "user@example.com",
            "cognito_session": "sess",
        },
    )

    async def fake_initialize(*_args, **_kwargs):
        return None

    async def fake_respond_otp(*_args, **_kwargs):
        return {"IdToken": "id-token", "RefreshToken": "refresh-token", "ExpiresIn": 3600}

    async def fake_profile(*_args, **_kwargs):
        return {"Sernum": "serial", "eSERNUM": "motor"}

    monkeypatch.setattr(flow_manager_module, "async_get_clientsession", lambda _hass: object())
    monkeypatch.setattr(
        flow_manager_module.IntegrationInfo,
        "initialize",
        fake_initialize,
    )
    monkeypatch.setattr(flow_manager_module, "cognito_respond_otp", fake_respond_otp)
    monkeypatch.setattr(flow_manager_module, "fetch_user_profile", fake_profile)

    manager = IntegrationFlowManager(
        hass=SimpleNamespace(),
        flow_handler=flow_handler,
        entry=flow_handler._get_reauth_entry(),
        source=SOURCE_REAUTH,
    )

    result = await manager.async_step_otp({CONF_OTP: "123456"})

    assert result["type"] == "abort"
    assert flow_handler.reauth_updates is not None
    updates = flow_handler.reauth_updates["data_updates"]
    assert updates is not None
    assert updates[INITIAL_TOKENS_KEY][STORAGE_DATA_ID_TOKEN] == "id-token"
    assert updates[INITIAL_TOKENS_KEY][STORAGE_DATA_REFRESH_TOKEN] == "refresh-token"


# BUG-02 removed the inbound test
# `test_coordinator_reauth_is_started_once_for_expired_token` because its
# premise — the internal ``_reauth_in_progress`` flag guarding a second
# call — no longer exists. The replacement
# ``tests/test_bug_01_02_reauth_flow.py::test_bug02_repeated_calls_each_trigger_async_start_reauth``
# asserts the opposite (correct) semantic: every EXPIRED_TOKEN cycle now
# reaches ``entry.async_start_reauth``, with HA Core handling idempotency.
