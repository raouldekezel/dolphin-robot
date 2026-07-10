"""Regression tests for BUG-23 — transient network failures during Cognito
token refresh must NOT wipe stored credentials.

Before the fix, ``_cognito_call``'s blanket ``except Exception → raise
LoginError`` turned any transient failure (DNS timeout, socket error,
Cognito 5xx/429, malformed body) into an indistinguishable ``LoginError``.
``_ensure_id_token_valid`` then treated it as a real server-side rejection
and called ``reset_login_details()`` — permanently wiping ``id-token``,
``refresh-token`` and ``id-token-expires-at`` in ``.storage``. Every network
glitch became a manual OTP reauth.

After the fix:

* ``_cognito_call`` classifies failures into two disjoint types:
  ``TransientAuthError`` for retryable failures (network, timeout, 5xx,
  429, unknown) and ``LoginError`` for 4xx or protocol-level rejects.
* ``TransientAuthError`` is a subclass of ``LoginError`` so any existing
  ``except LoginError`` site (notably ``flow_manager``'s config/reauth
  steps) keeps catching it and re-shows the form gracefully — no
  regression on the OTP path.
* ``_ensure_id_token_valid`` orders ``except TransientAuthError`` before
  ``except LoginError``: transient → status ``FAILED`` (WARNING level),
  tokens preserved, coordinator retries. Terminal reject → wipe and force
  OTP reauth (unchanged behaviour).
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import json
import logging
from unittest.mock import MagicMock

from aiohttp import ClientOSError
import pytest

from custom_components.mydolphin_plus.common.connectivity_status import (
    ConnectivityStatus,
)
import custom_components.mydolphin_plus.managers.rest_api as rest_api_module
from custom_components.mydolphin_plus.managers.rest_api import (
    RestAPI,
    _cognito_call,
    cognito_refresh,
)
from custom_components.mydolphin_plus.models.exceptions import (
    LoginError,
    TransientAuthError,
)


class _DummyIntegrationInfo:
    def set_user_agent(self, headers: dict) -> None:
        headers["User-Agent"] = "HA-MyDolphin-Plus/test"

    async def initialize(self, _hass):
        return None


class _RaisingContextManager:
    """``async with session.post(...) as response`` context manager whose
    ``__aenter__`` raises the given exception."""

    def __init__(self, exc: BaseException):
        self._exc = exc

    async def __aenter__(self):
        raise self._exc

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeResponse:
    def __init__(self, status: int, payload=None, text_override: str | None = None):
        self.status = status
        self._payload = payload if payload is not None else {}
        self._text_override = text_override

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def text(self) -> str:
        if self._text_override is not None:
            return self._text_override
        return json.dumps(self._payload)


class _FakeSession:
    def __init__(
        self,
        *,
        response: _FakeResponse | None = None,
        raise_on_post: BaseException | None = None,
    ):
        self._response = response
        self._raise = raise_on_post

    def post(self, _url, headers=None, data=None):
        if self._raise is not None:
            return _RaisingContextManager(self._raise)
        return self._response


# ---------------------------------------------------------------------------
# Subclass invariant — §1 remedy
# ---------------------------------------------------------------------------


def test_transient_auth_error_is_login_error_subclass():
    """TransientAuthError MUST be a subclass of LoginError so every
    ``except LoginError`` site (flow_manager config/reauth steps) keeps
    catching it. Removing this relation reintroduces the OTP-path
    regression called out in the design review."""
    assert issubclass(TransientAuthError, LoginError)
    assert isinstance(TransientAuthError("x"), LoginError)


# ---------------------------------------------------------------------------
# _cognito_call classification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [500, 502, 503, 429])
async def test_cognito_call_5xx_or_429_raises_transient(status):
    session = _FakeSession(
        response=_FakeResponse(status, {"__type": "InternalFailure"})
    )
    with pytest.raises(TransientAuthError):
        await _cognito_call(
            session, "InitiateAuth", {}, integration_info=_DummyIntegrationInfo()
        )


@pytest.mark.asyncio
async def test_cognito_call_4xx_raises_login_error_not_transient():
    session = _FakeSession(
        response=_FakeResponse(400, {"__type": "NotAuthorizedException"})
    )
    with pytest.raises(LoginError) as ei:
        await _cognito_call(
            session, "InitiateAuth", {}, integration_info=_DummyIntegrationInfo()
        )
    # 4xx must be a terminal LoginError, not the retryable subclass.
    assert not isinstance(ei.value, TransientAuthError)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [
        ClientOSError("dns down"),
        asyncio.TimeoutError(),
        RuntimeError("unexpected"),
    ],
    ids=["client_os_error", "timeout", "unknown_exception"],
)
async def test_cognito_call_network_or_unknown_raises_transient(exc):
    session = _FakeSession(raise_on_post=exc)
    with pytest.raises(TransientAuthError):
        await _cognito_call(
            session, "InitiateAuth", {}, integration_info=_DummyIntegrationInfo()
        )


@pytest.mark.asyncio
async def test_cognito_call_malformed_2xx_body_raises_transient():
    """Non-JSON body on a 200 → fail-safe default → transient (JSONDecodeError
    is caught by the generic Exception arm and rewrapped as
    TransientAuthError; the refresh token stays intact)."""
    session = _FakeSession(response=_FakeResponse(200, text_override="<html>not-json"))
    with pytest.raises(TransientAuthError):
        await _cognito_call(
            session, "InitiateAuth", {}, integration_info=_DummyIntegrationInfo()
        )


@pytest.mark.asyncio
async def test_cognito_refresh_200_without_authentication_result_is_terminal():
    """A 200 response without ``AuthenticationResult`` is a protocol-level
    reject (``rest_api.py:167``): the refresh token is genuinely dead and
    must trigger OTP reauth. This test pins that the reject is a plain
    ``LoginError`` — not the retryable ``TransientAuthError`` subclass —
    so a future drift can't accidentally route it into the fail-safe
    transient path and retry forever on a dead token."""
    session = _FakeSession(response=_FakeResponse(200, {"ChallengeName": "OTP"}))
    with pytest.raises(LoginError) as ei:
        await cognito_refresh(
            session, "some-refresh-token", integration_info=_DummyIntegrationInfo()
        )
    assert not isinstance(ei.value, TransientAuthError)


# ---------------------------------------------------------------------------
# _ensure_id_token_valid handler
# ---------------------------------------------------------------------------


class _TrackingConfigManager:
    """Records whether ``reset_login_details()`` was called and tracks
    ``update_tokens`` writes so tests can assert credential lifecycle."""

    def __init__(self):
        now = datetime.now().timestamp()
        self.id_token = "id-token"
        self.refresh_token = "refresh-token"
        self.id_token_expires_at = 0  # forces the refresh path
        self.serial_number = None
        self.motor_unit_serial = None
        self.last_token_fetch = now
        self.entry_id = "entry-id"
        self.reset_called = False
        self.tokens_written: dict | None = None

    @property
    def config_data(self):
        return None

    async def update_tokens(self, id_token, refresh_token, expires_at):
        self.tokens_written = {
            "id_token": id_token,
            "refresh_token": refresh_token,
            "expires_at": expires_at,
        }
        self.id_token = id_token
        if refresh_token is not None:
            self.refresh_token = refresh_token
        self.id_token_expires_at = expires_at

    async def reset_login_details(self):
        self.reset_called = True
        self.id_token = None
        self.refresh_token = None
        self.id_token_expires_at = None

    async def update_serial_number(self, serial_number):
        self.serial_number = serial_number

    async def update_motor_unit_serial(self, motor_unit_serial):
        self.motor_unit_serial = motor_unit_serial


def _build_api(cfg):
    api = RestAPI(None, cfg)
    api._session = object()
    api.set_local_async_dispatcher_send(lambda *_args: None)
    return api


@pytest.mark.asyncio
async def test_ensure_id_token_valid_transient_preserves_tokens(monkeypatch):
    """Transient failure during refresh must NOT wipe credentials."""
    cfg = _TrackingConfigManager()
    api = _build_api(cfg)

    async def failing(*_args, **_kwargs):
        raise TransientAuthError("Cognito InitiateAuth network failure: dns")

    monkeypatch.setattr(rest_api_module, "cognito_refresh", failing)

    result = await api._ensure_id_token_valid()

    assert result is False
    assert cfg.reset_called is False, "reset_login_details must NOT run on transient"
    assert cfg.refresh_token == "refresh-token"
    assert cfg.id_token == "id-token"
    assert api.status == ConnectivityStatus.FAILED


@pytest.mark.asyncio
async def test_ensure_id_token_valid_reject_wipes_tokens(monkeypatch):
    """Server-side reject (real 4xx / protocol reject) must wipe credentials
    and set EXPIRED_TOKEN (unchanged behaviour on this path)."""
    cfg = _TrackingConfigManager()
    api = _build_api(cfg)

    async def failing(*_args, **_kwargs):
        raise LoginError("Refresh token rejected by Cognito")

    monkeypatch.setattr(rest_api_module, "cognito_refresh", failing)

    result = await api._ensure_id_token_valid()

    assert result is False
    assert cfg.reset_called is True
    assert cfg.refresh_token is None
    assert cfg.id_token is None
    assert api.status == ConnectivityStatus.EXPIRED_TOKEN


@pytest.mark.asyncio
async def test_ensure_id_token_valid_recovers_after_transient(monkeypatch):
    """After a transient failure, the next tick with a healthy Cognito
    response must succeed and rotate tokens — the credentials were
    preserved so the recovery path is reachable."""
    cfg = _TrackingConfigManager()
    api = _build_api(cfg)

    async def failing(*_args, **_kwargs):
        raise TransientAuthError("timeout")

    monkeypatch.setattr(rest_api_module, "cognito_refresh", failing)
    assert await api._ensure_id_token_valid() is False
    assert cfg.reset_called is False
    assert cfg.refresh_token == "refresh-token"

    async def healthy(*_args, **_kwargs):
        return {
            "IdToken": "new-id-token",
            "RefreshToken": "new-refresh-token",
            "ExpiresIn": 3600,
        }

    monkeypatch.setattr(rest_api_module, "cognito_refresh", healthy)
    assert await api._ensure_id_token_valid() is True
    assert cfg.tokens_written is not None
    assert cfg.tokens_written["id_token"] == "new-id-token"
    assert cfg.tokens_written["refresh_token"] == "new-refresh-token"


@pytest.mark.asyncio
async def test_transient_refresh_does_not_log_error(monkeypatch, caplog):
    """Transient failure must be logged at WARNING or below — a sustained
    outage would otherwise spam ERROR every retry (fix uses
    ``force_log_level=logging.WARNING``)."""
    cfg = _TrackingConfigManager()
    api = _build_api(cfg)

    async def failing(*_args, **_kwargs):
        raise TransientAuthError("dns")

    monkeypatch.setattr(rest_api_module, "cognito_refresh", failing)

    logger_name = "custom_components.mydolphin_plus.managers.rest_api"
    with caplog.at_level(logging.DEBUG, logger=logger_name):
        await api._ensure_id_token_valid()

    error_records = [
        r for r in caplog.records if r.levelno >= logging.ERROR and r.name == logger_name
    ]
    assert error_records == [], (
        f"transient refresh must not emit ERROR: {[r.getMessage() for r in error_records]}"
    )


# ---------------------------------------------------------------------------
# Config-flow safety-net — the flow_manager must still catch transient
# errors via ``except LoginError`` and re-show the form (regression guard
# for the §1 remedy).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flow_manager_user_step_catches_transient_via_login_error(
    monkeypatch,
):
    """Transient network failure during ``async_step_user`` (email entry)
    must be caught by the existing ``except LoginError`` — the form is
    re-shown with ``otp_send_failed`` and no exception escapes."""
    from custom_components.mydolphin_plus.common.consts import CONF_TITLE
    from custom_components.mydolphin_plus.managers import flow_manager as fm_module
    from homeassistant.const import CONF_USERNAME

    async def failing_initiate(*_args, **_kwargs):
        raise TransientAuthError("Cognito InitiateAuth network failure: dns")

    monkeypatch.setattr(fm_module, "cognito_initiate_auth", failing_initiate)
    monkeypatch.setattr(
        fm_module, "async_get_clientsession", lambda _hass: object()
    )

    hass = MagicMock()
    flow_handler = MagicMock()
    # `async_show_form` is a plain callable — return a sentinel we can assert on.
    flow_handler.async_show_form = MagicMock(
        side_effect=lambda **kwargs: {"type": "form", **kwargs}
    )

    mgr = fm_module.IntegrationFlowManager(hass, flow_handler)
    # Replace the manager's real IntegrationInfo with a stub whose
    # ``initialize`` is a no-op coroutine (avoids touching HA storage).
    mgr._integration_info = _DummyIntegrationInfo()

    result = await mgr.async_step_user(
        {CONF_TITLE: "Nono", CONF_USERNAME: "user@example.com"}
    )

    assert result["type"] == "form"
    assert result["errors"] == {"base": "otp_send_failed"}


@pytest.mark.asyncio
async def test_flow_manager_otp_step_catches_transient_via_login_error(
    monkeypatch,
):
    """Transient network failure during ``async_step_otp`` must be caught
    by ``except LoginError`` — form re-shown with ``invalid_otp`` and no
    exception escapes."""
    from custom_components.mydolphin_plus.common.consts import CONF_OTP
    from custom_components.mydolphin_plus.managers import flow_manager as fm_module

    async def failing_respond(*_args, **_kwargs):
        raise TransientAuthError("Cognito RespondToAuthChallenge network failure: dns")

    monkeypatch.setattr(fm_module, "cognito_respond_otp", failing_respond)
    monkeypatch.setattr(
        fm_module, "async_get_clientsession", lambda _hass: object()
    )

    hass = MagicMock()
    flow_handler = MagicMock()
    flow_handler.async_show_form = MagicMock(
        side_effect=lambda **kwargs: {"type": "form", **kwargs}
    )
    # Prime the in-progress state that async_step_otp expects.
    setattr(
        flow_handler,
        fm_module._FLOW_STATE_ATTR,
        {"title": "Nono", "email": "user@example.com", "cognito_session": "s"},
    )

    mgr = fm_module.IntegrationFlowManager(hass, flow_handler)
    mgr._integration_info = _DummyIntegrationInfo()

    result = await mgr.async_step_otp({CONF_OTP: "123456"})

    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_otp"}
