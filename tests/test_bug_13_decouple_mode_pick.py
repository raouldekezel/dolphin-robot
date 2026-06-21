"""Regression tests for BUG-13 (#47) — decouple cleaning-mode pick from start.

Pre-fix, ``coordinator._set_cleaning_mode`` always called
``aws_client.set_cleaning_mode`` on any mode delta, and the firmware
interpreted that as "set mode + start now" when the robot was docked
(holdWeekly/holdDelay/off → on within ~2.5 s). Picking a mode from the
combo box was therefore an implicit start.

Post-fix:

* ``coordinator._set_cleaning_mode`` splits on ``self._system_details.is_active``.
  When the robot is active (CLEANING/RETURNING), today's live mode-swap path is
  kept — same shape as the Maytronics app. When the robot is docked, the call
  routes through ``aws_client.set_cleaning_mode_silent``.
* ``aws_client.set_cleaning_mode_silent`` arms a monotonic deadline
  (``_silent_stop_deadline``) and publishes the mode the same way as
  ``set_cleaning_mode``. The existing BUG-08 chain emits ``cycleTime`` ~1 s
  later; the AWS ``/update/accepted`` echo of that cycleTime write triggers a
  ``pause()`` (E-B primitive, validated in #85). The pause lands before the
  firmware reports ``pwsState=on``, so the firmware adopts mode + cycleTime
  and the robot stays docked.
* ``_silent_stop_due`` is the gate: pending iff (a) deadline armed and not
  expired, (b) echoed ``desired`` carries ``cycleInfo.cycleTime``. The TTL is
  the safety net that prevents a dropped cycleTime echo from later turning
  an unrelated cycleTime write into a spurious mid-cycle stop.

The empirical foundation is #85 (E-B PASS, E-A FAIL) and the SPIKE-02 pre-D2
diag (#72): mode-only write starts the robot (E3b); cycleTime-only write is a
no-op (E2); combined ``{mode, cycleTime}`` is lossy on ``cycleTime`` (E7).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_aws_stub(*, our_token: str | None = "OURTOKEN"):
    """Build an ``AWSClient`` stub for the ``_on_update_accepted`` branches.

    The two predicate methods are bound to the real implementations so the
    test exercises the actual gates, not MagicMock auto-returns.
    """
    from custom_components.mydolphin_plus.managers.aws_client import AWSClient

    stub = MagicMock(spec=AWSClient)
    stub._our_token = our_token
    stub._silent_stop_deadline = None
    stub.data = {}
    stub._on_data_update_callback = lambda: None
    stub._robot_family = None
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
    stub._silent_stop_due = lambda desired: AWSClient._silent_stop_due(stub, desired)
    stub.pause = MagicMock()
    return stub


def _make_coordinator_stub(*, vacuum_mode: str, is_active: bool):
    """Build a coordinator stub exposing only what ``_set_cleaning_mode`` reads."""
    stub = MagicMock()
    stub._system_details = SimpleNamespace(is_active=is_active)
    stub._aws_client = MagicMock()
    stub._get_vacuum_data = MagicMock(
        return_value={"attributes": {"mode": vacuum_mode}}
    )
    return stub


def _encode(payload: dict) -> bytes:
    return json.dumps(payload).encode("utf-8")


def _run_callback_with_fast_sleep(stub, topic, payload):
    """``_message_callback`` with the module-level ``sleep`` patched to a no-op."""
    from custom_components.mydolphin_plus.managers import aws_client as aws_client_mod
    from custom_components.mydolphin_plus.managers.aws_client import AWSClient

    with patch.object(aws_client_mod, "sleep") as sleep_mock:
        AWSClient._message_callback(stub, topic, payload, False, 0, False)
    return sleep_mock


# ---------------------------------------------------------------------------
# Coordinator split — docked vs running
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_cleaning_mode_docked_routes_to_silent():
    """Docked + real mode delta → silent E-B path. The shared
    ``set_cleaning_mode`` (which Start and pickup also use) must NOT be
    called on this path — otherwise the robot starts cleaning."""
    from custom_components.mydolphin_plus.managers.coordinator import (
        MyDolphinPlusCoordinator,
    )

    stub = _make_coordinator_stub(vacuum_mode="all", is_active=False)

    await MyDolphinPlusCoordinator._set_cleaning_mode(stub, None, "stairs")

    stub._aws_client.set_cleaning_mode_silent.assert_called_once_with("stairs")
    stub._aws_client.set_cleaning_mode.assert_not_called()


@pytest.mark.asyncio
async def test_set_cleaning_mode_running_keeps_live_write():
    """Running + real mode delta → live mode-swap, matching today's
    behaviour (and the Maytronics app's). The silent path must NOT be
    used — it would stop the cycle the operator is currently watching."""
    from custom_components.mydolphin_plus.managers.coordinator import (
        MyDolphinPlusCoordinator,
    )

    stub = _make_coordinator_stub(vacuum_mode="all", is_active=True)

    await MyDolphinPlusCoordinator._set_cleaning_mode(stub, None, "stairs")

    stub._aws_client.set_cleaning_mode.assert_called_once_with("stairs")
    stub._aws_client.set_cleaning_mode_silent.assert_not_called()


@pytest.mark.asyncio
async def test_set_cleaning_mode_same_mode_is_no_op_docked():
    """Picking the already-current mode is a no-op on either side — no AWS
    write, no silent-stop arming, no flicker."""
    from custom_components.mydolphin_plus.managers.coordinator import (
        MyDolphinPlusCoordinator,
    )

    stub = _make_coordinator_stub(vacuum_mode="all", is_active=False)

    await MyDolphinPlusCoordinator._set_cleaning_mode(stub, None, "all")

    stub._aws_client.set_cleaning_mode.assert_not_called()
    stub._aws_client.set_cleaning_mode_silent.assert_not_called()


@pytest.mark.asyncio
async def test_set_cleaning_mode_same_mode_is_no_op_running():
    """Same as above for the running branch — locks the early-return."""
    from custom_components.mydolphin_plus.managers.coordinator import (
        MyDolphinPlusCoordinator,
    )

    stub = _make_coordinator_stub(vacuum_mode="all", is_active=True)

    await MyDolphinPlusCoordinator._set_cleaning_mode(stub, None, "all")

    stub._aws_client.set_cleaning_mode.assert_not_called()
    stub._aws_client.set_cleaning_mode_silent.assert_not_called()


# ---------------------------------------------------------------------------
# Silent-set arming
# ---------------------------------------------------------------------------


def test_silent_set_arms_deadline_and_publishes_mode():
    """``set_cleaning_mode_silent`` must (a) arm a deadline in the future
    and (b) delegate to ``set_cleaning_mode`` so the existing publisher and
    BUG-08 chain stay in charge of the wire."""
    from custom_components.mydolphin_plus.managers import aws_client as aws_client_mod
    from custom_components.mydolphin_plus.managers.aws_client import AWSClient

    stub = MagicMock(spec=AWSClient)
    stub._silent_stop_deadline = None
    stub.set_cleaning_mode = MagicMock()

    with patch.object(aws_client_mod, "monotonic", return_value=1000.0):
        AWSClient.set_cleaning_mode_silent(stub, "stairs")

    assert stub._silent_stop_deadline > 1000.0
    assert stub._silent_stop_deadline - 1000.0 == pytest.approx(
        aws_client_mod._SILENT_STOP_TTL_SECONDS
    )
    stub.set_cleaning_mode.assert_called_once_with("stairs")


# ---------------------------------------------------------------------------
# _silent_stop_due — the gate
# ---------------------------------------------------------------------------


def test_silent_stop_due_false_when_not_armed():
    from custom_components.mydolphin_plus.managers.aws_client import AWSClient

    stub = MagicMock(spec=AWSClient)
    stub._silent_stop_deadline = None

    assert AWSClient._silent_stop_due(stub, {"cycleInfo": {"cycleTime": 120}}) is False


def test_silent_stop_due_true_when_armed_and_cycle_time_present():
    from custom_components.mydolphin_plus.managers import aws_client as aws_client_mod
    from custom_components.mydolphin_plus.managers.aws_client import AWSClient

    stub = MagicMock(spec=AWSClient)
    stub._silent_stop_deadline = 1000.0

    with patch.object(aws_client_mod, "monotonic", return_value=999.0):
        result = AWSClient._silent_stop_due(stub, {"cycleInfo": {"cycleTime": 120}})

    assert result is True
    # Deadline left intact — the caller (the callback) consumes it.
    assert stub._silent_stop_deadline == 1000.0


def test_silent_stop_due_false_and_cleared_when_expired():
    """A stale armed deadline must self-clear so a future write doesn't
    inherit it. Without this, finding 2 of the design review's blocker
    scenario lets a dangling pending stop a running robot mid-cycle."""
    from custom_components.mydolphin_plus.managers import aws_client as aws_client_mod
    from custom_components.mydolphin_plus.managers.aws_client import AWSClient

    stub = MagicMock(spec=AWSClient)
    stub._silent_stop_deadline = 1000.0

    with patch.object(aws_client_mod, "monotonic", return_value=1500.0):
        result = AWSClient._silent_stop_due(stub, {"cycleInfo": {"cycleTime": 120}})

    assert result is False
    assert stub._silent_stop_deadline is None


def test_silent_stop_due_false_without_cycle_time_field():
    """The pause must NOT fire on a sibling-section write
    (e.g. ``systemState``, ``led``) that happens to land within the TTL
    window. The shape discriminator is non-negotiable."""
    from custom_components.mydolphin_plus.managers import aws_client as aws_client_mod
    from custom_components.mydolphin_plus.managers.aws_client import AWSClient

    stub = MagicMock(spec=AWSClient)
    stub._silent_stop_deadline = 1000.0

    with patch.object(aws_client_mod, "monotonic", return_value=999.0):
        assert AWSClient._silent_stop_due(stub, {}) is False
        assert (
            AWSClient._silent_stop_due(stub, {"systemState": {"pwsState": "off"}})
            is False
        )
        assert AWSClient._silent_stop_due(stub, {"cycleInfo": {}}) is False


# ---------------------------------------------------------------------------
# Observer integration — full _message_callback path
# ---------------------------------------------------------------------------


def test_observer_pauses_on_our_cycle_time_echo_with_pending():
    """End-to-end happy path: armed pending + our-token cycleTime echo →
    ``pause()`` fires and the deadline is cleared in one step."""
    from custom_components.mydolphin_plus.managers import aws_client as aws_client_mod

    stub = _make_aws_stub(our_token="OURTOKEN")
    stub._silent_stop_deadline = 1_000_000.0  # far future
    payload = _encode(
        {
            "state": {"desired": {"cycleInfo": {"cycleTime": 60}}},
            "clientToken": "OURTOKEN",
            "version": 100,
            "timestamp": 1000,
        }
    )

    with patch.object(aws_client_mod, "monotonic", return_value=999.0):
        _run_callback_with_fast_sleep(stub, stub._topic_data.update_accepted, payload)

    stub.pause.assert_called_once()
    assert stub._silent_stop_deadline is None
    stub._set_cycle_time.assert_not_called()


def test_observer_no_pause_when_no_pending():
    """Our own cycleTime write echoing back WITHOUT a pending silent set
    (e.g. operator changed a number entity, or any future cycleTime path)
    must not interfere. This is the lock on finding 2 of the review."""
    stub = _make_aws_stub(our_token="OURTOKEN")
    stub._silent_stop_deadline = None
    payload = _encode(
        {
            "state": {"desired": {"cycleInfo": {"cycleTime": 60}}},
            "clientToken": "OURTOKEN",
            "version": 100,
            "timestamp": 1000,
        }
    )

    _run_callback_with_fast_sleep(stub, stub._topic_data.update_accepted, payload)

    stub.pause.assert_not_called()


def test_observer_no_pause_on_foreign_cycle_time_echo_even_when_pending():
    """The provenance gate stays in force on the new branch — an app-issued
    cycleTime write must not consume our pending. Otherwise an app cycleTime
    write landing within our TTL window would stop the robot."""
    from custom_components.mydolphin_plus.managers import aws_client as aws_client_mod

    stub = _make_aws_stub(our_token="OURTOKEN")
    stub._silent_stop_deadline = 1_000_000.0
    payload = _encode(
        {
            "state": {"desired": {"cycleInfo": {"cycleTime": 60}}},
            # no clientToken → app-authored
            "version": 100,
            "timestamp": 1000,
        }
    )

    with patch.object(aws_client_mod, "monotonic", return_value=999.0):
        _run_callback_with_fast_sleep(stub, stub._topic_data.update_accepted, payload)

    stub.pause.assert_not_called()
    # Pending is left intact — our own cycleTime echo may still arrive.
    assert stub._silent_stop_deadline == 1_000_000.0


def test_observer_no_pause_on_our_mode_echo_with_pending():
    """The mode echo of OUR silent set (the first BUG-08 step) must take
    the existing branch (sleep + set_cycle_time) and NOT also trigger the
    pause — otherwise we stop before the cycleTime is even written.

    This locks the ``return`` after ``_set_cycle_time`` in the callback,
    which is otherwise only structurally guaranteed.
    """
    from custom_components.mydolphin_plus.managers import aws_client as aws_client_mod

    stub = _make_aws_stub(our_token="OURTOKEN")
    stub._silent_stop_deadline = 1_000_000.0
    payload = _encode(
        {
            "state": {"desired": {"cleaningMode": {"mode": "stairs"}}},
            "clientToken": "OURTOKEN",
            "version": 100,
            "timestamp": 1000,
        }
    )

    with patch.object(aws_client_mod, "monotonic", return_value=999.0):
        sleep_mock = _run_callback_with_fast_sleep(
            stub, stub._topic_data.update_accepted, payload
        )

    sleep_mock.assert_called_once_with(1)
    stub._set_cycle_time.assert_called_once_with("stairs")
    stub.pause.assert_not_called()
    # Pending stays armed — the cycleTime echo is the next step.
    assert stub._silent_stop_deadline == 1_000_000.0


def test_observer_no_pause_on_pause_own_echo():
    """Our own ``pause()`` write echoes back as ``desired.systemState.pwsState``
    (no ``cycleInfo``). The shape discriminator must reject it so we don't
    issue a second pause."""
    from custom_components.mydolphin_plus.managers import aws_client as aws_client_mod

    stub = _make_aws_stub(our_token="OURTOKEN")
    stub._silent_stop_deadline = 1_000_000.0  # could plausibly still be armed
    payload = _encode(
        {
            "state": {"desired": {"systemState": {"pwsState": "off"}}},
            "clientToken": "OURTOKEN",
            "version": 100,
            "timestamp": 1000,
        }
    )

    with patch.object(aws_client_mod, "monotonic", return_value=999.0):
        _run_callback_with_fast_sleep(stub, stub._topic_data.update_accepted, payload)

    stub.pause.assert_not_called()


def test_observer_clears_expired_pending_without_action():
    """An accepted carrying our cycleTime arrives AFTER the TTL elapses
    (e.g. broker delay): no pause, and the stale pending is cleared so a
    later, unrelated cycleTime write cannot trigger a spurious stop."""
    from custom_components.mydolphin_plus.managers import aws_client as aws_client_mod

    stub = _make_aws_stub(our_token="OURTOKEN")
    stub._silent_stop_deadline = 1000.0
    payload = _encode(
        {
            "state": {"desired": {"cycleInfo": {"cycleTime": 60}}},
            "clientToken": "OURTOKEN",
            "version": 100,
            "timestamp": 1000,
        }
    )

    with patch.object(aws_client_mod, "monotonic", return_value=2000.0):
        _run_callback_with_fast_sleep(stub, stub._topic_data.update_accepted, payload)

    stub.pause.assert_not_called()
    assert stub._silent_stop_deadline is None
