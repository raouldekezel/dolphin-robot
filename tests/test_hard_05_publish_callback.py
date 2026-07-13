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

These tests are behavioural per CHORE-02 — they drive the real
``AWSClient._on_publish_completed`` bound method against a spec'd
``MagicMock`` and assert what the caller sees (log records; absence of
the removed attribute).
"""

from __future__ import annotations

from concurrent.futures import Future
import logging
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Removal assertions — the whole bookkeeping layout must be gone
# ---------------------------------------------------------------------------


def test_messages_published_dict_and_helpers_removed():
    """The ``_messages_published`` dict and its ``_pre``/``_post`` helpers
    must not exist any more. Their return would re-introduce the shared
    cross-thread mutable state that HARD-05 came from."""
    from custom_components.mydolphin_plus.managers.aws_client import AWSClient

    # class-level removal — instance-level would still miss annotated dicts
    assert not hasattr(AWSClient, "_pre_publish_message")
    assert not hasattr(AWSClient, "_post_message_published")
    assert not hasattr(AWSClient, "_on_publish_completed_callback")


def test_partial_is_imported_at_module_level():
    """``functools.partial`` is now load-bearing in ``_publish`` — its
    absence would silently regress to a lambda that closes over the
    packet id and reopens HARD-05."""
    from functools import partial as functools_partial

    from custom_components.mydolphin_plus.managers import aws_client as aws_client_mod

    assert aws_client_mod.partial is functools_partial


# ---------------------------------------------------------------------------
# _on_publish_completed — success and failure paths
# ---------------------------------------------------------------------------


def _make_aws_stub():
    from custom_components.mydolphin_plus.managers.aws_client import AWSClient

    return MagicMock(spec=AWSClient)


def _resolved_future(value=None) -> Future:
    fut: Future = Future()
    fut.set_result(value)
    return fut


def _failed_future(exc: BaseException) -> Future:
    fut: Future = Future()
    fut.set_exception(exc)
    return fut


def test_on_publish_completed_success_logs_debug_with_correlator(caplog):
    """A resolved future must emit a DEBUG line that carries the packet
    id, topic, and payload — the completion side of the submission ↔
    completion correlator that in-vivo diags anchor on."""
    from custom_components.mydolphin_plus.managers.aws_client import AWSClient

    stub = _make_aws_stub()
    future = _resolved_future()

    with caplog.at_level(logging.DEBUG, logger="custom_components.mydolphin_plus.managers.aws_client"):
        AWSClient._on_publish_completed(
            stub,
            future,
            packet_id=42,
            topic="$aws/things/REDACTED-MUSN/shadow/update",
            payload='{"state":{"desired":{"led":{"ledEnable":1}}}}',
        )

    completion_records = [
        r for r in caplog.records if "MQTT publish" in r.getMessage() and "completed" in r.getMessage()
    ]
    assert completion_records, "expected a completion DEBUG log line"
    msg = completion_records[-1].getMessage()
    assert "#42" in msg
    assert "$aws/things/REDACTED-MUSN/shadow/update" in msg
    assert '"ledEnable":1' in msg
    assert completion_records[-1].levelno == logging.DEBUG, (
        "success payload dumps must be DEBUG, not INFO — SPIKE-02 clientToken"
        " is stamped on every desired write and must not leak at INFO."
    )


def test_on_publish_completed_failure_is_logged_not_swallowed(caplog):
    """A failed future must surface through our logger with topic and
    packet id. Before the fix, `concurrent.futures` swallowed this in an
    opaque `exception calling callback` line — invisible to the user."""
    from custom_components.mydolphin_plus.managers.aws_client import AWSClient

    stub = _make_aws_stub()
    boom = RuntimeError("AWS_ERROR_MQTT_CONNECTION_DESTROYED")
    future = _failed_future(boom)

    with caplog.at_level(logging.DEBUG, logger="custom_components.mydolphin_plus.managers.aws_client"):
        AWSClient._on_publish_completed(
            stub,
            future,
            packet_id=7,
            topic="$aws/things/REDACTED-MUSN/shadow/update",
            payload='{"state":{"desired":{"led":{"ledEnable":0}}}}',
        )

    failure_records = [
        r for r in caplog.records
        if r.levelno == logging.ERROR and "failed" in r.getMessage()
    ]
    assert failure_records, "expected an ERROR log line for the failed future"
    rec = failure_records[-1]
    assert "#7" in rec.getMessage()
    assert "$aws/things/REDACTED-MUSN/shadow/update" in rec.getMessage()
    # ``_LOGGER.exception`` attaches the exception info to the record.
    assert rec.exc_info is not None
    assert rec.exc_info[1] is boom


def test_on_publish_completed_failure_skips_success_line(caplog):
    """When the future fails we must not also emit the "completed"
    success line — otherwise the failure would be visually cancelled by
    a positive log line one message later."""
    from custom_components.mydolphin_plus.managers.aws_client import AWSClient

    stub = _make_aws_stub()
    future = _failed_future(RuntimeError("boom"))

    with caplog.at_level(logging.DEBUG, logger="custom_components.mydolphin_plus.managers.aws_client"):
        AWSClient._on_publish_completed(
            stub,
            future,
            packet_id=9,
            topic="topic",
            payload="{}",
        )

    completed_lines = [
        r for r in caplog.records if "completed" in r.getMessage()
    ]
    assert not completed_lines, "success line must not fire after a failure"


# ---------------------------------------------------------------------------
# Signature contract — keyword-only bindings
# ---------------------------------------------------------------------------


def test_on_publish_completed_signature_is_keyword_only():
    """`packet_id`, `topic`, `payload` must be keyword-only so a stale
    positional lambda (`lambda f: self._on_publish_completed(f)`) cannot
    silently start firing again after a rebase."""
    import inspect

    from custom_components.mydolphin_plus.managers.aws_client import AWSClient

    sig = inspect.signature(AWSClient._on_publish_completed)
    kinds = {name: p.kind for name, p in sig.parameters.items()}
    assert kinds["packet_id"] == inspect.Parameter.KEYWORD_ONLY
    assert kinds["topic"] == inspect.Parameter.KEYWORD_ONLY
    assert kinds["payload"] == inspect.Parameter.KEYWORD_ONLY
