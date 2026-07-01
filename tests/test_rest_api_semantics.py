"""Tests for auth headers and rate-limit semantics."""

from __future__ import annotations

from datetime import datetime

import pytest

from custom_components.mydolphin_plus.common.connectivity_status import (
    ConnectivityStatus,
)
from custom_components.mydolphin_plus.common.consts import API_TOKEN_FIELDS
import custom_components.mydolphin_plus.managers.rest_api as rest_api_module
from custom_components.mydolphin_plus.managers.rest_api import (
    RestAPI,
    cognito_initiate_auth,
    fetch_aws_credentials,
    fetch_user_profile,
)


class DummyIntegrationInfo:
    """Simple user-agent provider used in tests."""

    def set_user_agent(self, headers: dict) -> None:
        headers["User-Agent"] = "HA-MyDolphin-Plus/test"


class FakeResponse:
    """Minimal async response object."""

    def __init__(self, payload: dict, status: int = 200):
        self._payload = payload
        self.status = status
        self.message = "error"

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def text(self) -> str:
        import json

        return json.dumps(self._payload)

    async def json(self) -> dict:
        return self._payload

    def raise_for_status(self):
        if self.status >= 400:
            raise Exception("http failure")


class FakeSession:
    """Captures request headers for assertions."""

    def __init__(self, post_payload: dict | None = None, get_payload: dict | None = None):
        self._post_payload = post_payload or {}
        self._get_payload = get_payload or {}
        self.last_headers: dict | None = None

    def post(self, _url, headers=None, data=None):
        self.last_headers = headers
        return FakeResponse(self._post_payload)

    def get(self, _url, headers=None):
        self.last_headers = headers
        return FakeResponse(self._get_payload)


class DummyConfigManager:
    """Config manager surface needed by RestAPI for these tests."""

    def __init__(self):
        now = datetime.now().timestamp()
        self.id_token = "id-token"
        self.refresh_token = "refresh-token"
        self.id_token_expires_at = now + 3600
        self.serial_number = None
        self.motor_unit_serial = None
        self.last_token_fetch = now
        self.entry_id = "entry-id"

    @property
    def config_data(self):
        return None

    async def update_tokens(self, *_args, **_kwargs):
        return None

    async def reset_login_details(self):
        return None

    async def update_serial_number(self, serial_number: str):
        self.serial_number = serial_number

    async def update_motor_unit_serial(self, motor_unit_serial: str):
        self.motor_unit_serial = motor_unit_serial


@pytest.mark.asyncio
async def test_cognito_initiate_auth_sets_user_agent():
    """Cognito initiate auth includes User-Agent headers."""
    session = FakeSession(post_payload={"ChallengeName": "CUSTOM_CHALLENGE", "Session": "x"})
    info = DummyIntegrationInfo()

    await cognito_initiate_auth(session, "user@example.com", integration_info=info)

    assert session.last_headers["User-Agent"] == "HA-MyDolphin-Plus/test"


@pytest.mark.asyncio
async def test_fetch_user_profile_sets_user_agent():
    """authenticate-user request includes User-Agent headers."""
    session = FakeSession(post_payload={"Data": {"Sernum": "123"}})
    info = DummyIntegrationInfo()

    await fetch_user_profile(session, "id-token", integration_info=info)

    assert session.last_headers["User-Agent"] == "HA-MyDolphin-Plus/test"
    assert session.last_headers["Authorization"] == "Bearer id-token"


@pytest.mark.asyncio
async def test_fetch_aws_credentials_sets_user_agent():
    """getToken request includes User-Agent headers."""
    session = FakeSession(get_payload={"Data": {"AccessKeyId": "AKIA"}})
    info = DummyIntegrationInfo()

    await fetch_aws_credentials(session, "id-token", integration_info=info)

    assert session.last_headers["User-Agent"] == "HA-MyDolphin-Plus/test"
    assert session.last_headers["Authorization"] == "Bearer id-token"


@pytest.mark.asyncio
async def test_refresh_aws_credentials_records_in_memory_fetch_timestamp(monkeypatch):
    """After a successful fetch, RestAPI records the timestamp in memory (BUG-17)."""
    cfg = DummyConfigManager()
    cfg.last_token_fetch = datetime.now().timestamp()
    api = RestAPI(None, cfg)
    api._session = object()
    api.set_local_async_dispatcher_send(lambda *_args: None)

    assert api._last_aws_credentials_fetch == 0.0
    assert api._aws_credentials_expiry == 0.0

    async def fake_fetch_aws_credentials(_session, _id_token, integration_info=None):
        assert integration_info is not None
        return {
            "Token": "t",
            "AccessKeyId": "ak",
            "SecretAccessKey": "sk",
        }

    monkeypatch.setattr(rest_api_module, "fetch_aws_credentials", fake_fetch_aws_credentials)

    await api._refresh_aws_credentials()

    assert api._last_aws_credentials_fetch > 0
    assert api._aws_credentials_expiry > api._last_aws_credentials_fetch


@pytest.mark.asyncio
async def test_rate_limited_with_expired_cache_sets_failed():
    """Rate-limited path should not claim connected with expired cache (BUG-17: fields now on RestAPI)."""
    cfg = DummyConfigManager()
    api = RestAPI(None, cfg)
    api._session = object()
    api.set_local_async_dispatcher_send(lambda *_args: None)
    now = datetime.now().timestamp()
    api._last_aws_credentials_fetch = now
    api._aws_credentials_expiry = now - 60
    for field in API_TOKEN_FIELDS:
        api.data[field] = f"cached-{field}"

    await api._refresh_aws_credentials()

    assert api.status == ConnectivityStatus.FAILED
