"""Regression tests for HARD-02 / F4 — REST session lifecycle hardening.

HARD-02 (#23) reopened after BUG-24 (#122) moved the reconnect retry inside
``_async_update_data``: the tick now awaits ``RestAPI.initialize()`` directly,
so an aiohttp request that hits the implicit ~300 s default timeout stalls
the coordinator tick, HARD-11 overlay reconciles, and the pause-guard TTL.
F4 is the paired session-replacement leak spotted in the same review:
``_initialize_session()`` overwrote ``self._session`` on every reconnect
attempt, and ``terminate()`` only ever closed the *last* reference — so
a sustained outage under BUG-24 leaked one ``ClientSession`` per attempt.

The fix in ``rest_api.py``:

* introduces a module-level ``REST_API_TIMEOUT = ClientTimeout(total=30,
  sock_connect=5, sock_read=10)`` and passes it to both session
  constructors (HA-hosted ``async_create_clientsession`` and standalone
  ``ClientSession``);
* makes ``_initialize_session()`` idempotent — it reuses an open session,
  and only creates a new one when ``self._session is None`` or the
  existing session is closed;
* splits ownership in ``terminate()``: HA-mode sessions are ``detach()``ed
  (sync — they share HA's global aiohttp connector, closing them would
  affect other consumers), standalone sessions are ``close()``d (they own
  their connector). Both paths run under a single ``try/finally`` that
  clears ``self._session`` and sets ``DISCONNECTED`` even if cleanup
  raises, so a repeat call is a safe no-op;
* makes ``_initialize_session()`` report success/failure via ``bool`` so
  that ``initialize()`` can bail out before ``_login()`` when session
  construction fails (otherwise ``_login()`` would run with
  ``self._session is None`` and the resulting ``AttributeError`` would
  overwrite the original ``FAILED`` status);
* tightens ``is_connected`` so a closed session is never reported as
  connected.

These tests use fake sessions and monkeypatching only — no network I/O.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from aiohttp import ClientTimeout
import pytest

from custom_components.mydolphin_plus.common.connectivity_status import (
    ConnectivityStatus,
)
import custom_components.mydolphin_plus.managers.rest_api as rest_api_module
from custom_components.mydolphin_plus.managers.rest_api import REST_API_TIMEOUT, RestAPI

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeSession:
    """Stand-in for ``aiohttp.ClientSession``.

    Tracks ``close()`` **and** ``detach()`` invocations separately so the
    tests can pin the ownership split: HA-mode sessions must be detached
    (they share HA's global connector), standalone sessions must be
    closed (they own their connector). Real aiohttp ``detach()`` sets the
    connector to ``None`` and the ``closed`` property then reports
    ``True``; the fake mirrors that by flipping ``closed`` in both
    ``close()`` and ``detach()``. Idempotence under ``terminate()`` still
    holds because ``terminate()`` also drops the reference in its
    ``finally`` — a second call sees ``self._session is None`` and skips
    the whole branch.
    """

    def __init__(self, timeout: ClientTimeout | None = None):
        self.timeout = timeout
        self.closed = False
        self.close_calls = 0
        self.detach_calls = 0

    async def close(self) -> None:
        self.close_calls += 1
        self.closed = True

    def detach(self) -> None:
        self.detach_calls += 1
        self.closed = True


class DummyConfigManager:
    """Minimal ConfigManager surface used by RestAPI initialize/terminate."""

    entry_id = "entry-id-hard-02"


@pytest.fixture(autouse=True)
def _silence_ha_dispatcher(monkeypatch):
    """Neutralise HA's dispatcher_send so tests don't need a live event loop.

    ``_set_status`` fans out through ``dispatcher_send(self._hass, ...)``
    when the HA-mode code path is exercised (see ``_async_dispatcher_send``).
    That call assumes a real HomeAssistant with a running loop; the sentinel
    object we pass in as ``hass`` does not have one. Replacing the module-
    level import with a no-op keeps the tests focused on session lifecycle
    without dragging HA test scaffolding into the fixtures.
    """
    monkeypatch.setattr(
        rest_api_module,
        "dispatcher_send",
        lambda *_args, **_kwargs: None,
    )


def _make_api(hass: object | None = None) -> RestAPI:
    """Build a RestAPI with a no-op dispatcher and stubbed IntegrationInfo.

    ``set_local_async_dispatcher_send`` is used so the standalone code path
    does not require a HomeAssistant instance for signal dispatching.
    """
    api = RestAPI(hass, DummyConfigManager())
    api.set_local_async_dispatcher_send(lambda *_args, **_kwargs: None)
    api._integration_info = AsyncMock()
    api._integration_info.initialize = AsyncMock(return_value=None)
    return api


# ---------------------------------------------------------------------------
# 1. Timeout configuration
# ---------------------------------------------------------------------------


def test_module_level_timeout_has_expected_values():
    """The single source of truth for the REST timeout policy (values, not identity)."""
    assert isinstance(REST_API_TIMEOUT, ClientTimeout)
    assert REST_API_TIMEOUT.total == 30
    assert REST_API_TIMEOUT.sock_connect == 5
    assert REST_API_TIMEOUT.sock_read == 10


@pytest.mark.asyncio
async def test_home_assistant_mode_passes_explicit_timeout(monkeypatch):
    """HA path calls async_create_clientsession() with the explicit timeout."""
    calls: list[dict] = []

    def fake_create_clientsession(*, hass, timeout=None, **kwargs):
        calls.append({"hass": hass, "timeout": timeout, **kwargs})
        return FakeSession(timeout=timeout)

    monkeypatch.setattr(
        rest_api_module,
        "async_create_clientsession",
        fake_create_clientsession,
    )

    sentinel_hass = object()
    api = _make_api(hass=sentinel_hass)
    await api._initialize_session()

    assert len(calls) == 1
    timeout = calls[0]["timeout"]
    assert isinstance(timeout, ClientTimeout)
    assert timeout.total == 30
    assert timeout.sock_connect == 5
    assert timeout.sock_read == 10


@pytest.mark.asyncio
async def test_standalone_mode_passes_explicit_timeout(monkeypatch):
    """Standalone path constructs ClientSession() with the same explicit timeout."""
    captured: list[ClientTimeout | None] = []

    def fake_client_session(*args, timeout=None, **kwargs):
        captured.append(timeout)
        return FakeSession(timeout=timeout)

    monkeypatch.setattr(rest_api_module, "ClientSession", fake_client_session)

    api = _make_api(hass=None)
    await api._initialize_session()

    assert len(captured) == 1
    timeout = captured[0]
    assert isinstance(timeout, ClientTimeout)
    assert timeout.total == 30
    assert timeout.sock_connect == 5
    assert timeout.sock_read == 10


# ---------------------------------------------------------------------------
# 2. Reuse — an open session must be preserved across repeated attempts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_initialize_session_reuses_open_session(monkeypatch):
    """Second call to _initialize_session() with an open session is a no-op."""
    created: list[FakeSession] = []

    def fake_create_clientsession(*, hass, timeout=None, **_kwargs):
        session = FakeSession(timeout=timeout)
        created.append(session)
        return session

    monkeypatch.setattr(
        rest_api_module,
        "async_create_clientsession",
        fake_create_clientsession,
    )

    api = _make_api(hass=object())

    await api._initialize_session()
    first = api._session
    await api._initialize_session()

    assert len(created) == 1
    assert api._session is first


@pytest.mark.asyncio
async def test_initialize_repeat_does_not_recreate_session(monkeypatch):
    """RestAPI.initialize() called repeatedly reuses the same open session."""
    created: list[FakeSession] = []

    def fake_create_clientsession(*, hass, timeout=None, **_kwargs):
        session = FakeSession(timeout=timeout)
        created.append(session)
        return session

    monkeypatch.setattr(
        rest_api_module,
        "async_create_clientsession",
        fake_create_clientsession,
    )

    api = _make_api(hass=object())
    # Neutralise _login() so this test performs no network work.
    api._login = AsyncMock(return_value=None)

    await api.initialize()
    await api.initialize()
    await api.initialize()

    assert len(created) == 1
    assert not created[0].closed


@pytest.mark.asyncio
async def test_ten_reconnect_initializations_open_one_session(monkeypatch):
    """Simulate a sustained outage: 10 reconnect ticks must not leak sessions."""
    created: list[FakeSession] = []

    def fake_create_clientsession(*, hass, timeout=None, **_kwargs):
        session = FakeSession(timeout=timeout)
        created.append(session)
        return session

    monkeypatch.setattr(
        rest_api_module,
        "async_create_clientsession",
        fake_create_clientsession,
    )

    api = _make_api(hass=object())
    api._login = AsyncMock(return_value=None)

    for _ in range(10):
        await api.initialize()

    assert len(created) == 1
    open_sessions = [s for s in created if not s.closed]
    assert len(open_sessions) == 1


# ---------------------------------------------------------------------------
# 3. Closed-session recovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_closed_session_is_replaced(monkeypatch):
    """A closed session must not be reused; a fresh one is created instead."""
    created: list[FakeSession] = []

    def fake_create_clientsession(*, hass, timeout=None, **_kwargs):
        session = FakeSession(timeout=timeout)
        created.append(session)
        return session

    monkeypatch.setattr(
        rest_api_module,
        "async_create_clientsession",
        fake_create_clientsession,
    )

    api = _make_api(hass=object())
    await api._initialize_session()
    first = api._session
    first.closed = True

    await api._initialize_session()
    replacement = api._session

    assert replacement is not first
    assert len(created) == 2


@pytest.mark.asyncio
async def test_replacement_session_receives_explicit_timeout(monkeypatch):
    """The recreated session gets the same explicit ClientTimeout."""
    created_timeouts: list[ClientTimeout | None] = []

    def fake_create_clientsession(*, hass, timeout=None, **_kwargs):
        created_timeouts.append(timeout)
        return FakeSession(timeout=timeout)

    monkeypatch.setattr(
        rest_api_module,
        "async_create_clientsession",
        fake_create_clientsession,
    )

    api = _make_api(hass=object())
    await api._initialize_session()
    api._session.closed = True
    await api._initialize_session()

    assert len(created_timeouts) == 2
    for timeout in created_timeouts:
        assert isinstance(timeout, ClientTimeout)
        assert timeout.total == 30
        assert timeout.sock_connect == 5
        assert timeout.sock_read == 10


# ---------------------------------------------------------------------------
# 4. Termination
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminate_ha_mode_detaches_and_does_not_close(monkeypatch):
    """HA-mode terminate() calls detach() (sync) and never close().

    HA-mode sessions share HA's global aiohttp connector. Closing them can
    tear down the shared connector and affect other integrations. HA's own
    cleanup path calls detach() — this test pins that ownership split.
    """

    def fake_create_clientsession(*, hass, timeout=None, **_kwargs):
        return FakeSession(timeout=timeout)

    monkeypatch.setattr(
        rest_api_module,
        "async_create_clientsession",
        fake_create_clientsession,
    )

    api = _make_api(hass=object())
    await api._initialize_session()
    session = api._session

    await api.terminate()

    assert session.detach_calls == 1
    assert session.close_calls == 0


@pytest.mark.asyncio
async def test_terminate_standalone_mode_closes_and_does_not_detach(monkeypatch):
    """Standalone terminate() calls close() (async) and never detach().

    A standalone ClientSession owns its connector and must be closed to
    release the underlying sockets.
    """

    def fake_client_session(*_args, timeout=None, **_kwargs):
        return FakeSession(timeout=timeout)

    monkeypatch.setattr(rest_api_module, "ClientSession", fake_client_session)

    api = _make_api(hass=None)
    await api._initialize_session()
    session = api._session

    await api.terminate()

    assert session.close_calls == 1
    assert session.closed is True
    assert session.detach_calls == 0


@pytest.mark.asyncio
async def test_terminate_clears_session_reference(monkeypatch):
    """After terminate(), self._session is None."""

    def fake_create_clientsession(*, hass, timeout=None, **_kwargs):
        return FakeSession(timeout=timeout)

    monkeypatch.setattr(
        rest_api_module,
        "async_create_clientsession",
        fake_create_clientsession,
    )

    api = _make_api(hass=object())
    await api._initialize_session()

    await api.terminate()

    assert api._session is None


@pytest.mark.asyncio
async def test_terminate_ha_mode_twice_does_not_double_detach(monkeypatch):
    """A second HA-mode terminate() is a no-op — never detaches twice."""

    def fake_create_clientsession(*, hass, timeout=None, **_kwargs):
        return FakeSession(timeout=timeout)

    monkeypatch.setattr(
        rest_api_module,
        "async_create_clientsession",
        fake_create_clientsession,
    )

    api = _make_api(hass=object())
    await api._initialize_session()
    session = api._session

    await api.terminate()
    await api.terminate()

    assert session.detach_calls == 1
    assert session.close_calls == 0


@pytest.mark.asyncio
async def test_terminate_standalone_mode_twice_does_not_double_close(monkeypatch):
    """A second standalone terminate() is a no-op — never closes twice."""

    def fake_client_session(*_args, timeout=None, **_kwargs):
        return FakeSession(timeout=timeout)

    monkeypatch.setattr(rest_api_module, "ClientSession", fake_client_session)

    api = _make_api(hass=None)
    await api._initialize_session()
    session = api._session

    await api.terminate()
    await api.terminate()

    assert session.close_calls == 1


@pytest.mark.asyncio
async def test_terminate_without_session_is_safe():
    """terminate() with no session is a no-op (does not raise)."""
    api = _make_api(hass=None)
    assert api._session is None

    await api.terminate()

    assert api._session is None


@pytest.mark.asyncio
async def test_reinitialize_after_terminate_creates_new_session(monkeypatch):
    """After terminate(), _initialize_session() opens a fresh session."""
    created: list[FakeSession] = []

    def fake_create_clientsession(*, hass, timeout=None, **_kwargs):
        session = FakeSession(timeout=timeout)
        created.append(session)
        return session

    monkeypatch.setattr(
        rest_api_module,
        "async_create_clientsession",
        fake_create_clientsession,
    )

    api = _make_api(hass=object())
    await api._initialize_session()
    await api.terminate()
    await api._initialize_session()

    assert len(created) == 2
    assert api._session is created[1]
    assert not created[1].closed


# ---------------------------------------------------------------------------
# 5. Connectivity semantics
# ---------------------------------------------------------------------------


def test_is_connected_false_without_session():
    api = _make_api(hass=None)
    assert api._session is None
    assert api.is_connected is False


def test_is_connected_true_with_open_session():
    api = _make_api(hass=None)
    api._session = FakeSession()
    assert api.is_connected is True


def test_is_connected_false_with_closed_session():
    api = _make_api(hass=None)
    session = FakeSession()
    session.closed = True
    api._session = session
    assert api.is_connected is False


# ---------------------------------------------------------------------------
# 6. Creation failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_creation_failure_sets_failed_status(monkeypatch):
    """If the session constructor raises, connectivity status becomes FAILED."""
    def exploding_create_clientsession(**_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        rest_api_module,
        "async_create_clientsession",
        exploding_create_clientsession,
    )

    api = _make_api(hass=object())
    await api._initialize_session()

    assert api.status == ConnectivityStatus.FAILED


@pytest.mark.asyncio
async def test_creation_failure_leaves_session_none(monkeypatch):
    """A failed constructor must not leave a stale/partial session reference."""

    def exploding_create_clientsession(**_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        rest_api_module,
        "async_create_clientsession",
        exploding_create_clientsession,
    )

    api = _make_api(hass=object())
    await api._initialize_session()

    assert api._session is None
    assert api.is_connected is False


@pytest.mark.asyncio
async def test_creation_failure_stops_initialize_before_login(monkeypatch):
    """initialize() must abort before _login() when session creation fails.

    Otherwise _login() runs with self._session is None and its first
    self._session.post(...) raises AttributeError, overwriting the FAILED
    status set by _initialize_session() with a secondary, misleading error.
    """

    def exploding_create_clientsession(**_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        rest_api_module,
        "async_create_clientsession",
        exploding_create_clientsession,
    )

    api = _make_api(hass=object())
    api._login = AsyncMock(return_value=None)

    await api.initialize()

    api._login.assert_not_called()
    assert api._session is None
    assert api.status == ConnectivityStatus.FAILED


@pytest.mark.asyncio
async def test_initialize_session_returns_true_on_success(monkeypatch):
    """_initialize_session() reports success so initialize() can gate on it."""

    def fake_create_clientsession(*, hass, timeout=None, **_kwargs):
        return FakeSession(timeout=timeout)

    monkeypatch.setattr(
        rest_api_module,
        "async_create_clientsession",
        fake_create_clientsession,
    )

    api = _make_api(hass=object())
    result = await api._initialize_session()

    assert result is True
    # And the reuse path also returns True — an idempotent no-op is a success.
    assert await api._initialize_session() is True


@pytest.mark.asyncio
async def test_initialize_session_returns_false_on_failure(monkeypatch):
    """_initialize_session() reports failure so initialize() can bail out."""

    def exploding_create_clientsession(**_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        rest_api_module,
        "async_create_clientsession",
        exploding_create_clientsession,
    )

    api = _make_api(hass=object())
    result = await api._initialize_session()

    assert result is False
