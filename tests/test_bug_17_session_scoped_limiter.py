"""BUG-17: getToken rate-limiter is session-scoped.

The pre-fix asymmetry:
- `last_aws_credentials_fetch` and `aws_credentials_expiry` were persisted to
  `.storage` by ConfigManager (survives reload).
- The AWS creds values themselves (Token / AccessKeyId / SecretAccessKey) lived
  only on RestAPI.data (dropped at every reload).

Consequence at reload within 5 min of a previous fetch:
  * `last_fetch > 0` (persisted) → rate-limit branch fires → WARNING
    `Token fetch rate limited. Last fetch was Ns ago. Need to wait N more.`
  * `_has_cached_credentials()` False (empty self.data) → fall-through WARNING
    `No cached credentials available, attempting fetch despite rate limit`
  * Fetch happens anyway → self-defeating limiter.

Direction 2 fix (per Elad Bar's #275 intent — AWS STS creds stay in memory
only): move the two timestamps to instance-scoped fields on RestAPI. A fresh
instance starts both at 0.0; `last_fetch > 0` is False; the rate-limit branch
short-circuits; a clean single fetch happens with no contradictory WARNING pair.
"""

from __future__ import annotations

from datetime import datetime
import logging

import pytest

from custom_components.mydolphin_plus.managers.config_manager import ConfigManager
import custom_components.mydolphin_plus.managers.rest_api as rest_api_module
from custom_components.mydolphin_plus.managers.rest_api import RestAPI


class _StubConfigManager:
    """Minimal ConfigManager surface used by RestAPI in these tests."""

    def __init__(self):
        self.id_token = "id-token"
        self.last_token_fetch = datetime.now().timestamp()
        self.serial_number = None
        self.motor_unit_serial = None
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


def _fresh_api() -> RestAPI:
    api = RestAPI(None, _StubConfigManager())
    api._session = object()
    api.set_local_async_dispatcher_send(lambda *_args: None)
    return api


def _install_fake_fetch(monkeypatch, counter: dict[str, int]) -> None:
    async def fake_fetch(_session, _id_token, integration_info=None):
        counter["n"] = counter.get("n", 0) + 1
        return {"Token": "t", "AccessKeyId": "ak", "SecretAccessKey": "sk"}

    monkeypatch.setattr(rest_api_module, "fetch_aws_credentials", fake_fetch)


# --- Fields are instance-scoped, not delegated to ConfigManager -------------


def test_fresh_restapi_starts_with_zero_ratelimit_fields():
    """Instance-scoped rate-limit state starts at 0.0 on every construction."""
    api = _fresh_api()
    assert api._last_aws_credentials_fetch == 0.0
    assert api._aws_credentials_expiry == 0.0


def test_restapi_no_longer_reads_ratelimit_state_from_config_manager():
    """The ConfigManager no longer exposes AWS rate-limit fields (delete regression pin)."""
    assert not hasattr(ConfigManager, "last_aws_credentials_fetch")
    assert not hasattr(ConfigManager, "aws_credentials_expiry")
    assert not hasattr(ConfigManager, "update_last_aws_credentials_fetch")
    assert not hasattr(ConfigManager, "update_aws_credentials_expiry")
    assert not hasattr(ConfigManager, "_validate_cached_credentials")


# --- Reload path (the reported symptom) -------------------------------------


@pytest.mark.asyncio
async def test_fresh_instance_does_not_emit_ratelimit_warning_pair(monkeypatch, caplog):
    """Reload = fresh RestAPI = last_fetch 0.0 → no rate-limit branch, no WARNING.

    Reproduces the BUG-17 scenario as observed on S2000 (8 reloads inside 5 min).
    Pre-fix: WARNING pair emitted every time. Post-fix: silent single fetch.
    """
    api = _fresh_api()
    counter: dict[str, int] = {}
    _install_fake_fetch(monkeypatch, counter)

    caplog.set_level(logging.WARNING, logger=rest_api_module.__name__)

    await api._refresh_aws_credentials()

    # Fetch happened exactly once.
    assert counter.get("n") == 1
    # Timestamps recorded in memory.
    assert api._last_aws_credentials_fetch > 0
    assert api._aws_credentials_expiry > api._last_aws_credentials_fetch
    # The reported symptom strings must not appear.
    joined = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "Token fetch rate limited" not in joined
    assert "attempting fetch despite rate limit" not in joined


# --- Intra-session limiter still works --------------------------------------


@pytest.mark.asyncio
async def test_two_rapid_refresh_calls_within_session_hit_in_memory_limit(monkeypatch, caplog):
    """Same-instance rapid refresh trips the rate-limit branch.

    First call populates the fields + `self.data`; second call within 5 min sees
    `last_fetch > 0` and takes the "use cached creds" branch (INFO), not the
    self-bypass path — because self.data is now populated, `_has_cached_credentials`
    is True and the fall-through WARNING is unreachable.
    """
    api = _fresh_api()
    counter: dict[str, int] = {}
    _install_fake_fetch(monkeypatch, counter)

    caplog.set_level(logging.WARNING, logger=rest_api_module.__name__)

    await api._refresh_aws_credentials()
    assert counter.get("n") == 1

    await api._refresh_aws_credentials()
    # Second call did NOT fetch (used the cached creds).
    assert counter.get("n") == 1
    # The self-bypass string must still be unreachable — self.data is populated.
    joined = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "attempting fetch despite rate limit" not in joined


