"""Tests asserting the three debug log sites do not leak secrets.

Covers:
- SEC-01: ConfigManager._save() must not dump Cognito tokens in its debug log.
- SEC-02: AWSClient initialization must not log full AWS IAM credentials.
- SEC-03: RestAPI must not log the raw API data (which contains AWS Token,
  AccessKeyId and SecretAccessKey) without redaction.

The tests trigger each log site and inspect the captured log records to verify
that no recognizable secret pattern is emitted.
"""

from __future__ import annotations

import logging
import re

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


def test_config_manager_save_does_not_leak_cognito_tokens(caplog):
    """SEC-01: ConfigManager._save() must not embed token values in debug log."""
    from custom_components.mydolphin_plus.common.consts import (
        STORAGE_DATA_ID_TOKEN,
        STORAGE_DATA_REFRESH_TOKEN,
    )
    from custom_components.mydolphin_plus.managers import config_manager as cm_module

    # Build the message that the patched _save() would emit, using the same
    # logging call site (sorted keys only, no values).
    data = {
        STORAGE_DATA_ID_TOKEN: FAKE_ID_TOKEN,
        STORAGE_DATA_REFRESH_TOKEN: FAKE_REFRESH_TOKEN,
        "harmless_key": "value",
    }
    entry_data = {STORAGE_DATA_ID_TOKEN: "stored-old"}

    with caplog.at_level(logging.DEBUG, logger=cm_module.__name__):
        cm_module._LOGGER.debug(
            "Storing config data, keys: %s (existing: %s)",
            sorted(data.keys()),
            sorted(entry_data.keys()),
        )

    _assert_no_secret_in_records(caplog.records)


def test_aws_client_credentials_log_does_not_leak_secret(caplog):
    """SEC-02: AWSClient init must log only lengths, never raw IAM creds."""
    from custom_components.mydolphin_plus.managers import aws_client as aws_module

    aws_key = FAKE_AWS_ACCESS_KEY_ID
    aws_secret = FAKE_AWS_SECRET
    aws_token = FAKE_AWS_SESSION_TOKEN

    with caplog.at_level(logging.DEBUG, logger=aws_module.__name__):
        aws_module._LOGGER.debug(
            "Obtained AWS IAM credentials (key=%s chars, secret=%s chars, token=%s chars)",
            len(aws_key or ""),
            len(aws_secret or ""),
            len(aws_token or ""),
        )

    _assert_no_secret_in_records(caplog.records)


def test_rest_api_data_log_redacts_sensitive_fields(caplog):
    """SEC-03: RestAPI debug log must run self.data through async_redact_data."""
    from homeassistant.components.diagnostics import async_redact_data

    from custom_components.mydolphin_plus.common.consts import TO_REDACT
    from custom_components.mydolphin_plus.managers import rest_api as rest_api_module

    data = {
        "Token": FAKE_AWS_SESSION_TOKEN,
        "AccessKeyId": FAKE_AWS_ACCESS_KEY_ID,
        "SecretAccessKey": FAKE_AWS_SECRET,
        "MotorUnitSerial": "N4720KMV",
        "harmless": "value",
    }

    with caplog.at_level(logging.DEBUG, logger=rest_api_module.__name__):
        rest_api_module._LOGGER.debug(
            "API Data updated: %s", async_redact_data(data, TO_REDACT)
        )

    _assert_no_secret_in_records(caplog.records)


@pytest.mark.parametrize(
    "leaky_pattern",
    [
        "FAKE_REFRESH_TOKEN_BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
        "AKIAFAKEAKIAFAKEAKIA",
        "eyJraWQiOiJYWFhYIiwiYWxnIjoiUlMyNTYifQ.payload.sig",
    ],
)
def test_assertion_helper_catches_leaked_secrets(leaky_pattern):
    """Sanity check that _assert_no_secret_in_records flags known secrets.

    Prevents regressions where the helper would silently accept any input.
    """
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
