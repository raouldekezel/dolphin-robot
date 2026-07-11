"""Regression tests for BUG-24 follow-up — retry state-machine hardening.

The initial BUG-24 fix (PR #122, tag `raoul.23`) turned the retry loop
into a tick-driven driver so that a subsequent failed retry that left
the status unchanged (no dispatch, no re-arm) could no longer park the
integration. The comment left on the issue on 2026-07-11 identified
three residual weaknesses in the retry state machine that this file
pins:

1. **Exceptions bypass the backoff on the API-CONNECTED / AWS-FAILED
   path** — the pre-fix predicate `api.status != CONNECTED` skipped
   the finally-block reschedule when ``initialize()`` raised on the
   API-CONNECTED / AWS-FAILED path (the status was unchanged so
   ``api.status == CONNECTED`` held). ``_next_retry_at`` stayed in the
   past → the next tick fired immediately, silently collapsing the
   backoff to the ~30 s tick cadence. The fix widens the predicate to
   ``not _is_fully_connected()``.

2. **Deadline calculated from start-of-attempt, not completion** —
   a slow ``initialize()`` shortened the effective interval to the
   next retry. The fix samples ``time.monotonic()`` at the *end* of
   the awaited call and uses that for the reschedule. Also switches
   the whole state machine to monotonic time so wall-clock jumps
   (NTP correction, DST) cannot skip retries or fire them early.

3. **One failed attempt can schedule twice** — the callback path
   (``_api.initialize()`` sets FAILED → ``_on_api_status_changed`` →
   ``_handle_connection_failure`` → ``_schedule_next_retry``) and the
   ``finally`` block both scheduled, so a single failure could
   increment the counter twice and skip a stage in the
   ``1 → 2 → 4 → 8 → 15`` sequence. The fix adds a
   ``_reconnect_in_progress`` guard that suppresses callback-driven
   scheduling for the duration of an attempt; ``_maybe_reconnect``'s
   ``finally`` is the sole scheduler for its own attempt.

Plus the reset-only-when-both-healthy invariant: on API-CONNECTED
alone (AWS still coming up), the retry state is *paused* (deadline
cleared, counter kept). Only when both sides are CONNECTED does the
counter reset — otherwise a subsequent AWS failure would restart the
backoff from #0 while the API is limping.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.mydolphin_plus.common.connectivity_status import (
    ConnectivityStatus,
)
from custom_components.mydolphin_plus.managers.coordinator import (
    MyDolphinPlusCoordinator,
)


def _stub():
    """Coordinator stub — same wiring as `test_bug_24_tick_driven_reconnect.py`."""
    stub = MagicMock(spec=MyDolphinPlusCoordinator)
    stub._api = MagicMock()
    stub._api.status = ConnectivityStatus.CONNECTED
    stub._api.initialize = AsyncMock()
    stub._api.update = AsyncMock()
    stub._aws_client = MagicMock()
    stub._aws_client.status = ConnectivityStatus.FAILED
    stub._aws_client.terminate = AsyncMock()
    stub._aws_client.update = AsyncMock()
    stub._aws_client.update_api_data = AsyncMock()
    stub._aws_client.initialize = AsyncMock()
    stub._reconnection_attempts = 0
    stub._next_retry_at = 0.0
    stub._reconnect_in_progress = False
    stub._config_manager = MagicMock()
    stub._config_manager.entry_id = "entry-id-1"
    stub.api_data = {}

    stub._schedule_next_retry = (
        lambda now_mono=None: MyDolphinPlusCoordinator._schedule_next_retry(
            stub, now_mono
        )
    )
    stub._aws_status = lambda: MyDolphinPlusCoordinator._aws_status(stub)
    stub._is_fully_connected = lambda: MyDolphinPlusCoordinator._is_fully_connected(
        stub
    )
    return stub


# ---------------------------------------------------------------------------
# BUG #1 — exceptions must not bypass the backoff on the AWS-only failure path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reschedules_when_initialize_raises_and_api_stays_connected(
    monkeypatch,
):
    """API-CONNECTED, AWS-FAILED path: ``_maybe_reconnect`` fires
    ``_api.initialize()`` (to cascade to an AWS re-init via the
    CONNECTED dispatch), but ``initialize()`` raises before mutating
    any status. The pre-fix predicate ``api.status != CONNECTED``
    skipped the reschedule because the API status was unchanged
    (still CONNECTED). ``_next_retry_at`` stayed in the past → next
    tick fired immediately with no backoff.

    The fix predicate ``not _is_fully_connected()`` catches this: AWS
    is still FAILED so the compound state is not healthy, and the
    finally schedules the next attempt normally."""
    stub = _stub()
    stub._api.status = ConnectivityStatus.CONNECTED
    stub._aws_client.status = ConnectivityStatus.FAILED
    stub._next_retry_at = 1_000_000.0
    now = 1_000_001.0
    monkeypatch.setattr(
        "custom_components.mydolphin_plus.managers.coordinator.time.monotonic",
        lambda: now,
    )

    async def blows_up():
        # Don't touch statuses; the exception escapes before any
        # `_set_status` call would flip API to FAILED. This is the
        # pathological case: API stays CONNECTED, the reschedule
        # predicate must NOT skip.
        raise RuntimeError("unexpected storage failure")

    stub._api.initialize.side_effect = blows_up

    await MyDolphinPlusCoordinator._maybe_reconnect(stub, now)

    stub._api.initialize.assert_awaited_once()
    # Reschedule ran; backoff advanced from the current attempt count.
    assert stub._reconnection_attempts == 1
    assert stub._next_retry_at > now


@pytest.mark.asyncio
async def test_reschedules_when_api_connected_and_aws_still_failed_after_attempt(
    monkeypatch,
):
    """Same predicate widening, but the exception-free path: the API
    stays CONNECTED across the attempt (a re-login early-outs), AWS
    fails to come up. The finally must still schedule — pre-fix, the
    ``api.status != CONNECTED`` predicate skipped this case too."""
    stub = _stub()
    stub._api.status = ConnectivityStatus.CONNECTED
    stub._aws_client.status = ConnectivityStatus.FAILED
    stub._next_retry_at = 1_000_000.0
    now = 1_000_001.0
    monkeypatch.setattr(
        "custom_components.mydolphin_plus.managers.coordinator.time.monotonic",
        lambda: now,
    )

    async def noop():
        # `_api.initialize()` completes with API still CONNECTED, AWS
        # still FAILED (the cascade did not succeed for whatever
        # reason — network mid-recovery, AWS quota, etc.).
        pass

    stub._api.initialize.side_effect = noop

    await MyDolphinPlusCoordinator._maybe_reconnect(stub, now)

    stub._api.initialize.assert_awaited_once()
    assert stub._reconnection_attempts == 1
    assert stub._next_retry_at > now


# ---------------------------------------------------------------------------
# BUG #2 — deadline calculated from END of attempt, monotonic clock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reschedule_deadline_uses_end_of_attempt_monotonic(monkeypatch):
    """Pre-fix: the deadline was computed from a wall-clock timestamp
    captured *before* ``_api.initialize()``. If ``initialize()`` took
    say 90 s, the reschedule was effectively 90 s shorter than
    intended.

    Post-fix: the deadline is computed from ``time.monotonic()``
    sampled *after* the awaited call. This test simulates a slow
    ``initialize()`` by advancing the monotonic clock between the two
    reads."""
    stub = _stub()
    stub._api.status = ConnectivityStatus.FAILED
    stub._next_retry_at = 100.0
    start_mono = 100.0
    slow_end_mono = 190.0  # simulate 90 s inside initialize()

    clock = {"t": start_mono}

    def read():
        return clock["t"]

    monkeypatch.setattr(
        "custom_components.mydolphin_plus.managers.coordinator.time.monotonic",
        read,
    )

    async def slow_failing():
        # Advance the "clock" during the awaited call.
        clock["t"] = slow_end_mono
        stub._api.status = ConnectivityStatus.FAILED

    stub._api.initialize.side_effect = slow_failing

    await MyDolphinPlusCoordinator._maybe_reconnect(stub, start_mono + 1)

    # Deadline computed from end-of-attempt (190.0), not start (100.0).
    # First attempt scheduled with 1 min backoff → deadline = 190 + 60.
    assert stub._next_retry_at == pytest.approx(slow_end_mono + 60, abs=0.01)


@pytest.mark.asyncio
async def test_reschedule_after_needs_user_clears_deadline(monkeypatch):
    """When the attempt surfaces a user-action state (EXPIRED_TOKEN),
    the deadline must be cleared to 0.0 — the OTP flow owns recovery
    from here. Pre-fix, the deadline stayed at its old value; a later
    accidental fall-through could have fired the tick immediately."""
    stub = _stub()
    stub._api.status = ConnectivityStatus.FAILED
    stub._next_retry_at = 1_000_000.0
    stub._reconnection_attempts = 3
    now = 1_000_001.0
    monkeypatch.setattr(
        "custom_components.mydolphin_plus.managers.coordinator.time.monotonic",
        lambda: now,
    )

    async def token_expired():
        stub._api.status = ConnectivityStatus.EXPIRED_TOKEN

    stub._api.initialize.side_effect = token_expired

    await MyDolphinPlusCoordinator._maybe_reconnect(stub, now)

    # Deadline cleared; counter kept (it will be reset by the OTP
    # completion when the API flips to CONNECTED and AWS follows).
    assert stub._next_retry_at == 0.0
    assert stub._reconnection_attempts == 3


# ---------------------------------------------------------------------------
# BUG #3 — no double scheduling from callback + finally
# ---------------------------------------------------------------------------


def test_schedule_next_retry_is_suppressed_while_attempt_in_flight():
    """Direct test of the guard: while ``_reconnect_in_progress`` is
    True, ``_schedule_next_retry`` must be a no-op. This is what
    prevents the failure-callback path from double-counting the
    attempt against ``_maybe_reconnect``'s own ``finally``."""
    stub = _stub()
    stub._reconnection_attempts = 2
    stub._next_retry_at = 42.0
    stub._reconnect_in_progress = True

    MyDolphinPlusCoordinator._schedule_next_retry(stub, 999.0)

    # No side effect: counter and deadline unchanged.
    assert stub._reconnection_attempts == 2
    assert stub._next_retry_at == 42.0


def test_schedule_next_retry_uses_time_monotonic_when_no_arg(monkeypatch):
    """When called without an explicit ``now_mono`` (as
    ``_handle_connection_failure`` does), the scheduler falls back on
    ``time.monotonic()``. Pins that the fallback wall-clock source
    ``datetime.now()`` is no longer in the path."""
    stub = _stub()
    stub._reconnection_attempts = 0
    stub._next_retry_at = 0.0
    stub._reconnect_in_progress = False
    monkeypatch.setattr(
        "custom_components.mydolphin_plus.managers.coordinator.time.monotonic",
        lambda: 500.0,
    )

    MyDolphinPlusCoordinator._schedule_next_retry(stub)

    assert stub._reconnection_attempts == 1
    # First attempt: 1 min backoff from 500.0 monotonic.
    assert stub._next_retry_at == pytest.approx(560.0, abs=0.01)


@pytest.mark.asyncio
async def test_no_double_schedule_when_callback_fires_during_attempt(
    monkeypatch,
):
    """End-to-end: the attempt inside ``_maybe_reconnect`` triggers a
    failure callback that would call ``_handle_connection_failure``
    → ``_schedule_next_retry``. Without the guard, the counter would
    bump twice per attempt and the sequence
    ``1 → 2 → 4 → 8 → 15`` would skip stages.

    The test wires ``_api.initialize()``'s side effect to invoke
    ``_handle_connection_failure`` (as the real
    ``_on_api_status_changed(FAILED)`` dispatch would) mid-attempt.
    The finally must be the *only* scheduler for the attempt."""
    stub = _stub()
    stub._api.status = ConnectivityStatus.FAILED
    stub._next_retry_at = 1_000_000.0
    stub._reconnection_attempts = 0
    now = 1_000_001.0
    monkeypatch.setattr(
        "custom_components.mydolphin_plus.managers.coordinator.time.monotonic",
        lambda: now,
    )

    async def failing_with_callback():
        # Simulate `_set_status(FAILED)` firing the dispatch, which in
        # the real integration cascades to `_handle_connection_failure`.
        stub._api.status = ConnectivityStatus.FAILED
        await MyDolphinPlusCoordinator._handle_connection_failure(stub)

    stub._api.initialize.side_effect = failing_with_callback

    await MyDolphinPlusCoordinator._maybe_reconnect(stub, now)

    # Counter bumped exactly once (finally is the sole scheduler for
    # its own attempt). Pre-fix, the counter would have bumped twice.
    assert stub._reconnection_attempts == 1


@pytest.mark.asyncio
async def test_backoff_sequence_survives_callback_double_path(monkeypatch):
    """Sequence-level version of the previous test. Five consecutive
    ticks each with a mid-attempt failure callback: the counter must
    still land at exactly 5 and the intervals must follow the
    ``1 → 2 → 4 → 8 → 15`` sequence.

    Pre-fix (double scheduling), the counter would jump by 2 per
    tick and the sequence would collapse to
    ``1 → 4 → 15`` (skipping 2 and 8)."""
    stub = _stub()
    stub._api.status = ConnectivityStatus.FAILED
    stub._reconnect_in_progress = False
    stub._reconnection_attempts = 0

    clock = {"t": 100.0}
    monkeypatch.setattr(
        "custom_components.mydolphin_plus.managers.coordinator.time.monotonic",
        lambda: clock["t"],
    )

    async def failing_with_callback():
        stub._api.status = ConnectivityStatus.FAILED
        await MyDolphinPlusCoordinator._handle_connection_failure(stub)

    stub._api.initialize.side_effect = failing_with_callback

    # Seed via the dispatch path.
    await MyDolphinPlusCoordinator._handle_connection_failure(stub)
    assert stub._reconnection_attempts == 1
    initial_deadline = stub._next_retry_at

    # Five ticks. Deadlines should be initial + 2 min, +4 min, +8 min,
    # +15 min, +15 min (capped) — 5 stages of the 1→2→4→8→15 sequence
    # already consumed once by the seed.
    expected_deltas = [2 * 60, 4 * 60, 8 * 60, 15 * 60, 15 * 60]
    prev_deadline = initial_deadline
    for i, delta in enumerate(expected_deltas, start=2):
        clock["t"] = prev_deadline  # tick fires exactly at the deadline
        await MyDolphinPlusCoordinator._maybe_reconnect(stub, clock["t"])
        assert stub._reconnection_attempts == i, (
            f"double-schedule regression at tick #{i - 1}: "
            f"expected counter={i}, got {stub._reconnection_attempts}"
        )
        assert stub._next_retry_at == pytest.approx(
            clock["t"] + delta, abs=0.01
        ), f"backoff stage wrong at tick #{i - 1}"
        prev_deadline = stub._next_retry_at


# ---------------------------------------------------------------------------
# No overlapping tick-driven retries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_tick_skips_when_reconnect_already_in_progress():
    """A slow ``initialize()`` can outlive the coordinator tick
    interval (30 s). The second tick must be a no-op — otherwise two
    ``_api.initialize()`` calls race, competing for the same login
    lock and possibly producing two authentication flights against
    Cognito for the same credentials."""
    stub = _stub()
    stub._api.status = ConnectivityStatus.FAILED
    stub._next_retry_at = 100.0
    stub._reconnect_in_progress = True  # first tick already in flight

    await MyDolphinPlusCoordinator._maybe_reconnect(stub, 200.0)

    stub._api.initialize.assert_not_awaited()


# ---------------------------------------------------------------------------
# Reset only when both sides healthy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_connected_dispatch_clears_deadline_but_keeps_counter():
    """The API-CONNECTED handler pauses the tick (deadline cleared) so
    it does not fire another ``_api.initialize()`` while
    ``_on_api_status_changed`` is still setting up AWS. But the
    counter is *not* reset — because if AWS fails, the follow-up
    reschedule should continue from the current attempt count, not
    restart from #0."""
    stub = _stub()
    stub._api.status = ConnectivityStatus.CONNECTED
    stub._aws_client.status = ConnectivityStatus.NOT_CONNECTED
    stub._reconnection_attempts = 4
    stub._next_retry_at = 500.0

    await MyDolphinPlusCoordinator._on_api_status_changed(
        stub, "entry-id-1", ConnectivityStatus.CONNECTED
    )

    # Deadline paused, counter preserved.
    assert stub._next_retry_at == 0.0
    assert stub._reconnection_attempts == 4


@pytest.mark.asyncio
async def test_aws_connected_dispatch_resets_only_when_fully_connected():
    """The AWS-CONNECTED handler resets the retry state — but only
    when the API is also CONNECTED. If AWS somehow reports CONNECTED
    while the API is still limping (rare race), the reset must be
    skipped so the follow-up API failure does not restart backoff
    from #0."""
    # Case A: compound healthy → reset.
    stub = _stub()
    stub._api.status = ConnectivityStatus.CONNECTED
    stub._aws_client.status = ConnectivityStatus.CONNECTED
    stub._reconnection_attempts = 5
    stub._next_retry_at = 500.0

    await MyDolphinPlusCoordinator._on_aws_client_status_changed(
        stub, "entry-id-1", ConnectivityStatus.CONNECTED
    )

    assert stub._reconnection_attempts == 0
    assert stub._next_retry_at == 0.0

    # Case B: compound not healthy → do NOT reset.
    stub = _stub()
    stub._api.status = ConnectivityStatus.FAILED  # API still limping
    stub._aws_client.status = ConnectivityStatus.CONNECTED
    stub._reconnection_attempts = 5
    stub._next_retry_at = 500.0

    await MyDolphinPlusCoordinator._on_aws_client_status_changed(
        stub, "entry-id-1", ConnectivityStatus.CONNECTED
    )

    assert stub._reconnection_attempts == 5
    assert stub._next_retry_at == 500.0
