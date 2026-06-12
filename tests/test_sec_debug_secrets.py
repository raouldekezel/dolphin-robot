"""Tests asserting that the SEC fixes don't allow secrets to leak.

Each test drives **production code** (not a copy of the fixed log call) so
that reverting the corresponding fix in production makes the test fail.

Coverage:

- SEC-01: ConfigManager._save() — drives the real method against a mocked
  Store and asserts the captured log records contain no token value.
- SEC-02: AWSClient._debug_log_credentials_received() — extracted helper
  invoked from initialize(); the test calls it directly with realistic-looking
  credentials and asserts only lengths appear in the log.
- SEC-03: RestAPI._debug_log_api_data_updated() — extracted helper invoked
  from update(); the test calls it directly with a payload containing AWS
  Token / AccessKeyId / SecretAccessKey and asserts they are redacted.
- SEC-04: ConfigManager._load() — drives the real method against a mocked
  Store loaded with token-containing data and asserts the INFO log records
  contain no token value.

A defense-in-depth grep-style check (``test_*_source_has_no_raw_secret_pattern``)
inspects each module's source for forbidden formatting patterns. This catches
a revert that removes the helper indirection and restores a raw f-string at
the original site, even if the helper itself is still present (and thus the
helper test would still pass).
"""

from __future__ import annotations

import inspect
import logging
from pathlib import Path
import re
from unittest.mock import AsyncMock, MagicMock

import pytest

# Realistic-looking but fake credentials used to exercise the log paths.
FAKE_ID_TOKEN = (
    "eyJraWQiOiJYWFhYIiwiYWxnIjoiUlMyNTYifQ."
    "eyJzdWIiOiJyYW91bC1mYWtlIiwiZW1haWwiOiJyYW91bEBleGFtcGxlLmNvbSJ9."
    "FAKE_SIGNATURE_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
)
FAKE_REFRESH_TOKEN = "FAKE_REFRESH_TOKEN_BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
FAKE_AWS_ACCESS_KEY_ID = "AKIAFAKEAKIAFAKEAKIA"
FAKE_AWS_SECRET = "FAKEsecretFAKEsecretFAKEsecretFAKEsecret"
FAKE_AWS_SESSION_TOKEN = "FAKEsessionFAKEsessionFAKEsessionFAKEsessionFAKEsession"

# Patterns we must never see anywhere in debug logs.
SECRET_PATTERNS = [
    re.compile(re.escape(FAKE_ID_TOKEN)),
    re.compile(re.escape(FAKE_REFRESH_TOKEN)),
    re.compile(re.escape(FAKE_AWS_ACCESS_KEY_ID)),
    re.compile(re.escape(FAKE_AWS_SECRET)),
    re.compile(re.escape(FAKE_AWS_SESSION_TOKEN)),
    # Defensive: any JWT-looking prefix from a real Cognito IdToken.
    re.compile(r"\beyJ[A-Za-z0-9_\-]{20,}\."),
    # Defensive: any plausible AWS access key id (AKIA + 16 chars).
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
]


def _assert_no_secret_in_records(records: list[logging.LogRecord]) -> None:
    """Fail if any log record contains a recognized secret pattern."""
    for record in records:
        message = record.getMessage()
        for pattern in SECRET_PATTERNS:
            assert not pattern.search(message), (
                f"Secret pattern '{pattern.pattern}' leaked into log: {message!r}"
            )


# --- SEC-01 : real ConfigManager._save() ------------------------------------


@pytest.mark.asyncio
async def test_sec01_config_manager_save_does_not_leak_tokens(caplog):
    """Drive the real ConfigManager._save() and assert tokens never appear."""
    from custom_components.mydolphin_plus.common.consts import (
        STORAGE_DATA_ID_TOKEN,
        STORAGE_DATA_MOTOR_UNIT_SERIAL,
        STORAGE_DATA_REFRESH_TOKEN,
    )
    from custom_components.mydolphin_plus.managers import config_manager as cm_module
    from custom_components.mydolphin_plus.managers.config_manager import ConfigManager

    hass = MagicMock()
    entry = MagicMock()
    entry.entry_id = "test-entry"
    entry.title = "Test"

    cm = ConfigManager(hass, entry)
    cm._store = MagicMock()
    cm._store.async_load = AsyncMock(return_value={})
    cm._store.async_save = AsyncMock()
    cm._data = {
        STORAGE_DATA_ID_TOKEN: FAKE_ID_TOKEN,
        STORAGE_DATA_REFRESH_TOKEN: FAKE_REFRESH_TOKEN,
        STORAGE_DATA_MOTOR_UNIT_SERIAL: "N4720KMV",
    }

    with caplog.at_level(logging.DEBUG, logger=cm_module.__name__):
        await cm._save()

    _assert_no_secret_in_records(caplog.records)
    # And confirm the log site was actually hit.
    assert any("Storing config data" in r.getMessage() for r in caplog.records), (
        "expected the _save() debug log to fire"
    )


# --- SEC-04 : real ConfigManager._load() ------------------------------------


@pytest.mark.asyncio
async def test_sec04_config_manager_load_does_not_leak_tokens(caplog):
    """Drive the real ConfigManager._load() and assert tokens never appear."""
    from custom_components.mydolphin_plus.common.consts import (
        STORAGE_DATA_ID_TOKEN,
        STORAGE_DATA_REFRESH_TOKEN,
    )
    from custom_components.mydolphin_plus.managers import config_manager as cm_module
    from custom_components.mydolphin_plus.managers.config_manager import ConfigManager

    hass = MagicMock()
    entry = MagicMock()
    entry.entry_id = "test-entry"
    entry.title = "Test"

    cm = ConfigManager(hass, entry)
    cm._store = MagicMock()
    cm._store.async_load = AsyncMock(
        return_value={
            "test-entry": {
                STORAGE_DATA_ID_TOKEN: FAKE_ID_TOKEN,
                STORAGE_DATA_REFRESH_TOKEN: FAKE_REFRESH_TOKEN,
            }
        }
    )
    cm._store.async_save = AsyncMock()

    with caplog.at_level(logging.INFO, logger=cm_module.__name__):
        await cm._load()

    _assert_no_secret_in_records(caplog.records)
    assert any("loaded config data" in r.getMessage() for r in caplog.records), (
        "expected the _load() info log to fire"
    )


# --- SEC-02 : real AWSClient._debug_log_credentials_received() --------------


def test_sec02_aws_client_credentials_log_does_not_leak(caplog):
    """Call the real helper that initialize() uses and assert only lengths leak."""
    from custom_components.mydolphin_plus.managers import aws_client as aws_module
    from custom_components.mydolphin_plus.managers.aws_client import AWSClient

    with caplog.at_level(logging.DEBUG, logger=aws_module.__name__):
        AWSClient._debug_log_credentials_received(
            FAKE_AWS_ACCESS_KEY_ID, FAKE_AWS_SECRET, FAKE_AWS_SESSION_TOKEN
        )

    _assert_no_secret_in_records(caplog.records)
    assert any(
        "Obtained AWS IAM credentials" in r.getMessage() for r in caplog.records
    ), "expected the credentials-received debug log to fire"


# --- SEC-03 : real RestAPI._debug_log_api_data_updated() --------------------


def test_sec03_rest_api_data_log_redacts_sensitive_fields(caplog):
    """Call the real helper that update() uses and assert secrets are redacted."""
    from custom_components.mydolphin_plus.managers import rest_api as rest_api_module
    from custom_components.mydolphin_plus.managers.rest_api import RestAPI

    # We don't need a fully constructed RestAPI; the helper only touches
    # self.data via the `data` property. Build a tiny stand-in with the
    # right attribute shape.
    stand_in = MagicMock(spec=RestAPI)
    stand_in.data = {
        "Token": FAKE_AWS_SESSION_TOKEN,
        "AccessKeyId": FAKE_AWS_ACCESS_KEY_ID,
        "SecretAccessKey": FAKE_AWS_SECRET,
        "MotorUnitSerial": "N4720KMV",
        "harmless": "value",
    }

    with caplog.at_level(logging.DEBUG, logger=rest_api_module.__name__):
        # Invoke the unbound method on the stand-in so the real helper runs.
        RestAPI._debug_log_api_data_updated(stand_in)

    _assert_no_secret_in_records(caplog.records)
    assert any(
        "API Data updated" in r.getMessage() for r in caplog.records
    ), "expected the API-data-updated debug log to fire"


# --- Defense in depth: source-level grep for forbidden patterns -------------

# Catches a revert that removes the helper indirection and restores a raw
# f-string at the original site, even if the helper itself is still defined.

_LOGGER_FMT_CALL_RE = re.compile(
    r"_LOGGER\.\w+\(\s*f[\"'][^\"']*\{[^}]*\b(self\._data|self\.data)\b[^}]*\}[^\"']*[\"']",
    re.DOTALL,
)
_AWS_RAW_LOG_RE = re.compile(
    r"_LOGGER\.\w+\([^)]*\baws_(?:key|secret|token)\b(?![^)]*\blen\(aws_(?:key|secret|token))",
    re.DOTALL,
)


def _read_module_source(module) -> str:
    return Path(inspect.getfile(module)).read_text(encoding="utf-8")


def test_config_manager_source_has_no_raw_data_fstring_log():
    """SEC-01/04 regression: _LOGGER.{level}(f"...{self._data}...") forbidden."""
    from custom_components.mydolphin_plus.managers import config_manager as cm_module

    src = _read_module_source(cm_module)
    matches = _LOGGER_FMT_CALL_RE.findall(src)
    assert not matches, (
        f"Raw self._data dump found in a _LOGGER f-string call: {matches}"
    )


def test_rest_api_source_has_no_raw_data_fstring_log():
    """SEC-03 regression: _LOGGER.{level}(f"...{self.data}...") forbidden."""
    from custom_components.mydolphin_plus.managers import rest_api as rest_api_module

    src = _read_module_source(rest_api_module)
    matches = _LOGGER_FMT_CALL_RE.findall(src)
    assert not matches, (
        f"Raw self.data dump found in a _LOGGER f-string call: {matches}"
    )


def test_aws_client_source_has_no_raw_credentials_log():
    """SEC-02 regression: raw aws_key/aws_secret/aws_token in any _LOGGER call forbidden."""
    from custom_components.mydolphin_plus.managers import aws_client as aws_module

    src = _read_module_source(aws_module)
    # Only the helper definition references the raw names, always wrapped in len().
    # The initialize() call site now uses the helper, no raw reference there.
    matches = _AWS_RAW_LOG_RE.findall(src)
    assert not matches, (
        f"Raw aws_key/secret/token found in a _LOGGER call: {matches}"
    )


# --- Self-check on the assertion helper -------------------------------------


@pytest.mark.parametrize(
    "leaky_pattern",
    [
        "FAKE_REFRESH_TOKEN_BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
        "AKIAFAKEAKIAFAKEAKIA",
        "eyJraWQiOiJYWFhYIiwiYWxnIjoiUlMyNTYifQ.payload.sig",
    ],
)
def test_assertion_helper_catches_leaked_secrets(leaky_pattern):
    """Sanity check that _assert_no_secret_in_records flags known secrets."""
    record = logging.LogRecord(
        name="test",
        level=logging.DEBUG,
        pathname=__file__,
        lineno=0,
        msg=f"leaked: {leaky_pattern}",
        args=(),
        exc_info=None,
    )
    with pytest.raises(AssertionError, match="leaked into log"):
        _assert_no_secret_in_records([record])
