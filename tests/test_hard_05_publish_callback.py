"""Regression tests for HARD-05 — `_post_message_published` KeyError.

Issue #26 originally reported a possible ``KeyError`` on
``del self._messages_published[message_id]`` after a ``.get(..., {})``
fallback. The chosen fix does not paper over the ``KeyError`` — it
removes the whole ``_messages_published`` bookkeeping. Topic and payload
are now bound directly into the completion callback via
``functools.partial``, so:

* the dict (and its structural ``KeyError`` risk) is gone;
* the permanent leak that the buggy layout allowed on failed publishes
  is impossible by construction — the ``partial`` context dies with the
  future;
* ``future.result()`` is now wrapped in ``try/except``, so awscrt
  failures (``AWS_ERROR_MQTT_CONNECTION_DESTROYED`` on teardown, the
  "not connected" call-time path) surface through the integration's
  logger instead of ``concurrent.futures``' opaque "exception calling
  callback" line.

These tests are behavioural per CHORE-02 — no source greps, no
`inspect.getsource`. They exercise the real ``_publish`` binding site
end-to-end and drive the completion callback directly on a real
instance built via ``__new__`` (bypassing ``__init__``'s executor loop
allocation).
"""

from __future__ import annotations

from concurrent.futures import Future
import logging
from unittest.mock import MagicMock

from custom_components.mydolphin_plus.common.connectivity_status import (
    ConnectivityStatus,
)
from custom_components.mydolphin_plus.managers.aws_client import AWSClient

LOGGER_NAME = "custom_components.mydolphin_plus.managers.aws_client"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bare_client() -> AWSClient:
    """Return an ``AWSClient`` instance without running ``__init__``.

    ``__init__`` spawns an event loop when ``hass is None`` and reaches
    into ``ConfigManager.entry_id`` — neither is relevant to the
    publish-callback contract. ``__new__`` sidesteps both.
    """
    return AWSClient.__new__(AWSClient)


# ---------------------------------------------------------------------------
# F1 — the removed bookkeeping must stay removed, at the instance level
# ---------------------------------------------------------------------------


def test_removed_bookkeeping_never_reappears_on_instance():
    """The ``_messages_published`` dict, its ``_pre``/``_post`` helpers,
    and the ``__init__`` lambda were the shared, cross-thread mutable
    state that HARD-05 came from. All four must stay gone.

    A ``__new__``-built instance would not catch a regression where the
    bookkeeping is reintroduced at instance level from ``__init__`` — so
    this test drives the real constructor with a MagicMock hass (its
    ``.loop`` attribute is enough to satisfy the branch) and then checks
    the attribute set that a live ``AWSClient`` carries.
    """
    hass = MagicMock()
    config_manager = MagicMock()
    config_manager.entry_id = "hard-05-test-entry"

    client = AWSClient(hass, config_manager, lambda: None)

    # Instance-only attributes — a class-level assertion cannot catch these.
    assert not hasattr(client, "_messages_published")
    assert not hasattr(client, "_on_publish_completed_callback")

    # Methods — the ``_pre``/``_post`` helpers must not come back at either level.
    assert not hasattr(client, "_pre_publish_message")
    assert not hasattr(client, "_post_message_published")
    assert not hasattr(AWSClient, "_pre_publish_message")
    assert not hasattr(AWSClient, "_post_message_published")


# ---------------------------------------------------------------------------
# F3 — the binding site (`_publish`) is what carries the HARD-05 contract
# ---------------------------------------------------------------------------


def test_publish_binds_own_context_and_logs_submission(caplog):
    """`_publish` must log the submission with the packet id and hand
    the completion callback a partial that carries *this call's* topic
    and payload — resolving the future then emits the completion line
    with the same correlator.
    """
    client = _bare_client()
    client._status = ConnectivityStatus.CONNECTED

    future: Future = Future()
    client._awsiot_client = MagicMock()
    client._awsiot_client.publish.return_value = (future, 123)

    topic = "$aws/things/REDACTED-MUSN/shadow/update"
    payload = {"state": {"desired": {"led": {"ledEnable": 1}}}}

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        client._publish(topic, payload)

        # Submission — must carry #packet_id + topic (the correlator anchor).
        submission = [
            r for r in caplog.records if r.getMessage().startswith("Publishing #")
        ]
        assert submission, "expected a submission DEBUG log line"
        assert "#123" in submission[-1].getMessage()
        assert topic in submission[-1].getMessage()
        assert '"ledEnable": 1' in submission[-1].getMessage()

        # Resolve the future — awscrt calls the done-callback synchronously
        # from the setting thread when the future is already set. Same effect.
        future.set_result(None)

    completions = [
        r
        for r in caplog.records
        if "MQTT publish" in r.getMessage() and "completed" in r.getMessage()
    ]
    assert completions, "expected a completion DEBUG log line"
    msg = completions[-1].getMessage()
    assert "#123" in msg
    assert topic in msg
    assert '"ledEnable": 1' in msg
    assert completions[-1].levelno == logging.DEBUG, (
        "success payload dumps must be DEBUG, not INFO — SPIKE-02 clientToken"
        " is stamped on every desired write and must not leak at INFO."
    )


def test_two_interleaved_publishes_carry_distinct_contexts(caplog):
    """The historical HARD-05 shape: two in-flight publishes complete
    out of order. Each completion callback must carry *its own*
    submission's context — the fix hinges on this. If the ``partial``
    binding ever regressed to a shared closure over a loop variable
    (or the dict was reintroduced and one entry overwrote the other),
    the two completions would report the same topic/payload.
    """
    client = _bare_client()
    client._status = ConnectivityStatus.CONNECTED
    client._awsiot_client = MagicMock()

    future_a: Future = Future()
    future_b: Future = Future()
    topic_a = "$aws/things/REDACTED-MUSN/shadow/update"
    topic_b = "$aws/things/REDACTED-MUSN/shadow/get"
    payload_a = {"state": {"desired": {"led": {"ledEnable": 1}}}}
    payload_b = {}

    client._awsiot_client.publish.side_effect = [
        (future_a, 100),
        (future_b, 200),
    ]

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        client._publish(topic_a, payload_a)
        client._publish(topic_b, payload_b)

        # Complete B *before* A — the swap the reported HARD-05 story hinges on.
        future_b.set_result(None)
        future_a.set_result(None)

    completions = [
        r
        for r in caplog.records
        if "MQTT publish" in r.getMessage() and "completed" in r.getMessage()
    ]
    assert len(completions) == 2, "expected one completion line per publish"

    # Ordered by resolution: B first, then A.
    msg_b, msg_a = completions[0].getMessage(), completions[1].getMessage()

    assert "#200" in msg_b and topic_b in msg_b
    assert "#100" in msg_a and topic_a in msg_a

    # Payload correlation — the discriminator that catches a shared closure.
    assert '"ledEnable": 1' in msg_a
    assert '"ledEnable"' not in msg_b


def test_publish_failure_is_logged_not_swallowed(caplog):
    """A failed future must surface through our logger with topic and
    packet id. Before the fix, ``future.result()`` was called unguarded;
    ``concurrent.futures`` swallowed the exception in an opaque
    "exception calling callback" line — invisible to the user.
    """
    client = _bare_client()
    client._status = ConnectivityStatus.CONNECTED
    client._awsiot_client = MagicMock()

    future: Future = Future()
    client._awsiot_client.publish.return_value = (future, 7)
    boom = RuntimeError("AWS_ERROR_MQTT_CONNECTION_DESTROYED")

    topic = "$aws/things/REDACTED-MUSN/shadow/update"
    payload = {"state": {"desired": {"led": {"ledEnable": 0}}}}

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        client._publish(topic, payload)
        future.set_exception(boom)

    failures = [
        r
        for r in caplog.records
        if r.levelno == logging.ERROR and "failed" in r.getMessage()
    ]
    assert failures, "expected an ERROR log line for the failed future"
    rec = failures[-1]
    assert "#7" in rec.getMessage()
    assert topic in rec.getMessage()
    # ``_LOGGER.exception`` attaches the exception info to the record —
    # the traceback is the intentional carrier for the failure context.
    assert rec.exc_info is not None
    assert rec.exc_info[1] is boom

    # And the success line must NOT also fire — otherwise the failure
    # is visually cancelled one message later.
    completed_lines = [r for r in caplog.records if "completed" in r.getMessage()]
    assert not completed_lines, "success line must not fire after a failure"


def test_publish_when_not_connected_logs_error_and_registers_no_callback(caplog):
    """A publish attempted while ``_status`` is not ``CONNECTED`` must
    log the ``Broker is not connected`` error and never touch the
    AWS-IoT client — so no callback can leak past the fix's scope.
    """
    client = _bare_client()
    client._status = ConnectivityStatus.DISCONNECTED
    client._awsiot_client = MagicMock()

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        client._publish("topic", {"anything": "here"})

    client._awsiot_client.publish.assert_not_called()
    assert any(
        r.levelno == logging.ERROR and "Broker is not connected" in r.getMessage()
        for r in caplog.records
    )


# ---------------------------------------------------------------------------
# Signature contract — keyword-only bindings
# ---------------------------------------------------------------------------


def test_on_publish_completed_signature_is_keyword_only():
    """Positional misuse of the callback would have `packet_id`/`topic`/
    `payload` swallowed by ``publish_future``; keyword-only forces the
    binding to be explicit at the ``partial`` call site and would trip
    immediately if the old positional lambda came back.
    """
    import inspect

    sig = inspect.signature(AWSClient._on_publish_completed)
    kinds = {name: p.kind for name, p in sig.parameters.items()}
    assert kinds["packet_id"] == inspect.Parameter.KEYWORD_ONLY
    assert kinds["topic"] == inspect.Parameter.KEYWORD_ONLY
    assert kinds["payload"] == inspect.Parameter.KEYWORD_ONLY
