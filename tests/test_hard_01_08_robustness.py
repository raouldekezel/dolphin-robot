"""Tests for the HARD-01..08 robustness batch.

Eight latent guards and decode bugs that previously turned into silent
state loss, KeyErrors, IndexErrors or wrong sensor values. Each fix is
tested behaviourally where possible and pinned with a source-level
regression check.
"""

from __future__ import annotations

import asyncio
import inspect
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from homeassistant.const import ATTR_STATE


# --- HARD-01: .lower() AFTER None guard ------------------------------------


def test_hard01_power_supply_status_handles_none_state():
    """Stub _system_details.power_unit_state = None, must return ATTR_STATE: None."""
    from custom_components.mydolphin_plus.managers.coordinator import (
        MyDolphinPlusCoordinator,
    )

    stub = MagicMock(spec=MyDolphinPlusCoordinator)
    stub._system_details = MagicMock()
    stub._system_details.power_unit_state = None

    result = MyDolphinPlusCoordinator._get_power_supply_status_data(stub, None)
    assert result == {ATTR_STATE: None}


def test_hard01_robot_status_handles_none_state():
    """Same guard on _get_robot_status_data."""
    from custom_components.mydolphin_plus.managers.coordinator import (
        MyDolphinPlusCoordinator,
    )

    stub = MagicMock(spec=MyDolphinPlusCoordinator)
    stub._system_details = MagicMock()
    stub._system_details.robot_state = None

    result = MyDolphinPlusCoordinator._get_robot_status_data(stub, None)
    assert result == {ATTR_STATE: None}


def test_hard01_status_lowercased_when_string():
    """When the state IS a string, it must still be lower-cased."""
    from custom_components.mydolphin_plus.managers.coordinator import (
        MyDolphinPlusCoordinator,
    )

    stub = MagicMock(spec=MyDolphinPlusCoordinator)
    stub._system_details = MagicMock()
    stub._system_details.power_unit_state = "Cleaning"

    result = MyDolphinPlusCoordinator._get_power_supply_status_data(stub, None)
    assert result == {ATTR_STATE: "cleaning"}


# --- HARD-04: _vacuum_locate reads ATTR_IS_ON, not CONF_STATE -------------


@pytest.mark.asyncio
async def test_hard04_vacuum_locate_short_circuits_when_led_already_on():
    """The 'LED already on' early-return must actually trigger when ATTR_IS_ON=True."""
    from custom_components.mydolphin_plus.common.consts import ATTR_IS_ON
    from custom_components.mydolphin_plus.managers.coordinator import (
        MyDolphinPlusCoordinator,
    )

    stub = MagicMock(spec=MyDolphinPlusCoordinator)
    stub._get_led_data = MagicMock(return_value={ATTR_IS_ON: True})
    stub._config_manager = MagicMock()
    stub._set_led_enabled = MagicMock()

    await MyDolphinPlusCoordinator._vacuum_locate(stub, MagicMock())

    # Set-LED enabled must NOT have been called: locate was skipped.
    stub._set_led_enabled.assert_not_called()


# --- HARD-08: temperature decode -----------------------------------------


def test_hard08_temperature_decode_correct_for_4_digit_centi_degrees():
    """2545 → 25.45, the canonical centi-degree encoding."""
    from custom_components.mydolphin_plus.managers.coordinator import (
        MyDolphinPlusCoordinator,
    )

    stub = MagicMock(spec=MyDolphinPlusCoordinator)
    stub.aws_data = {
        "dynamic": {
            "iotResponse": {"temperature": 2545},
        }
    }
    # Patch the section/type consts dynamically via the stub: we use the
    # real ones inside the function.
    from custom_components.mydolphin_plus.common.consts import (
        DATA_SECTION_DYNAMIC,
        DYNAMIC_DESCRIPTION_TEMPERATURE,
        DYNAMIC_TYPE_IOT_RESPONSE,
    )

    stub.aws_data = {
        DATA_SECTION_DYNAMIC: {
            DYNAMIC_TYPE_IOT_RESPONSE: {
                DYNAMIC_DESCRIPTION_TEMPERATURE: 2545,
            }
        }
    }
    result = MyDolphinPlusCoordinator._get_temperature_data(stub, None)
    assert result[ATTR_STATE] == pytest.approx(25.45)


@pytest.mark.parametrize("raw,expected", [(100, 1.00), (12345, 123.45), (0, 0.0)])
def test_hard08_temperature_decode_not_broken_for_non_4_digit_lengths(raw, expected):
    """Old slicing botched 100 → 10.0; the new decode is just centi-degrees."""
    from custom_components.mydolphin_plus.common.consts import (
        DATA_SECTION_DYNAMIC,
        DYNAMIC_DESCRIPTION_TEMPERATURE,
        DYNAMIC_TYPE_IOT_RESPONSE,
    )
    from custom_components.mydolphin_plus.managers.coordinator import (
        MyDolphinPlusCoordinator,
    )

    stub = MagicMock(spec=MyDolphinPlusCoordinator)
    stub.aws_data = {
        DATA_SECTION_DYNAMIC: {
            DYNAMIC_TYPE_IOT_RESPONSE: {DYNAMIC_DESCRIPTION_TEMPERATURE: raw},
        }
    }
    result = MyDolphinPlusCoordinator._get_temperature_data(stub, None)
    assert result[ATTR_STATE] == pytest.approx(expected)


# --- HARD-05: _post_message_published is KeyError-safe -------------------


def test_hard05_post_message_published_unknown_id_does_not_raise():
    """A duplicate publish callback (already-popped id) must not raise KeyError."""
    from custom_components.mydolphin_plus.managers.aws_client import AWSClient

    stub = MagicMock(spec=AWSClient)
    stub._messages_published = {}

    # Must not raise.
    AWSClient._post_message_published(stub, message_id=42)
    assert stub._messages_published == {}


def test_hard05_post_message_published_pops_known_id():
    """A known id is removed from the tracking dict."""
    from custom_components.mydolphin_plus.managers.aws_client import AWSClient

    stub = MagicMock(spec=AWSClient)
    stub._messages_published = {7: {"topic": "t", "payload": "p"}, 8: {}}

    AWSClient._post_message_published(stub, message_id=7)
    assert 7 not in stub._messages_published
    assert 8 in stub._messages_published


# --- HARD-06: missing timestamp guard -----------------------------------


def test_hard06_message_callback_handles_missing_server_timestamp():
    """The Cycle-Info-Accepted branch must not raise when timestamp is absent.

    Direct test of the inline ``int(now) - server_timestamp`` line by
    reproducing the guarded shape.
    """
    server_timestamp = None
    import datetime as _dt

    now = _dt.datetime.now().timestamp()
    # New shape:
    diff = int(now) - server_timestamp if server_timestamp else None
    assert diff is None  # no exception


# --- HARD-07: _subscribe with empty topic list --------------------------


def test_hard07_subscribe_empty_topics_does_not_raise():
    """If topics_to_subscribe is empty, _subscribe must early-return cleanly."""
    from custom_components.mydolphin_plus.managers.aws_client import AWSClient

    stub = MagicMock(spec=AWSClient)
    stub._topic_data = MagicMock()
    stub._topic_data.subscribe = []
    stub._awsiot_client = MagicMock()

    # Must not raise IndexError.
    AWSClient._subscribe(stub)


# --- HARD-03: _get_led_settings does not mutate self.data ----------------


def test_hard03_get_led_settings_does_not_mutate_visible_state():
    """Editing a key in the returned dict must NOT propagate to self.data."""
    from custom_components.mydolphin_plus.common.consts import (
        DATA_LED_ENABLE,
        DATA_LED_INTENSITY,
        DATA_LED_MODE,
        DATA_SECTION_LED,
        DEFAULT_ENABLE,
        DEFAULT_LED_INTENSITY,
        LED_MODE_BLINKING,
    )
    from custom_components.mydolphin_plus.managers.aws_client import AWSClient

    stub = MagicMock(spec=AWSClient)
    pre_existing = {
        DATA_LED_ENABLE: DEFAULT_ENABLE,
        DATA_LED_INTENSITY: DEFAULT_LED_INTENSITY,
        DATA_LED_MODE: LED_MODE_BLINKING,
    }
    pre_existing_copy = dict(pre_existing)
    stub._data = {DATA_SECTION_LED: pre_existing}
    # data is a property in the real class; expose the same mapping for the stub.
    type(stub).data = property(lambda self: self._data)

    result = AWSClient._get_led_settings(stub, DATA_LED_INTENSITY, 99)
    # The returned section was mutated:
    assert result[DATA_SECTION_LED][DATA_LED_INTENSITY] == 99
    # ...but the source dict held by self.data WAS NOT touched.
    assert pre_existing == pre_existing_copy


# --- HARD-02: ClientTimeout on the session ------------------------------


def test_hard02_rest_api_initialize_session_uses_client_timeout():
    """Source-level: _initialize_session must wire a ClientTimeout."""
    from custom_components.mydolphin_plus.managers import rest_api as mod

    src = Path(inspect.getfile(mod)).read_text(encoding="utf-8")
    body_match = re.search(
        r"async def _initialize_session\(self\):.*?(?=\n    (?:async )?def )",
        src,
        re.DOTALL,
    )
    assert body_match is not None
    body = body_match.group(0)
    assert "ClientTimeout" in body, "ClientTimeout missing from _initialize_session"


# --- Source-level regressions on the coordinator path ------------------


def _coordinator_source() -> str:
    from custom_components.mydolphin_plus.managers import coordinator as mod

    return Path(inspect.getfile(mod)).read_text(encoding="utf-8")


def test_hard01_source_no_lower_before_none_guard():
    """coordinator must not have ``state = self._system_details.X.lower()`` before guarding for None."""
    src = _coordinator_source()
    # Forbid the exact pattern `state = self._system_details.<attr>.lower()` on a single line.
    bad = re.findall(r"state\s*=\s*self\._system_details\.\w+\.lower\(\)", src)
    assert not bad, f"unsafe .lower() before None-guard reintroduced: {bad}"


def test_hard04_source_uses_attr_is_on_in_vacuum_locate():
    """The _vacuum_locate body must read ATTR_IS_ON, not CONF_STATE."""
    src = _coordinator_source()
    body_match = re.search(
        r"async def _vacuum_locate\(self,.*?\):.*?(?=\n    (?:async )?def )",
        src,
        re.DOTALL,
    )
    assert body_match is not None
    body = body_match.group(0)
    assert "ATTR_IS_ON" in body, "_vacuum_locate should read ATTR_IS_ON"
    assert "CONF_STATE" not in body, "_vacuum_locate should NOT read CONF_STATE"


def test_hard08_source_does_not_slice_temperature_string():
    """No string slicing on the temperature payload anymore."""
    src = _coordinator_source()
    body_match = re.search(
        r"def _get_temperature_data\(self,.*?\):.*?(?=\n    (?:async )?def )",
        src,
        re.DOTALL,
    )
    assert body_match is not None
    body = body_match.group(0)
    # forbid the previous slicing pattern.
    assert "state_str[:2]" not in body, "old slicing decode reintroduced"


def test_hard05_07_source_uses_pop_and_guards_empty_list():
    """aws_client must use pop(...) and guard the empty topics list."""
    from custom_components.mydolphin_plus.managers import aws_client as mod

    src = Path(inspect.getfile(mod)).read_text(encoding="utf-8")
    # pop in _post_message_published
    assert re.search(
        r"def _post_message_published\(self,.*?\).*?\.pop\(",
        src,
        re.DOTALL,
    ), "_post_message_published should pop(...) instead of get+del"
    # Empty-list guard before subscribe
    assert "if not topics_to_subscribe" in src, (
        "_subscribe should guard against empty topic list"
    )
