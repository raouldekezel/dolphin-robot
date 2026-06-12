"""Tests for the DIAG MQTT trace logger.

The trace logger is meant to be enabled on demand:

    logger:
      logs:
        custom_components.mydolphin_plus.managers.aws_client.mqtt: debug

When ON, every publish (>>) and every receive (<<) emits one line carrying
``time.monotonic_ns()``, the topic and the raw payload. Off-by-default so it
costs nothing in production.
"""

from __future__ import annotations

import logging
import re
from unittest.mock import MagicMock

import pytest


MQTT_LOGGER_NAME = "custom_components.mydolphin_plus.managers.aws_client.mqtt"


@pytest.fixture
def reset_mqtt_logger_level():
    """Make sure each test starts from a fresh, NOTSET state on the child logger."""
    lg = logging.getLogger(MQTT_LOGGER_NAME)
    original = lg.level
    yield
    lg.setLevel(original)


def test_mqtt_trace_off_by_default_no_emission(reset_mqtt_logger_level, caplog):
    """Without explicit DEBUG opt-in, the trace logger emits nothing."""
    from custom_components.mydolphin_plus.managers.aws_client import _mqtt_trace

    # Make sure the parent logger inherits the high default; the child is NOTSET.
    logging.getLogger(MQTT_LOGGER_NAME).setLevel(logging.WARNING)

    _mqtt_trace(">>", "topic/foo", '{"k": 1}')

    trace_records = [r for r in caplog.records if r.name == MQTT_LOGGER_NAME]
    assert trace_records == [], "no record should be emitted while DEBUG is off"


def test_mqtt_trace_on_emits_line_with_timestamp_topic_and_payload(
    reset_mqtt_logger_level, caplog
):
    """Once enabled, the trace must carry monotonic_ns, topic and payload."""
    from custom_components.mydolphin_plus.managers.aws_client import _mqtt_trace

    logging.getLogger(MQTT_LOGGER_NAME).setLevel(logging.DEBUG)

    with caplog.at_level(logging.DEBUG, logger=MQTT_LOGGER_NAME):
        _mqtt_trace(">>", "shadow/update", '{"state":{"desired":{"x":1}}}')

    records = [r for r in caplog.records if r.name == MQTT_LOGGER_NAME]
    assert len(records) == 1
    msg = records[0].getMessage()
    assert msg.startswith(">>")
    assert "topic=shadow/update" in msg
    assert '"x":1' in msg
    # Carries a monotonic-ns timestamp (digits-only after `t=`).
    assert re.search(r"\bt=\d+\b", msg), msg


def test_mqtt_trace_decodes_bytes_payload(reset_mqtt_logger_level, caplog):
    """When the callback hands us raw bytes (it usually does), they get decoded."""
    from custom_components.mydolphin_plus.managers.aws_client import _mqtt_trace

    logging.getLogger(MQTT_LOGGER_NAME).setLevel(logging.DEBUG)

    payload_bytes = '{"a": "café"}'.encode("utf-8")
    with caplog.at_level(logging.DEBUG, logger=MQTT_LOGGER_NAME):
        _mqtt_trace("<<", "shadow/update/accepted", payload_bytes)

    records = [r for r in caplog.records if r.name == MQTT_LOGGER_NAME]
    assert len(records) == 1
    msg = records[0].getMessage()
    assert "café" in msg
    assert msg.startswith("<<")


def test_publish_path_emits_trace_when_enabled(reset_mqtt_logger_level, caplog):
    """A real _publish call must trigger one >> trace line on the child logger."""
    from custom_components.mydolphin_plus.common.connectivity_status import (
        ConnectivityStatus,
    )
    from custom_components.mydolphin_plus.managers.aws_client import AWSClient

    logging.getLogger(MQTT_LOGGER_NAME).setLevel(logging.DEBUG)

    stub = MagicMock(spec=AWSClient)
    stub._status = ConnectivityStatus.CONNECTED
    stub._awsiot_client = MagicMock()
    stub._awsiot_client.publish.return_value = (MagicMock(), 42)
    stub._on_publish_completed_callback = MagicMock()

    with caplog.at_level(logging.DEBUG, logger=MQTT_LOGGER_NAME):
        AWSClient._publish(stub, "my/topic", {"a": 1})

    trace_records = [
        r for r in caplog.records if r.name == MQTT_LOGGER_NAME and r.getMessage().startswith(">>")
    ]
    assert len(trace_records) == 1
    assert "topic=my/topic" in trace_records[0].getMessage()


def test_message_callback_emits_trace_on_recv(reset_mqtt_logger_level, caplog):
    """A real _message_callback invocation must trigger one << trace line."""
    from custom_components.mydolphin_plus.managers.aws_client import AWSClient

    logging.getLogger(MQTT_LOGGER_NAME).setLevel(logging.DEBUG)

    stub = MagicMock(spec=AWSClient)
    stub._config_manager = MagicMock()
    stub._config_manager.motor_unit_serial = "N4720KMV"
    stub._topic_data = MagicMock()
    stub._topic_data.get_accepted = "irrelevant/get_accepted"
    stub._topic_data.update_accepted = "irrelevant/update_accepted"
    stub._topic_data.dynamic = "irrelevant/dynamic"
    stub._robot_family = None  # short-circuits the M700 branch

    with caplog.at_level(logging.DEBUG, logger=MQTT_LOGGER_NAME):
        # Payload that doesn't match any branch — we just want the trace.
        AWSClient._message_callback(
            stub,
            topic="some/other/topic",
            payload=b'{"k": 1}',
            dup=False,
            qos=1,
            retain=False,
        )

    trace_records = [
        r for r in caplog.records if r.name == MQTT_LOGGER_NAME and r.getMessage().startswith("<<")
    ]
    assert len(trace_records) == 1
    assert "topic=some/other/topic" in trace_records[0].getMessage()
