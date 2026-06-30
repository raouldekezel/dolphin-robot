"""Regression tests for BUG-13 (#47) — write-on-commit cleaning-mode pick.

Pre-fix, ``coordinator._set_cleaning_mode`` always called
``aws_client.set_cleaning_mode`` on any mode delta. The firmware
interprets that write as "set mode + start now" when the robot is docked
(``holdWeekly``/``holdDelay``/``off`` → ``on`` within ~2.5 s), so picking a
mode from the combo box was an implicit start.

The first attempt (E-B silent stop, PR #86) tried to suppress the implicit
start with a reactive ``pause()`` triggered by the BUG-08 cycleTime echo.
That race lost in two ways:

* BUG-19 (#96): a second consecutive docked pick ~17 s later still started
  a cycle anyway — the firmware window was non-deterministic.
* BUG-20 (#98): a scheduled cycle after a BUG-19 sequence left the firmware
  stuck in init for hours, with the cycle counter jumping to a sentinel.
  The start→pause mini-cycle was itself the trigger.

The pivot — **write-on-commit** (issue #47, 2026-06-27 comments):

* While **docked**, picking a mode stores the value in
  ``coordinator._desired_clean_mode`` and writes **nothing** to AWS. No
  shadow write → no implicit start → BUG-19 cannot occur, and the
  start→pause trigger of BUG-20 is removed by construction.
* While **running**, picking a mode also stages only (HARD-12, #104).
  The earlier pivot kept a live-write path for app parity, but the 2026-06-28
  in-vivo session (PR #102) showed every mid-cycle mode write transiently
  re-enters ``init`` for ~30 s and silently rewrites the in-flight cycle's
  ``cycleTime`` without restamping ``cycleStartTime`` — confusing for the
  operator. To apply a new mode mid-cycle, the operator stops then starts.
* **Run** (``_vacuum_start``) commits the staged value via the existing
  ``set_cleaning_mode`` primitive; the firmware's implicit start is now
  exactly what's wanted, and the BUG-08 chain supplies the cycle time.
* **Reconcile on foreign change / startup**: when the firmware-reported
  mode changes outside an HA-initiated write (Maytronics app, scheduler,
  or first refresh after init), the staged value is overwritten with
  reported. The contract is "desired := current".
* The BUG-08 ``cycleTime`` chain in ``aws_client._on_update_accepted``
  stays — it is the mandatory two-step delivery for a per-mode duration
  (SPIKE-02 E7: combined ``{mode, cycleTime}`` writes are firmware-lossy
  on the sibling cycleTime).
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

    ``_event_is_ours`` is bound to the real implementation so the
    provenance gate is exercised, not auto-stubbed.
    """
    from custom_components.mydolphin_plus.managers.aws_client import AWSClient

    stub = MagicMock(spec=AWSClient)
    stub._our_token = our_token
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
    stub.pause = MagicMock()
    return stub


def _make_coordinator_stub(
    *,
    desired: str | None,
    last_seen: str | None = None,
    is_active: bool = False,
    reported: str | None = None,
):
    """Stub the bits of ``MyDolphinPlusCoordinator`` that the BUG-13 paths read.

    Includes the optional firmware-reported mode in ``aws_data`` so the
    ``_vacuum_start`` fallback (and ``_reconcile_desired_clean_mode``) can
    be exercised end-to-end.
    """
    from custom_components.mydolphin_plus.common.consts import (
        DATA_CYCLE_INFO_CLEANING_MODE,
        DATA_SECTION_CYCLE_INFO,
        DATA_SECTION_SYSTEM_STATE,
    )

    stub = MagicMock()
    stub._desired_clean_mode = desired
    stub._last_seen_reported_clean_mode = last_seen
    stub._system_details = SimpleNamespace(is_active=is_active)
    stub._has_real_data = False
    stub._aws_client = MagicMock()
    stub._aws_client.data = {}
    if reported is not None:
        stub._aws_client.data[DATA_SECTION_CYCLE_INFO] = {
            DATA_CYCLE_INFO_CLEANING_MODE: {"mode": reported},
        }
        stub._aws_client.data[DATA_SECTION_SYSTEM_STATE] = {"pwsState": "holdWeekly"}
    # ``aws_data`` is a property on the real class — emulate it.
    stub.aws_data = stub._aws_client.data
    stub.async_update_listeners = MagicMock()
    # HARD-11 — neutralize the new pre-write helpers so this stub still
    # exercises the BUG-13 commit semantics. The HARD-11 paths are tested
    # separately in ``test_hard_11_optimistic_overlay.py``.
    stub._is_start_guard_active = MagicMock(return_value=False)
    stub._arm_optimistic_start = MagicMock()
    stub._optimistic_vacuum_state = None
    stub._optimistic_statut = None
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
# Step 1 rollback — the silent-stop apparatus is gone
# ---------------------------------------------------------------------------


def test_silent_stop_apparatus_removed_from_aws_client():
    """The reactive E-B / silent-stop primitives must not exist on the
    AWS client any more. Their presence would mean a follow-up could
    accidentally re-arm the start→pause race that triggered BUG-19/20."""
    from custom_components.mydolphin_plus.managers import aws_client as aws_client_mod
    from custom_components.mydolphin_plus.managers.aws_client import AWSClient

    assert not hasattr(AWSClient, "set_cleaning_mode_silent")
    assert not hasattr(AWSClient, "_silent_stop_due")
    assert not hasattr(aws_client_mod, "_SILENT_STOP_TTL_SECONDS")


def test_bug_08_chain_survives_on_our_mode_echo():
    """The BUG-08 chain must still fire on our own mode echo — without it,
    a started cycle would run at the firmware's persisted cycleTime instead
    of the integration's configured per-mode value (SPIKE-02 E7 makes the
    two-step chain mandatory)."""
    from custom_components.mydolphin_plus.managers import aws_client as aws_client_mod

    stub = _make_aws_stub(our_token="OURTOKEN")
    payload = _encode(
        {
            "state": {"desired": {"cleaningMode": {"mode": "stairs"}}},
            "clientToken": "OURTOKEN",
            "version": 100,
            "timestamp": 1000,
        }
    )

    sleep_mock = _run_callback_with_fast_sleep(
        stub, stub._topic_data.update_accepted, payload
    )

    sleep_mock.assert_called_once_with(1)
    stub._set_cycle_time.assert_called_once_with("stairs")
    # No reactive stop primitive any more — pause() must never be a side
    # effect of receiving a mode echo.
    stub.pause.assert_not_called()
    _ = aws_client_mod  # silence unused-import warning under strict linters


def test_bug_08_chain_does_not_fire_on_foreign_mode_echo():
    """Provenance gate is unchanged: app-issued mode writes (no token) must
    not trigger our cycleTime push. The app handles its own durations."""
    stub = _make_aws_stub(our_token="OURTOKEN")
    payload = _encode(
        {
            "state": {"desired": {"cleaningMode": {"mode": "stairs"}}},
            "version": 100,
            "timestamp": 1000,
        }
    )

    sleep_mock = _run_callback_with_fast_sleep(
        stub, stub._topic_data.update_accepted, payload
    )

    sleep_mock.assert_not_called()
    stub._set_cycle_time.assert_not_called()
    stub.pause.assert_not_called()


def test_no_pause_fires_on_our_cycle_time_echo():
    """Pre-pivot, an armed silent set turned our own cycleTime echo into a
    reactive ``pause()``. With the apparatus removed, the cycleTime echo
    must be inert in the observer — only the BUG-08 path matters, and the
    cycleTime echo never carries ``cleaningMode.mode``."""
    stub = _make_aws_stub(our_token="OURTOKEN")
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
    stub._set_cycle_time.assert_not_called()


# ---------------------------------------------------------------------------
# Step 2 — write-on-commit in the coordinator
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pick_while_docked_writes_nothing_only_stages():
    """The whole BUG-13/19/20 fix: a docked pick must not touch AWS. It
    stores the value in ``_desired_clean_mode`` and notifies listeners."""
    from custom_components.mydolphin_plus.managers.coordinator import (
        MyDolphinPlusCoordinator,
    )

    stub = _make_coordinator_stub(desired="all", is_active=False)

    await MyDolphinPlusCoordinator._set_cleaning_mode(stub, None, "stairs")

    assert stub._desired_clean_mode == "stairs"
    stub._aws_client.set_cleaning_mode.assert_not_called()
    # No silent primitive: even the legacy name must be untouched on stubs
    # that auto-create attributes.
    assert "set_cleaning_mode_silent" not in {
        c[0] for c in stub._aws_client.mock_calls
    }
    stub.async_update_listeners.assert_called_once()


@pytest.mark.asyncio
async def test_pick_while_running_stages_only_no_aws_write():
    """HARD-12 (#104) — picking a mode while the cycle is running must not
    touch AWS. Same write-nothing branch as the docked path: stage
    ``_desired`` and notify listeners; the firmware only hears the mode at
    the next Run."""
    from custom_components.mydolphin_plus.managers.coordinator import (
        MyDolphinPlusCoordinator,
    )

    stub = _make_coordinator_stub(desired="all", is_active=True)

    await MyDolphinPlusCoordinator._set_cleaning_mode(stub, None, "stairs")

    assert stub._desired_clean_mode == "stairs"
    stub._aws_client.set_cleaning_mode.assert_not_called()
    stub.async_update_listeners.assert_called_once()


@pytest.mark.asyncio
async def test_pick_same_mode_is_a_noop():
    """No write, no listener bump — both branches return early."""
    from custom_components.mydolphin_plus.managers.coordinator import (
        MyDolphinPlusCoordinator,
    )

    for is_active in (False, True):
        stub = _make_coordinator_stub(desired="all", is_active=is_active)
        await MyDolphinPlusCoordinator._set_cleaning_mode(stub, None, "all")
        stub._aws_client.set_cleaning_mode.assert_not_called()
        stub.async_update_listeners.assert_not_called()


@pytest.mark.asyncio
async def test_run_commits_staged_mode():
    """``_vacuum_start`` is the one place that writes a mode to the
    firmware on the docked path. It must read the staged value, not the
    firmware-reported mode — otherwise the user's pick is lost."""
    from custom_components.mydolphin_plus.managers.coordinator import (
        MyDolphinPlusCoordinator,
    )

    stub = _make_coordinator_stub(desired="stairs", reported="all")

    await MyDolphinPlusCoordinator._vacuum_start(stub, None, None)

    stub._aws_client.set_cleaning_mode.assert_called_once_with("stairs")


@pytest.mark.asyncio
async def test_run_falls_back_to_reported_when_nothing_staged():
    """Defensive fallback for an unusual sequence (Run before the first
    refresh has seeded ``_desired``). Should never happen in steady state."""
    from custom_components.mydolphin_plus.common.clean_modes import CleanModes
    from custom_components.mydolphin_plus.managers.coordinator import (
        MyDolphinPlusCoordinator,
    )

    stub = _make_coordinator_stub(desired=None, reported="floor")

    await MyDolphinPlusCoordinator._vacuum_start(stub, None, None)

    stub._aws_client.set_cleaning_mode.assert_called_once_with("floor")

    # And, true last-resort fallback when even reported is absent.
    stub2 = _make_coordinator_stub(desired=None, reported=None)
    await MyDolphinPlusCoordinator._vacuum_start(stub2, None, None)
    stub2._aws_client.set_cleaning_mode.assert_called_once_with(CleanModes.REGULAR)


# ---------------------------------------------------------------------------
# Step 2 — getters return the staged value while docked
# ---------------------------------------------------------------------------


def test_get_desired_clean_mode_data_returns_staged_value_while_docked():
    """The writable picker must show the operator's pick immediately, even
    though no firmware echo has arrived (and won't, on the docked path).
    HARD-13 moved this getter from ``_get_clean_mode_data`` to
    ``_get_desired_clean_mode_data`` so the sensor stays on the
    firmware-reported value; the staged behaviour now lives on the new
    select."""
    from custom_components.mydolphin_plus.managers.coordinator import (
        MyDolphinPlusCoordinator,
    )

    stub = _make_coordinator_stub(desired="stairs", reported="all")

    result = MyDolphinPlusCoordinator._get_desired_clean_mode_data(stub, None)

    assert result["state"] == "stairs"


def test_get_vacuum_data_returns_staged_value_while_docked():
    """Same source of truth for the vacuum entity's ``fan_speed``."""
    from custom_components.mydolphin_plus.common.consts import ATTR_ATTRIBUTES
    from custom_components.mydolphin_plus.managers.coordinator import (
        MyDolphinPlusCoordinator,
    )
    from homeassistant.const import ATTR_MODE

    stub = _make_coordinator_stub(desired="stairs", reported="all")
    # The getter also reads vacuum_state from system_details.
    from homeassistant.components.vacuum import VacuumActivity

    stub._system_details = SimpleNamespace(
        is_active=False, vacuum_state=VacuumActivity.DOCKED
    )

    result = MyDolphinPlusCoordinator._get_vacuum_data(stub, None)

    assert result[ATTR_ATTRIBUTES][ATTR_MODE] == "stairs"


# ---------------------------------------------------------------------------
# Step 2 — reconciliation
# ---------------------------------------------------------------------------


def test_reconcile_seeds_desired_from_reported_at_first_refresh():
    """Startup contract: a reboot shows the robot's real mode, not a stale
    pick. The first refresh after coordinator init seeds both
    ``_desired_clean_mode`` and ``_last_seen_reported_clean_mode``."""
    from custom_components.mydolphin_plus.managers.coordinator import (
        MyDolphinPlusCoordinator,
    )

    stub = _make_coordinator_stub(desired=None, last_seen=None, reported="all")

    MyDolphinPlusCoordinator._reconcile_desired_clean_mode(stub)

    assert stub._desired_clean_mode == "all"
    assert stub._last_seen_reported_clean_mode == "all"


def test_reconcile_preserves_pick_landed_before_first_refresh():
    """Tight race: the operator picks ``stairs`` before the first refresh
    arrives. Seeding must not clobber the pre-existing pick (whose own
    write paths would also have set it deliberately)."""
    from custom_components.mydolphin_plus.managers.coordinator import (
        MyDolphinPlusCoordinator,
    )

    stub = _make_coordinator_stub(desired="stairs", last_seen=None, reported="all")

    MyDolphinPlusCoordinator._reconcile_desired_clean_mode(stub)

    assert stub._desired_clean_mode == "stairs"
    # The baseline is still established so a SUBSEQUENT foreign change is
    # detected as a delta against "all".
    assert stub._last_seen_reported_clean_mode == "all"


def test_reconcile_overwrites_staged_pick_on_foreign_change():
    """Maytronics-app / scheduler-initiated mode change: the user's staged
    pick yields to the firmware's current mode (contract: "desired :=
    current")."""
    from custom_components.mydolphin_plus.managers.coordinator import (
        MyDolphinPlusCoordinator,
    )

    stub = _make_coordinator_stub(desired="stairs", last_seen="all", reported="wall")

    MyDolphinPlusCoordinator._reconcile_desired_clean_mode(stub)

    assert stub._desired_clean_mode == "wall"
    assert stub._last_seen_reported_clean_mode == "wall"


def test_reconcile_is_noop_when_reported_unchanged():
    """Steady state — most refreshes. Reported has not moved since last
    seen; the staged pick (if any) must survive intact."""
    from custom_components.mydolphin_plus.managers.coordinator import (
        MyDolphinPlusCoordinator,
    )

    stub = _make_coordinator_stub(desired="stairs", last_seen="all", reported="all")

    MyDolphinPlusCoordinator._reconcile_desired_clean_mode(stub)

    assert stub._desired_clean_mode == "stairs"
    assert stub._last_seen_reported_clean_mode == "all"


def test_reconcile_skips_when_reported_absent():
    """Pre-first-shadow window — the cleaning-mode slot is not in
    ``aws_data`` yet. The reconciler must not crash and must not seed a
    spurious value."""
    from custom_components.mydolphin_plus.managers.coordinator import (
        MyDolphinPlusCoordinator,
    )

    stub = _make_coordinator_stub(desired=None, last_seen=None, reported=None)

    MyDolphinPlusCoordinator._reconcile_desired_clean_mode(stub)

    assert stub._desired_clean_mode is None
    assert stub._last_seen_reported_clean_mode is None


def test_reconcile_idempotent_after_our_own_run_echo():
    """End-to-end shape of a Run committed by HA. We set ``_desired`` to
    ``stairs`` and call ``set_cleaning_mode("stairs")``; the firmware
    later echoes ``reported.cleaningMode.mode = stairs``. The reconciler
    sees a real delta against ``_last_seen``, refreshes both fields, and
    leaves ``_desired`` exactly where it already was — by construction
    of the value-based gate, our own writes never spuriously reset the
    staged value."""
    from custom_components.mydolphin_plus.managers.coordinator import (
        MyDolphinPlusCoordinator,
    )

    stub = _make_coordinator_stub(desired="stairs", last_seen="all", reported="stairs")

    MyDolphinPlusCoordinator._reconcile_desired_clean_mode(stub)

    assert stub._desired_clean_mode == "stairs"
    assert stub._last_seen_reported_clean_mode == "stairs"
