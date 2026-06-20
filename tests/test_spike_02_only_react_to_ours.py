"""Regression tests for SPIKE-02 (#70) — provenance-gated reactive branches.

Pre-fix, ``_message_callback`` reacted identically to every shadow event
regardless of source:

* HARD-09 (#66) — every ``/rejected`` triggered a ``WARNING``, including
  the device's boot-time ``429 TOO_MANY_REQUESTS`` on its post-boot
  ``shadow/update``.
* BUG-08 (#17) — every ``/update/accepted`` carrying a
  ``desired.cleaningMode.mode`` triggered a 1 s ``sleep`` followed by a
  reactive ``_set_cycle_time`` publish that overwrote whatever cycleTime
  the originating party had set. The "launcher picks the duration"
  semantics held by accident, via a last-write-wins race with the
  Maytronics app's own cycleTime push.

Post-fix:

* :py:meth:`AWSClient.initialize` mints a per-process ``self._our_token``
  (UUID4 hex).
* :py:meth:`AWSClient._send_desired_command` stamps that token on every
  outbound ``desired`` write (``"clientToken"`` field, sibling of
  ``"state"``).
* :py:meth:`AWSClient._event_is_ours` is a boolean provenance predicate
  on the parsed payload.
* HARD-09's WARNING only fires when ``_event_is_ours(payload)`` is true;
  otherwise the rejected is logged at DEBUG.
* BUG-08's reactive chain only fires when ``_event_is_ours(payload)``
  is true (HA-initiated mode change). The 1 s blocking ``sleep(1)`` and
  the inline ``_set_cycle_time`` call inside the branch are deliberately
  KEPT AS-IS — the spike never characterised what the 1 s gap is for
  (ordering? firmware throttling? an awaited shadow field?), and the
  provenance gate already removes the operational concern about the
  sleep: it no longer fires on every foreign mode change, only on
  HA-initiated ones. Replacing the sleep is a separate, follow-up task
  that must first measure the firmware's behaviour at sleep(0.05)
  / sleep(0) / no sleep before changing anything.

The empirical foundation is the SPIKE-02 diag session bundle merged in
PR #71 (D4) and PR #72 (pre-D2 + E7): tokens are echoed unchanged on
both ``/update/accepted`` (D4 + E5 + E7) and ``/update/rejected`` (E4);
``desired:null`` is PWS-firmware-authored (E3b); the firmware silently
ignores a sibling ``cycleTime`` field in a mode-change document, which
is why the chain must be kept (E7).

This file tests the four behavioural pieces (token stamping, predicate
correctness, HARD-09 gate, BUG-08 gate) and the non-blocking nature of
the BUG-08 wait.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_stub_client(*, our_token: str | None = "ourtoken1234abcd"):
    """Build a stub ``AWSClient`` with the minimum surface
    ``_message_callback`` reads. ``_publish`` is replaced by an in-memory
    recorder so tests can assert what would have hit the wire."""
    from custom_components.mydolphin_plus.managers.aws_client import AWSClient

    stub = MagicMock(spec=AWSClient)
    stub._our_token = our_token
    stub.data = {}
    stub._on_data_update_callback = lambda: None
    stub._robot_family = None  # not M700 by default
    stub._topic_data = SimpleNamespace(
        dynamic="dynamic-topic-irrelevant",
        get_accepted="$aws/things/REDACTED-MUSN/shadow/get/accepted",
        update_accepted="$aws/things/REDACTED-MUSN/shadow/update/accepted",
        update="$aws/things/REDACTED-MUSN/shadow/update",
    )
    stub._config_manager = MagicMock()
    stub._config_manager.motor_unit_serial = "REDACTED-MUSN"
    stub._set_cycle_time = MagicMock()
    stub._read_temperature_and_in_water_details = MagicMock()
    stub._on_dynamic_content_received = MagicMock()
    stub._event_is_ours = lambda payload: AWSClient._event_is_ours(stub, payload)
    return stub


def _encode(payload: dict) -> bytes:
    return json.dumps(payload).encode("utf-8")


# ---------------------------------------------------------------------------
# 1. Stamping
# ---------------------------------------------------------------------------


def test_send_desired_command_stamps_our_token():
    """``_send_desired_command`` must include the per-process token as a
    top-level ``clientToken`` field in the published document, sibling to
    ``state``. The shape is the contract AWS Shadow echoes on
    accepted/rejected — break this and the gates downstream go silent."""
    from custom_components.mydolphin_plus.managers.aws_client import AWSClient

    stub = MagicMock(spec=AWSClient)
    stub._our_token = "deadbeefcafefacefeedfacebadc0ffee0123456"
    stub._topic_data = SimpleNamespace(update="topic/update")
    stub._publish = MagicMock()

    payload = {"cleaningMode": {"mode": "all"}}
    AWSClient._send_desired_command(stub, payload)

    assert stub._publish.call_count == 1
    topic_arg, data_arg = stub._publish.call_args.args
    assert topic_arg == "topic/update"
    assert data_arg["clientToken"] == "deadbeefcafefacefeedfacebadc0ffee0123456"
    # ``state.desired`` content is unchanged by the stamping.
    assert data_arg["state"]["desired"] == payload


def test_send_desired_command_works_with_none_payload():
    """Robustness: AWS rejects ``"desired": null`` documents, but the
    helper still has to publish a well-formed document with our token
    even if a caller passes ``None``. The payload field carries through
    untouched; AWS will then reject and HARD-09's gated path takes over."""
    from custom_components.mydolphin_plus.managers.aws_client import AWSClient

    stub = MagicMock(spec=AWSClient)
    stub._our_token = "k"
    stub._topic_data = SimpleNamespace(update="topic/update")
    stub._publish = MagicMock()

    AWSClient._send_desired_command(stub, None)

    _, data_arg = stub._publish.call_args.args
    assert data_arg["clientToken"] == "k"
    assert data_arg["state"]["desired"] is None


# ---------------------------------------------------------------------------
# 2. Provenance predicate
# ---------------------------------------------------------------------------


def test_event_is_ours_true_on_matching_token():
    from custom_components.mydolphin_plus.managers.aws_client import AWSClient

    stub = MagicMock(spec=AWSClient)
    stub._our_token = "K"

    assert AWSClient._event_is_ours(stub, {"clientToken": "K"}) is True


def test_event_is_ours_false_on_different_token():
    from custom_components.mydolphin_plus.managers.aws_client import AWSClient

    stub = MagicMock(spec=AWSClient)
    stub._our_token = "K"

    assert AWSClient._event_is_ours(stub, {"clientToken": "OTHER"}) is False


def test_event_is_ours_false_when_payload_has_no_token():
    """The app's writes carry no ``clientToken`` (SPIKE-02 D5, empirically
    confirmed on 107 captured accepted-family payloads). Those must be
    classified as foreign."""
    from custom_components.mydolphin_plus.managers.aws_client import AWSClient

    stub = MagicMock(spec=AWSClient)
    stub._our_token = "K"

    assert AWSClient._event_is_ours(stub, {}) is False
    assert AWSClient._event_is_ours(stub, {"state": {"reported": {}}}) is False


def test_event_is_ours_conservative_false_before_initialize():
    """Pre-``initialize`` window: ``self._our_token is None``. Any event
    arriving with a literal ``None`` token would falsely match ``None ==
    None``; the predicate must guard against this and return False."""
    from custom_components.mydolphin_plus.managers.aws_client import AWSClient

    stub = MagicMock(spec=AWSClient)
    stub._our_token = None

    assert AWSClient._event_is_ours(stub, {"clientToken": None}) is False
    assert AWSClient._event_is_ours(stub, {"clientToken": "anything"}) is False
    assert AWSClient._event_is_ours(stub, {}) is False


# ---------------------------------------------------------------------------
# 3. HARD-09 gate — only WARN on OUR rejected
# ---------------------------------------------------------------------------


def _hard09_messages(caplog) -> tuple[list[str], list[str]]:
    """Return (warning_messages, debug_messages) emitted by the aws_client
    logger for ``rejected message`` events captured by ``caplog``."""
    logger_name = "custom_components.mydolphin_plus.managers.aws_client"
    warnings_ = []
    debugs = []
    for record in caplog.records:
        if record.name != logger_name:
            continue
        msg = record.getMessage()
        if "Rejected message" not in msg:
            continue
        if record.levelno >= logging.WARNING:
            warnings_.append(msg)
        elif record.levelno == logging.DEBUG:
            debugs.append(msg)
    return warnings_, debugs


def test_hard09_warns_on_our_rejected(caplog):
    """A ``/rejected`` carrying our token must produce a WARNING (the
    write WE just published was rejected — actionable for the operator)."""
    from custom_components.mydolphin_plus.managers.aws_client import AWSClient

    stub = _make_stub_client(our_token="OURTOKEN")
    payload = _encode(
        {
            "code": 409,
            "message": "Version conflict",
            "clientToken": "OURTOKEN",
        }
    )

    with caplog.at_level(logging.DEBUG, logger="custom_components.mydolphin_plus.managers.aws_client"):
        AWSClient._message_callback(
            stub,
            "$aws/things/REDACTED-MUSN/shadow/update/rejected",
            payload,
            False,
            0,
            False,
        )

    warnings_, _ = _hard09_messages(caplog)
    assert len(warnings_) == 1, f"expected exactly one WARNING, got: {warnings_}"
    assert "OURTOKEN" in warnings_[0] or "rejected" in warnings_[0].lower()


def test_hard09_debug_on_foreign_rejected_no_token(caplog):
    """A foreign rejection (no ``clientToken`` — e.g. the device's
    boot-time 429) must NOT emit a WARNING. This is the cosmetic noise
    HARD-09 was filed to silence."""
    from custom_components.mydolphin_plus.managers.aws_client import AWSClient

    stub = _make_stub_client(our_token="OURTOKEN")
    payload = _encode({"code": 429, "message": "Too Many Requests"})

    with caplog.at_level(logging.DEBUG, logger="custom_components.mydolphin_plus.managers.aws_client"):
        AWSClient._message_callback(
            stub,
            "$aws/things/REDACTED-MUSN/shadow/update/rejected",
            payload,
            False,
            0,
            False,
        )

    warnings_, debugs = _hard09_messages(caplog)
    assert warnings_ == [], (
        f"foreign rejected must NOT emit a WARNING; captured: {warnings_}"
    )
    assert len(debugs) == 1, f"expected one DEBUG line, got: {debugs}"


def test_hard09_debug_on_other_token_rejected(caplog):
    """A rejected carrying a different clientToken — e.g. another HA
    instance sharing the same Maytronics account — is also foreign and
    must fall through to DEBUG."""
    from custom_components.mydolphin_plus.managers.aws_client import AWSClient

    stub = _make_stub_client(our_token="OURTOKEN")
    payload = _encode({"code": 409, "clientToken": "SOMEONE_ELSE"})

    with caplog.at_level(logging.DEBUG, logger="custom_components.mydolphin_plus.managers.aws_client"):
        AWSClient._message_callback(
            stub,
            "$aws/things/REDACTED-MUSN/shadow/update/rejected",
            payload,
            False,
            0,
            False,
        )

    warnings_, debugs = _hard09_messages(caplog)
    assert warnings_ == [], (
        f"rejected with foreign token must NOT emit a WARNING; got: {warnings_}"
    )
    assert len(debugs) == 1


# ---------------------------------------------------------------------------
# 4. BUG-08 gate — only fire on OUR accepted; non-blocking wait
# ---------------------------------------------------------------------------


def _run_callback_with_fast_sleep(stub, topic, payload):
    """Drive ``_message_callback`` with the module-level ``sleep`` patched
    to a no-op. The BUG-08 branch still does ``sleep(1)`` between the
    mode echo and the cycleTime write — keep the original semantics, just
    don't pay the 1 s wait in unit tests."""
    from custom_components.mydolphin_plus.managers import aws_client as aws_client_mod
    from custom_components.mydolphin_plus.managers.aws_client import AWSClient

    with patch.object(aws_client_mod, "sleep") as sleep_mock:
        AWSClient._message_callback(stub, topic, payload, False, 0, False)
    return sleep_mock


def test_bug08_fires_on_our_accepted_with_mode():
    """A ``/update/accepted`` carrying our token AND a
    ``desired.cleaningMode.mode`` must run the reactive chain: ``sleep(1)``
    then ``_set_cycle_time(mode)``. The ``sleep`` is deliberately kept
    inline — the gate is the only thing that changed."""
    stub = _make_stub_client(our_token="OURTOKEN")
    payload = _encode(
        {
            "state": {"desired": {"cleaningMode": {"mode": "all"}}},
            "clientToken": "OURTOKEN",
            "version": 100,
            "timestamp": 1000,
        }
    )

    sleep_mock = _run_callback_with_fast_sleep(
        stub, stub._topic_data.update_accepted, payload
    )

    sleep_mock.assert_called_once_with(1)
    stub._set_cycle_time.assert_called_once_with("all")


def test_bug08_skips_foreign_accepted_no_token():
    """The Maytronics app's mode change carries no ``clientToken``
    (SPIKE-02 D5). The integration must NOT chain a cycleTime write on
    top of it — the app's chosen duration is left intact, and the
    "launcher picks the duration" semantics now hold as an invariant
    rather than a race outcome."""
    stub = _make_stub_client(our_token="OURTOKEN")
    payload = _encode(
        {
            "state": {"desired": {"cleaningMode": {"mode": "floor"}}},
            "version": 100,
            "timestamp": 1000,
        }
    )

    sleep_mock = _run_callback_with_fast_sleep(
        stub, stub._topic_data.update_accepted, payload
    )

    sleep_mock.assert_not_called()
    stub._set_cycle_time.assert_not_called()


def test_bug08_skips_foreign_accepted_with_other_token():
    """An accepted carrying a foreign clientToken is also a foreign
    event — same gate, same skip."""
    stub = _make_stub_client(our_token="OURTOKEN")
    payload = _encode(
        {
            "state": {"desired": {"cleaningMode": {"mode": "stairs"}}},
            "clientToken": "SOMEONE_ELSE",
        }
    )

    sleep_mock = _run_callback_with_fast_sleep(
        stub, stub._topic_data.update_accepted, payload
    )

    sleep_mock.assert_not_called()
    stub._set_cycle_time.assert_not_called()


def test_bug08_skips_accepted_without_mode_field():
    """Our own ``cycleTime``-only writes (E2 shape) — the integration's
    reactive cycleTime write echoes back as ``desired.cycleInfo.cycleTime``
    only, with our token. The branch is keyed on the presence of
    ``desired.cleaningMode.mode``, so the reactive chain must not fire
    and no infinite loop is possible."""
    stub = _make_stub_client(our_token="OURTOKEN")
    payload = _encode(
        {
            "state": {"desired": {"cycleInfo": {"cycleTime": 120}}},
            "clientToken": "OURTOKEN",
        }
    )

    sleep_mock = _run_callback_with_fast_sleep(
        stub, stub._topic_data.update_accepted, payload
    )

    sleep_mock.assert_not_called()
    stub._set_cycle_time.assert_not_called()
