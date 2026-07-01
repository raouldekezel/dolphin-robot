"""Regression tests for HARD-11 v1 (honest-core) — optimistic vacuum overlay
masking the firmware echo gap, plus the start-serialization guard.

The integration writes Run / Stop / pickup commands to the AWS shadow
fire-and-forget; the firmware only echoes back the resulting state
(``pwsState=on``, ``calculated_state=cleaning`` / ``holdWeekly``) tens of
seconds later. Without HARD-11 the picker UI shows nothing during that
window and the operator is tempted to re-click — which on rapid
Run → Stop → Run patterns reproduces the BUG-19 silent restart and the
BUG-20 stuck-init cascade (validated empirically in
``docs/diag/2026-06-26_bug-19_e5a-reactive-stop-hotpatch-validation/``).

The v1 design ("honest-core", per the in-thread review on #103):

* **Optimistic ``vacuum.activity`` on Run / pickup** — flips to ``CLEANING``
  (or ``RETURNING`` when the staged mode is ``pickup``) immediately on the
  service call so the standard HA more-info card swaps the controls. The
  AWS write happens at the same call; the overlay just masks the display
  gap.
* **Honest-linger on Stop** — the AWS ``pause()`` is written immediately,
  but ``vacuum.activity`` is *not* flipped optimistically: it follows the
  real shadow state and only transitions to ``docked`` on the
  ``pwsState=off`` echo. The click acknowledgement lives on the
  ``startingPending`` / ``pausingPending`` ``CalculatedState`` sub-states
  surfaced via ``sensor.<robot>_statut``.
* **TTL fallback** — a monotonic deadline (~120 s) clears the overlay
  silently if no echo ever arrives, so a robot that does not actually
  start (offline, AWS desync) does not leave a stale ``cleaning`` lie on
  the UI forever.
* **Origin-moved clear** — the overlay also clears as soon as the
  firmware's ``vacuum_state`` moves away from the click-time origin: the
  click reached the firmware, whatever the exact target.
* **ERROR clear** — a firmware ERROR shadow takes priority over the
  optimistic value immediately.
* **Start-serialization guard** — a fresh ``set_cleaning_mode`` is
  refused while the previous ``pause()`` is unacknowledged
  (``holdWeekly`` not yet observed and < 15 s elapsed). This is the
  load-bearing protection against the BUG-19 race.
* **Reconcile runs unconditionally** — ``_set_system_status_details``
  is called every coordinator tick (not gated on ``is_ready``), so TTL
  and guard cap fire even while the connection is down or the firmware
  has not yet emitted any ``systemState`` shadow.

These tests target observable behaviour through the public getter shape
(``_get_vacuum_data``, ``_get_status_data``) and the public action
methods (``_vacuum_start``, ``_vacuum_pause``, ``_pickup``), using a
``MagicMock``-backed coordinator stub. No source-text grep (CHORE-02).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from custom_components.mydolphin_plus.common.calculated_state import CalculatedState
from custom_components.mydolphin_plus.common.clean_modes import CleanModes
from custom_components.mydolphin_plus.managers import coordinator as coord_mod
from custom_components.mydolphin_plus.managers.coordinator import (
    _OPTIMISTIC_TTL_S,
    _PAUSE_GUARD_TTL_S,
    MyDolphinPlusCoordinator,
)
from homeassistant.components.vacuum import VacuumActivity

# ---------------------------------------------------------------------------
# Stub builder
# ---------------------------------------------------------------------------


def _make_coordinator_stub(
    *,
    real_vacuum_state: VacuumActivity = VacuumActivity.DOCKED,
    real_calculated_state: CalculatedState = CalculatedState.HOLD_WEEKLY,
    last_observed_calculated_state: CalculatedState | None = None,
    is_active: bool = False,
    desired_mode: str | None = "all",
    has_real_data: bool = True,
):
    """Build a MagicMock-backed coordinator stub for HARD-11 paths.

    Exposes exactly the surface the methods under test read:
    ``_system_details`` (vacuum_state / calculated_state / is_active),
    ``_aws_client`` (pause / pickup / set_cleaning_mode), the BUG-13
    staged mode, the HARD-11 overlay slots, the start-guard slot, and
    the v1.1 edge tracker.
    """
    # A real class instance (not a MagicMock) so `self._is_start_guard_active()`,
    # `self._arm_optimistic_start()`, `self._clear_optimistic_overlay()` etc.
    # dispatch to the real helpers under test instead of returning child mocks.
    stub = MyDolphinPlusCoordinator.__new__(MyDolphinPlusCoordinator)
    stub._desired_clean_mode = desired_mode
    stub._last_seen_reported_clean_mode = desired_mode
    stub._system_details = SimpleNamespace(
        vacuum_state=real_vacuum_state,
        calculated_state=real_calculated_state,
        is_active=is_active,
        data={},
    )
    stub._has_real_data = has_real_data
    stub._aws_client = MagicMock()
    stub._aws_client.data = {}
    # `aws_data` is a @property backed by `_aws_client.data` — no setter, so it
    # cannot be assigned; the @property reads through to the mock's `.data`.
    stub.async_update_listeners = MagicMock()

    # HARD-11 overlay slots — initially unarmed.
    stub._optimistic_vacuum_state = None
    stub._optimistic_statut = None
    stub._optimistic_origin_vacuum_state = None
    stub._optimistic_deadline = None
    stub._pause_issued_at = None
    # HARD-11 v1.1 — edge tracker for the pause-acknowledgement signal.
    # Defaults to None ("no prior observation"); tests that exercise the
    # edge predicate seed it explicitly.
    stub._last_observed_calculated_state = last_observed_calculated_state

    return stub


# ---------------------------------------------------------------------------
# Section 1 — Run arms the overlay; Stop leaves vacuum alone (honest-linger)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_arms_optimistic_cleaning_and_writes():
    """Click Run → optimistic vacuum = CLEANING, statut = startingPending,
    AWS write fires, listeners pushed. The card swap happens in the same
    refresh cycle as the AWS write."""
    stub = _make_coordinator_stub()

    with patch.object(coord_mod.time, "monotonic", return_value=1000.0):
        await MyDolphinPlusCoordinator._vacuum_start(stub, None, None)

    assert stub._optimistic_vacuum_state == VacuumActivity.CLEANING
    assert stub._optimistic_statut == CalculatedState.STARTING_PENDING
    assert stub._optimistic_origin_vacuum_state == VacuumActivity.DOCKED
    assert stub._optimistic_deadline == pytest.approx(1000.0 + _OPTIMISTIC_TTL_S)
    stub._aws_client.set_cleaning_mode.assert_called_once_with("all")
    stub.async_update_listeners.assert_called_once()


@pytest.mark.asyncio
async def test_pickup_arms_optimistic_returning():
    """Pickup → optimistic vacuum = RETURNING (not CLEANING). The HA
    standard card distinguishes Return-to-base from Cleaning visually."""
    stub = _make_coordinator_stub()

    with patch.object(coord_mod.time, "monotonic", return_value=1000.0):
        await MyDolphinPlusCoordinator._pickup(stub, None)

    assert stub._optimistic_vacuum_state == VacuumActivity.RETURNING
    assert stub._optimistic_statut == CalculatedState.STARTING_PENDING
    stub._aws_client.pickup.assert_called_once()


@pytest.mark.asyncio
async def test_run_with_pickup_staged_mode_arms_returning():
    """If the staged mode happens to be ``pickup`` and the operator clicks
    Run, the overlay target is RETURNING — not CLEANING — so the card does
    not lie about the trip direction."""
    stub = _make_coordinator_stub(desired_mode=CleanModes.PICKUP)

    await MyDolphinPlusCoordinator._vacuum_start(stub, None, None)

    assert stub._optimistic_vacuum_state == VacuumActivity.RETURNING


@pytest.mark.asyncio
async def test_pause_does_not_arm_vacuum_overlay_only_statut():
    """Honest-linger: Stop must not flip ``vacuum.activity`` optimistically
    (the firmware may yet ignore the pause and the UI must not claim
    ``docked`` until the echo arrives). Only the ``pausingPending`` statut
    is set, and the AWS pause is written."""
    stub = _make_coordinator_stub(
        real_vacuum_state=VacuumActivity.CLEANING,
        real_calculated_state=CalculatedState.CLEANING,
    )

    with patch.object(coord_mod.time, "monotonic", return_value=1000.0):
        await MyDolphinPlusCoordinator._vacuum_pause(
            stub, None, VacuumActivity.CLEANING
        )

    assert stub._optimistic_vacuum_state is None
    assert stub._optimistic_statut == CalculatedState.PAUSING_PENDING
    assert stub._optimistic_origin_vacuum_state == VacuumActivity.CLEANING
    assert stub._pause_issued_at == 1000.0
    stub._aws_client.pause.assert_called_once()


@pytest.mark.asyncio
async def test_pause_on_docked_is_a_noop():
    """Stop clicked while the robot is already docked must do nothing —
    no overlay, no AWS write, no guard armed. Pre-HARD-11 behaviour."""
    stub = _make_coordinator_stub()

    await MyDolphinPlusCoordinator._vacuum_pause(stub, None, VacuumActivity.DOCKED)

    assert stub._optimistic_statut is None
    assert stub._pause_issued_at is None
    stub._aws_client.pause.assert_not_called()


# ---------------------------------------------------------------------------
# Section 2 — Readers surface the overlay; getters stay pure
# ---------------------------------------------------------------------------


def test_get_vacuum_data_returns_optimistic_when_armed():
    """While the Run overlay is armed, ``_get_vacuum_data`` returns the
    optimistic CLEANING even though the real firmware state is still
    DOCKED. This is what makes the more-info card swap Start → Pause."""
    from homeassistant.const import ATTR_STATE

    stub = _make_coordinator_stub()
    stub._optimistic_vacuum_state = VacuumActivity.CLEANING

    result = MyDolphinPlusCoordinator._get_vacuum_data(stub, None)

    assert result[ATTR_STATE] == VacuumActivity.CLEANING


def test_get_vacuum_data_falls_back_to_real_when_unarmed():
    from homeassistant.const import ATTR_STATE

    stub = _make_coordinator_stub(real_vacuum_state=VacuumActivity.CLEANING)

    result = MyDolphinPlusCoordinator._get_vacuum_data(stub, None)

    assert result[ATTR_STATE] == VacuumActivity.CLEANING


def test_get_status_data_returns_optimistic_statut_when_armed():
    """Chip side: ``startingPending`` / ``pausingPending`` surface as the
    statut sub-state, lowercase (the StrEnum value is already
    lowercase but ``_get_status_data`` applies ``.lower()`` defensively)."""
    from homeassistant.const import ATTR_STATE

    stub = _make_coordinator_stub()
    stub._optimistic_statut = CalculatedState.STARTING_PENDING

    result = MyDolphinPlusCoordinator._get_status_data(stub, None)

    assert result[ATTR_STATE] == "startingpending"


def test_get_status_data_falls_back_to_real_when_unarmed():
    from homeassistant.const import ATTR_STATE

    stub = _make_coordinator_stub(real_calculated_state=CalculatedState.CLEANING)

    result = MyDolphinPlusCoordinator._get_status_data(stub, None)

    assert result[ATTR_STATE] == "cleaning"


def test_get_vacuum_data_does_not_clear_overlay():
    """Getters are pure reads. The reconcile in
    ``_set_system_status_details`` is the only place that clears."""
    stub = _make_coordinator_stub()
    stub._optimistic_vacuum_state = VacuumActivity.CLEANING
    stub._optimistic_origin_vacuum_state = VacuumActivity.DOCKED
    stub._optimistic_deadline = 999_999.0

    MyDolphinPlusCoordinator._get_vacuum_data(stub, None)

    assert stub._optimistic_vacuum_state == VacuumActivity.CLEANING


# ---------------------------------------------------------------------------
# Section 3 — Overlay reconcile clears on TTL / ERROR / origin-moved
# ---------------------------------------------------------------------------


def test_reconcile_clears_on_ttl_expiry():
    """No echo for the full TTL window → silent clear, regardless of
    real-data availability. This is the D2 (silent no-start) fallback."""
    stub = _make_coordinator_stub(has_real_data=False)
    stub._optimistic_vacuum_state = VacuumActivity.CLEANING
    stub._optimistic_statut = CalculatedState.STARTING_PENDING
    stub._optimistic_origin_vacuum_state = VacuumActivity.DOCKED
    stub._optimistic_deadline = 1000.0

    with patch.object(coord_mod.time, "monotonic", return_value=1000.0 + 0.1):
        MyDolphinPlusCoordinator._reconcile_optimistic_overlay(stub)

    assert stub._optimistic_vacuum_state is None
    assert stub._optimistic_statut is None
    assert stub._optimistic_deadline is None


def test_reconcile_clears_on_error_shadow():
    """D1 — firmware refuses with ERROR → clear immediately so the real
    ERROR is surfaced instead of the optimistic CLEANING lie."""
    stub = _make_coordinator_stub(real_vacuum_state=VacuumActivity.ERROR)
    stub._optimistic_vacuum_state = VacuumActivity.CLEANING
    stub._optimistic_origin_vacuum_state = VacuumActivity.DOCKED
    stub._optimistic_deadline = 99_999.0

    with patch.object(coord_mod.time, "monotonic", return_value=0.0):
        MyDolphinPlusCoordinator._reconcile_optimistic_overlay(stub)

    assert stub._optimistic_vacuum_state is None


def test_reconcile_clears_when_firmware_leaves_origin():
    """Scenario A end — real state moved DOCKED → CLEANING → clear the
    overlay; the real value (now equal to the optimistic target) takes
    over without any UI hiccup."""
    stub = _make_coordinator_stub(real_vacuum_state=VacuumActivity.CLEANING)
    stub._optimistic_vacuum_state = VacuumActivity.CLEANING
    stub._optimistic_origin_vacuum_state = VacuumActivity.DOCKED
    stub._optimistic_deadline = 99_999.0

    with patch.object(coord_mod.time, "monotonic", return_value=0.0):
        MyDolphinPlusCoordinator._reconcile_optimistic_overlay(stub)

    assert stub._optimistic_vacuum_state is None


def test_reconcile_keeps_overlay_while_firmware_silent():
    """During the echo gap (real state still at origin), the overlay must
    *not* be cleared — that is precisely what it exists to mask."""
    stub = _make_coordinator_stub(real_vacuum_state=VacuumActivity.DOCKED)
    stub._optimistic_vacuum_state = VacuumActivity.CLEANING
    stub._optimistic_origin_vacuum_state = VacuumActivity.DOCKED
    stub._optimistic_deadline = 99_999.0

    with patch.object(coord_mod.time, "monotonic", return_value=0.0):
        MyDolphinPlusCoordinator._reconcile_optimistic_overlay(stub)

    assert stub._optimistic_vacuum_state == VacuumActivity.CLEANING


def test_reconcile_noop_when_overlay_unarmed():
    """An unarmed overlay must short-circuit without any state read; this
    is the per-tick hot path."""
    stub = _make_coordinator_stub()

    MyDolphinPlusCoordinator._reconcile_optimistic_overlay(stub)

    assert stub._optimistic_vacuum_state is None
    assert stub._optimistic_deadline is None


# ---------------------------------------------------------------------------
# Section 4 — Start-serialization guard (BUG-19 protection)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_guard_refuses_start_within_window_after_pause():
    """Run clicked < 15 s after Pause, before ``holdWeekly`` arrived →
    refuse. No AWS write, no overlay armed. This is the load-bearing
    protection against the BUG-19 / BUG-20 cascade."""
    stub = _make_coordinator_stub()
    # Pause was issued 5 s ago, holdWeekly not yet observed.
    stub._pause_issued_at = 1000.0

    with patch.object(coord_mod.time, "monotonic", return_value=1005.0):
        await MyDolphinPlusCoordinator._vacuum_start(stub, None, None)

    stub._aws_client.set_cleaning_mode.assert_not_called()
    assert stub._optimistic_vacuum_state is None


@pytest.mark.asyncio
async def test_guard_refuses_pickup_within_window_after_pause():
    """Pickup writes via the same ``set_cleaning_mode`` primitive — same
    race risk, same guard."""
    stub = _make_coordinator_stub()
    stub._pause_issued_at = 1000.0

    with patch.object(coord_mod.time, "monotonic", return_value=1005.0):
        await MyDolphinPlusCoordinator._pickup(stub, None)

    stub._aws_client.pickup.assert_not_called()


@pytest.mark.asyncio
async def test_guard_allows_start_after_ttl():
    """After the guard TTL elapses without an acknowledgement edge, allow
    the Run — the firmware may have dropped the pause echo, but we cannot
    block forever. v1.2 collapsed block-window and cap-timeout into a
    single threshold so allow-time and bookkeeping-clear-time coincide."""
    stub = _make_coordinator_stub()
    stub._pause_issued_at = 1000.0

    elapsed = _PAUSE_GUARD_TTL_S + 0.5
    with patch.object(coord_mod.time, "monotonic", return_value=1000.0 + elapsed):
        await MyDolphinPlusCoordinator._vacuum_start(stub, None, None)

    stub._aws_client.set_cleaning_mode.assert_called_once()


@pytest.mark.asyncio
async def test_guard_allows_start_when_unarmed():
    """No prior pause → no guard → Run goes through immediately. This is
    the common nominal case."""
    stub = _make_coordinator_stub()

    await MyDolphinPlusCoordinator._vacuum_start(stub, None, None)

    stub._aws_client.set_cleaning_mode.assert_called_once()


def test_pause_guard_reconcile_clears_on_holdweekly_edge():
    """Entering-`holdWeekly` transition observed since the pause → the
    firmware acknowledged. The guard drops and a subsequent Run is allowed.
    Note the explicit ``last_observed_calculated_state=CLEANING`` —
    without that *non-`holdWeekly`* prior, the edge predicate cannot fire
    by construction (see ``test_pause_guard_does_not_clear_...`` below for
    the regression that pinned this)."""
    stub = _make_coordinator_stub(
        real_calculated_state=CalculatedState.HOLD_WEEKLY,
        last_observed_calculated_state=CalculatedState.CLEANING,
    )
    stub._pause_issued_at = 1000.0

    with patch.object(coord_mod.time, "monotonic", return_value=1003.0):
        MyDolphinPlusCoordinator._reconcile_pause_guard(stub)

    assert stub._pause_issued_at is None


def test_pause_guard_does_not_clear_on_pre_existing_holdweekly_review_blocker_2():
    """Regression for PR #110 review blocker #2.

    A Stop clicked during the start echo gap (firmware has not yet flipped
    ``pwsState=on``, so ``calculated_state`` is still ``HOLD_WEEKLY``) must
    not clear the guard on the very next tick. Level-triggering on
    ``current == HOLD_WEEKLY`` would defeat the guard in exactly the
    BUG-19 / BUG-20 window it exists to cover. The edge predicate forbids
    the clear unless the system *left* ``HOLD_WEEKLY`` first.
    """
    stub = _make_coordinator_stub(
        real_calculated_state=CalculatedState.HOLD_WEEKLY,
        last_observed_calculated_state=CalculatedState.HOLD_WEEKLY,
    )
    stub._pause_issued_at = 1000.0

    with patch.object(coord_mod.time, "monotonic", return_value=1003.0):
        MyDolphinPlusCoordinator._reconcile_pause_guard(stub)

    assert stub._pause_issued_at == 1000.0


def test_pause_guard_reconcile_clears_on_ttl():
    """holdWeekly never arrives → the single TTL kicks in, the guard
    drops, a new Run is allowed. Without this the guard could stick
    forever if the connection dropped between the pause write and the
    holdWeekly echo. v1.2 collapsed the prior block-window / cap-timeout
    split into a single `_PAUSE_GUARD_TTL_S` threshold."""
    stub = _make_coordinator_stub(
        real_calculated_state=CalculatedState.CLEANING,
        last_observed_calculated_state=CalculatedState.CLEANING,
    )
    stub._pause_issued_at = 1000.0

    with patch.object(
        coord_mod.time, "monotonic", return_value=1000.0 + _PAUSE_GUARD_TTL_S + 0.1
    ):
        MyDolphinPlusCoordinator._reconcile_pause_guard(stub)

    assert stub._pause_issued_at is None


def test_pause_guard_reconcile_keeps_guard_while_in_window_and_not_acknowledged():
    """No holdWeekly yet, still in window → guard stays armed."""
    stub = _make_coordinator_stub(
        real_calculated_state=CalculatedState.CLEANING,
        last_observed_calculated_state=CalculatedState.CLEANING,
    )
    stub._pause_issued_at = 1000.0

    with patch.object(coord_mod.time, "monotonic", return_value=1005.0):
        MyDolphinPlusCoordinator._reconcile_pause_guard(stub)

    assert stub._pause_issued_at == 1000.0


# ---------------------------------------------------------------------------
# Section 5 — Guard resolution drops the overlay (review blocker #1 fix)
# ---------------------------------------------------------------------------


def test_guard_resolution_clears_overlay_on_ttl_review_blocker_1():
    """Regression for PR #110 review blocker #1.

    Run → Stop within the start echo gap → firmware suppresses the start
    (pause beats the start, or both are ignored). The vacuum overlay
    (`CLEANING`, from the Run) and the statut overlay (`PAUSING_PENDING`,
    from the Stop) share `origin == DOCKED` from the Run. The firmware
    never leaves `DOCKED` / `HOLD_WEEKLY`, so the vacuum overlay's
    origin-moved check cannot fire. Without HARD-11 v1.1 the overlay
    stays armed for the full 120 s TTL (UI claims "cleaning + Stopping…"
    while the robot in fact never moved).

    The v1.1 fix ties the overlay clear to the guard resolution: when
    the guard TTL elapses without an edge, the optimistic overlay is
    also dropped. v1.2 collapsed the prior block-window / cap split so
    the TTL is now a single threshold.
    """
    stub = _make_coordinator_stub(
        real_vacuum_state=VacuumActivity.DOCKED,
        real_calculated_state=CalculatedState.HOLD_WEEKLY,
        last_observed_calculated_state=CalculatedState.HOLD_WEEKLY,
    )
    # Overlay armed as if Run had just been clicked, then Stop in the gap.
    stub._optimistic_vacuum_state = VacuumActivity.CLEANING
    stub._optimistic_statut = CalculatedState.PAUSING_PENDING
    stub._optimistic_origin_vacuum_state = VacuumActivity.DOCKED
    stub._optimistic_deadline = 10_000.0  # TTL far away — must NOT be the signal
    stub._pause_issued_at = 1000.0

    # TTL reached without any edge.
    with patch.object(
        coord_mod.time, "monotonic", return_value=1000.0 + _PAUSE_GUARD_TTL_S + 0.1
    ):
        MyDolphinPlusCoordinator._reconcile_pause_guard(stub)

    assert stub._pause_issued_at is None
    assert stub._optimistic_vacuum_state is None
    assert stub._optimistic_statut is None
    assert stub._optimistic_origin_vacuum_state is None
    assert stub._optimistic_deadline is None


def test_guard_resolution_clears_overlay_on_holdweekly_edge():
    """Stop clicked while the robot was actually running → firmware acks
    by returning to `holdWeekly`. The edge fires and the optimistic
    statut (`PAUSING_PENDING`) is dropped together with the guard, so
    the chip reverts to "En attente (hebdomadaire)" without waiting for
    the TTL."""
    stub = _make_coordinator_stub(
        real_calculated_state=CalculatedState.HOLD_WEEKLY,
        last_observed_calculated_state=CalculatedState.CLEANING,
    )
    stub._optimistic_statut = CalculatedState.PAUSING_PENDING
    stub._optimistic_origin_vacuum_state = VacuumActivity.CLEANING
    stub._optimistic_deadline = 10_000.0
    stub._pause_issued_at = 1000.0

    with patch.object(coord_mod.time, "monotonic", return_value=1003.0):
        MyDolphinPlusCoordinator._reconcile_pause_guard(stub)

    assert stub._pause_issued_at is None
    assert stub._optimistic_statut is None


def test_guard_resolution_when_no_overlay_armed_is_noop():
    """Edge clear path must be idempotent on overlay slots already
    `None` — covers the corner where the guard outlives the overlay
    (which cleared earlier via the origin-moved check)."""
    stub = _make_coordinator_stub(
        real_calculated_state=CalculatedState.HOLD_WEEKLY,
        last_observed_calculated_state=CalculatedState.CLEANING,
    )
    stub._pause_issued_at = 1000.0
    # Overlay slots stay None.

    with patch.object(coord_mod.time, "monotonic", return_value=1003.0):
        MyDolphinPlusCoordinator._reconcile_pause_guard(stub)

    assert stub._pause_issued_at is None
    assert stub._optimistic_vacuum_state is None
    assert stub._optimistic_statut is None


# ---------------------------------------------------------------------------
# Section 6 — Rest-set edge predicate (v1.2 follow-up #2)
# ---------------------------------------------------------------------------


def test_pause_guard_clears_on_holddelay_edge():
    """A robot configured without an active weekly schedule settles to
    `HOLD_DELAY` after a pause, not `HOLD_WEEKLY`. v1.1 hardcoded the
    `HOLD_WEEKLY` edge so the guard would have ridden to the TTL on such
    robots; v1.2 widens the predicate to the at-rest set, so the edge
    fires correctly here."""
    stub = _make_coordinator_stub(
        real_calculated_state=CalculatedState.HOLD_DELAY,
        last_observed_calculated_state=CalculatedState.CLEANING,
    )
    stub._pause_issued_at = 1000.0

    with patch.object(coord_mod.time, "monotonic", return_value=1003.0):
        MyDolphinPlusCoordinator._reconcile_pause_guard(stub)

    assert stub._pause_issued_at is None


def test_pause_guard_clears_on_off_edge():
    """`OFF` is the `_get_updated_data` default — emitted when the
    firmware reports a `pwsState` outside the known branches. A pause
    that lands a robot back to `OFF` from `CLEANING` is a valid
    acknowledgement edge."""
    stub = _make_coordinator_stub(
        real_calculated_state=CalculatedState.OFF,
        last_observed_calculated_state=CalculatedState.CLEANING,
    )
    stub._pause_issued_at = 1000.0

    with patch.object(coord_mod.time, "monotonic", return_value=1003.0):
        MyDolphinPlusCoordinator._reconcile_pause_guard(stub)

    assert stub._pause_issued_at is None


def test_pause_guard_does_not_clear_on_pre_existing_rest_state():
    """Generalisation of v1.1's blocker #2 regression to the wider rest
    set: a Stop clicked while `prev` is already a rest state (any of
    HOLD_WEEKLY / HOLD_DELAY / OFF) must not trigger the edge on the
    very next tick, regardless of which rest state is current."""
    for state in (
        CalculatedState.HOLD_WEEKLY,
        CalculatedState.HOLD_DELAY,
        CalculatedState.OFF,
    ):
        stub = _make_coordinator_stub(
            real_calculated_state=state,
            last_observed_calculated_state=state,
        )
        stub._pause_issued_at = 1000.0

        with patch.object(coord_mod.time, "monotonic", return_value=1003.0):
            MyDolphinPlusCoordinator._reconcile_pause_guard(stub)

        assert stub._pause_issued_at == 1000.0, f"unexpected clear for {state}"


# ---------------------------------------------------------------------------
# Section 7 — Single-threshold semantics (v1.2 follow-up #1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_guard_lift_and_overlay_clear_coincide():
    """v1.1 had a 5 s sliver where `_is_start_guard_active` already
    returned False (block lifted at 15 s) but `_reconcile_pause_guard`
    had not yet fired its cap clear (scheduled for 20 s). A Run issued
    in that sliver armed a fresh overlay that the still-pending cap
    would then wipe ~4 s into the new Run's life.

    v1.2 collapsed both into a single TTL, so the precise moment the
    guard lifts coincides with the moment it stops tracking — there is
    no in-between state where a Run could land and have its fresh
    overlay invalidated by stale bookkeeping.
    """
    # 1 s past TTL: block must lift...
    stub1 = _make_coordinator_stub()
    stub1._pause_issued_at = 1000.0
    with patch.object(
        coord_mod.time, "monotonic", return_value=1000.0 + _PAUSE_GUARD_TTL_S + 1.0
    ):
        await MyDolphinPlusCoordinator._vacuum_start(stub1, None, None)
    stub1._aws_client.set_cleaning_mode.assert_called_once()

    # ...AND the bookkeeping clear must already have happened on the
    # same reconcile path, so no later tick can pull the rug from the
    # fresh overlay.
    stub2 = _make_coordinator_stub(
        last_observed_calculated_state=CalculatedState.HOLD_WEEKLY,
    )
    stub2._pause_issued_at = 1000.0
    with patch.object(
        coord_mod.time, "monotonic", return_value=1000.0 + _PAUSE_GUARD_TTL_S + 1.0
    ):
        MyDolphinPlusCoordinator._reconcile_pause_guard(stub2)
    assert stub2._pause_issued_at is None


@pytest.mark.asyncio
async def test_run_clears_pause_bookkeeping_so_late_reconcile_cannot_wipe_overlay():
    """v1.3 — closes the sub-tick residual of fragility #1.

    Even after v1.2 collapsed block and bookkeeping into one threshold,
    `_is_start_guard_active()` (checked in `_vacuum_start`) and the TTL
    clear (checked in `_reconcile_pause_guard`) are evaluated at
    different instants against the same threshold. At `t` slightly past
    TTL, a Run is allowed and arms a fresh overlay, but if the next
    coordinator tick fires before any other clear path,
    `_reconcile_pause_guard` still sees `_pause_issued_at` set and the
    elapsed past TTL → it clears the guard AND wipes the just-armed
    overlay.

    The closer: a permitted Run drops the bookkeeping itself, so the
    next reconcile early-returns and the fresh overlay survives.
    """
    stub = _make_coordinator_stub()
    stub._pause_issued_at = 1000.0

    # Run lands at t slightly past TTL → guard allows.
    with patch.object(
        coord_mod.time, "monotonic", return_value=1000.0 + _PAUSE_GUARD_TTL_S + 0.5
    ):
        await MyDolphinPlusCoordinator._vacuum_start(stub, None, None)

    assert stub._pause_issued_at is None
    assert stub._optimistic_vacuum_state == VacuumActivity.CLEANING
    assert stub._optimistic_statut == CalculatedState.STARTING_PENDING

    # Subsequent reconcile inside what would have been the sub-tick race
    # window — must not touch the fresh overlay.
    with patch.object(
        coord_mod.time, "monotonic", return_value=1000.0 + _PAUSE_GUARD_TTL_S + 0.6
    ):
        MyDolphinPlusCoordinator._reconcile_pause_guard(stub)

    assert stub._optimistic_vacuum_state == VacuumActivity.CLEANING
    assert stub._optimistic_statut == CalculatedState.STARTING_PENDING


@pytest.mark.asyncio
async def test_pickup_clears_pause_bookkeeping_so_late_reconcile_cannot_wipe_overlay():
    """Same residual closer applies to `_pickup`, which goes through the
    same `set_cleaning_mode` primitive and therefore shares the race."""
    stub = _make_coordinator_stub()
    stub._pause_issued_at = 1000.0

    with patch.object(
        coord_mod.time, "monotonic", return_value=1000.0 + _PAUSE_GUARD_TTL_S + 0.5
    ):
        await MyDolphinPlusCoordinator._pickup(stub, None)

    assert stub._pause_issued_at is None
    assert stub._optimistic_vacuum_state == VacuumActivity.RETURNING

    with patch.object(
        coord_mod.time, "monotonic", return_value=1000.0 + _PAUSE_GUARD_TTL_S + 0.6
    ):
        MyDolphinPlusCoordinator._reconcile_pause_guard(stub)

    assert stub._optimistic_vacuum_state == VacuumActivity.RETURNING
