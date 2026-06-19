"""Tests for BUG-11 — should_login and _token_details removed as dead code.

``ConfigManager.should_login`` was never called from anywhere in the
integration. Its definition conflated Cognito tokens (id-token,
refresh-token, id-token-expires-at) with AWS credential lifecycle
fields (last-token-fetch, last-aws-credentials-fetch, aws-credentials-
expiry) and serial numbers, all pulled from ``TOKEN_PARAMS``. As a
property it would have produced ``True`` whenever any of those fields
was ``None``, which is semantically wrong (a missing AWS credential
timestamp ≠ "you must reauthenticate"). The supporting
``_token_details`` helper was used only by ``should_login``.

Both are now removed. These tests pin the deletion.
"""

from __future__ import annotations


def test_bug11_should_login_property_is_gone():
    """ConfigManager.should_login is removed."""
    from custom_components.mydolphin_plus.managers.config_manager import ConfigManager

    assert not hasattr(ConfigManager, "should_login"), (
        "ConfigManager.should_login was reintroduced — it is dead code that "
        "conflates auth tokens with AWS credential timestamps"
    )


def test_bug11_token_details_helper_is_gone():
    """ConfigManager._token_details is removed (was only used by should_login)."""
    from custom_components.mydolphin_plus.managers.config_manager import ConfigManager

    assert not hasattr(ConfigManager, "_token_details"), (
        "ConfigManager._token_details was reintroduced — it was only used by "
        "the removed should_login property"
    )
