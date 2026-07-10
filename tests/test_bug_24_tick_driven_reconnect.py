"""Regression tests for BUG-24 — tick-driven reconnect.

Pre-fix ``_handle_connection_failure`` was single-shot per status
transition: it slept the backoff and called ``_api.initialize()``
exactly once, then returned. On a retry that left the API status
unchanged (``FAILED`` → ``FAILED``, ``EXPIRED_TOKEN`` →
``EXPIRED_TOKEN``), ``_set_status``'s state-change guard skipped the
dispatch, no new ``_handle_connection_failure`` fired, and the
integration stayed dormant until reload. Confirmed empirically on
2026-07-10 (log timeline in issue #120): reconnection attempts stopped
at #2 and never advanced to #3/#4/#5 despite the network healing.

Post-fix, ``_handle_connection_failure`` no longer sleeps or calls
``initialize`` — it just terminates the AWS client and seeds the retry
schedule. The coordinator's ~30 s tick (``_async_update_data``) drives
retries via ``_maybe_reconnect``:

* fires ``_api.initialize()`` when the status is in the narrow
  ``{FAILED, NOT_CONNECTED}`` set and the scheduled backoff has
  elapsed, then
* schedules the next attempt (``1 → 2 → 4 → 8 → 15`` min capped) so
  that the no-dispatch halt case can never park the integration.

Predicate is deliberately narrower than ``is_disconnected()``:
``EXPIRED_TOKEN`` and ``INVALID_*`` are excluded so a tick-retry never
re-hits "no refresh token stored" forever, races the OTP reauth flow,
or spams the API on a fatal state that needs user action.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.mydolphin_plus.common.connectivity_status import (
    ConnectivityStatus,
)
from custom_components.mydolphin_plus.managers.coordinator import (
    _RETRYABLE_STATUSES,
    MyDolphinPlusCoordinator,
)


def _stub_coordinator(status: ConnectivityStatus = ConnectivityStatus.FAILED):
    """Minimum coordinator stand-in for the BUG-24 methods under test.

    ``_handle_connection_failure`` / ``_maybe_reconnect`` /
    ``_schedule_next_retry`` only touch ``_api``, ``_aws_client``,
    ``_reconnection_attempts`` and ``_next_retry_at``.
    """
    stub = MagicMock(spec=MyDolphinPlusCoordinator)
    stub._api = MagicMock()
    stub._api.status = status
    stub._api.initialize = AsyncMock()
    stub._aws_client = MagicMock()
    stub._aws_client.terminate = AsyncMock()
    stub._reconnection_attempts = 0
    stub._next_retry_at = 0.0
    # Bind the real ``_schedule_next_retry`` so counter / timestamp writes
    # are observable when ``_handle_connection_failure`` and
    # ``_maybe_reconnect`` call ``self._schedule_next_retry(...)``.
    # Without this, MagicMock replaces it and the side effects vanish.
    stub._schedule_next_retry = lambda now: MyDolphinPlusCoordinator._schedule_next_retry(
        stub, now
    )
    return stub


# ---------------------------------------------------------------------------
# Predicate — deliberately narrower than is_disconnected()
# ---------------------------------------------------------------------------


def test_retryable_statuses_are_narrow():
    """The tick-retry predicate is limited to ``FAILED`` and
    ``NOT_CONNECTED``. Widening it to ``is_disconnected()`` would
    tick-retry ``EXPIRED_TOKEN`` (needs OTP flow, never recovers by
    itself) and ``INVALID_*`` (need user action) — pointless and
    interfering with the reauth flow."""
    assert _RETRYABLE_STATUSES == frozenset(
        {ConnectivityStatus.FAILED, ConnectivityStatus.NOT_CONNECTED}
    )
    for s in [
        ConnectivityStatus.EXPIRED_TOKEN,
        ConnectivityStatus.INVALID_CREDENTIALS,
        ConnectivityStatus.INVALID_ACCOUNT,
        ConnectivityStatus.MISSING_API_KEY,
        ConnectivityStatus.DISCONNECTED,
        ConnectivityStatus.API_NOT_FOUND,
    ]:
        assert s not in _RETRYABLE_STATUSES, f"{s} must not be tick-retried"


# ---------------------------------------------------------------------------
# _handle_connection_failure — seed only, no drive
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_connection_failure_terminates_and_seeds_only():
    """The dispatch entry point must NOT call ``_api.initialize()``
    itself — doing both here and in the tick would race two concurrent
    initialize() calls. It must only terminate and seed the schedule."""
    stub = _stub_coordinator()

    await MyDolphinPlusCoordinator._handle_connection_failure(stub)

    stub._aws_client.terminate.assert_awaited_once()
    stub._api.initialize.assert_not_awaited()  # ← load-bearing
    assert stub._reconnection_attempts == 1
    assert stub._next_retry_at > 0.0


# ---------------------------------------------------------------------------
# _maybe_reconnect — tick-driven retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_maybe_reconnect_skips_when_backoff_not_elapsed():
    stub = _stub_coordinator(ConnectivityStatus.FAILED)
    stub._next_retry_at = 1_000_000.0
    now = 999_999.0  # 1 s before the scheduled retry

    await MyDolphinPlusCoordinator._maybe_reconnect(stub, now)

    stub._api.initialize.assert_not_awaited()


@pytest.mark.asyncio
async def test_maybe_reconnect_fires_on_failed_when_backoff_elapsed():
    stub = _stub_coordinator(ConnectivityStatus.FAILED)
    stub._next_retry_at = 1_000_000.0
    now = 1_000_001.0

    # Simulate: initialize() ran but left status FAILED (persistent outage).
    async def failing_initialize():
        stub._api.status = ConnectivityStatus.FAILED

    stub._api.initialize.side_effect = failing_initialize

    await MyDolphinPlusCoordinator._maybe_reconnect(stub, now)

    stub._api.initialize.assert_awaited_once()
    # Failed retry must schedule the next attempt (this is the load-bearing
    # anti-halt behaviour — pre-fix, the halt happened right here).
    assert stub._reconnection_attempts == 1
    assert stub._next_retry_at > now


@pytest.mark.asyncio
async def test_maybe_reconnect_does_not_reschedule_after_success():
    """After a successful retry the API dispatches ``CONNECTED`` and
    ``_on_api_status_changed`` resets both counters. ``_maybe_reconnect``
    itself must NOT bump / reschedule when the retry recovered."""
    stub = _stub_coordinator(ConnectivityStatus.FAILED)
    stub._next_retry_at = 1_000_000.0
    stub._reconnection_attempts = 3
    initial_next = stub._next_retry_at
    now = 1_000_001.0

    async def healthy_initialize():
        stub._api.status = ConnectivityStatus.CONNECTED

    stub._api.initialize.side_effect = healthy_initialize

    await MyDolphinPlusCoordinator._maybe_reconnect(stub, now)

    stub._api.initialize.assert_awaited_once()
    # Reset is the responsibility of the dispatch handler
    # (`_on_api_status_changed(CONNECTED)`), not of `_maybe_reconnect`.
    assert stub._reconnection_attempts == 3
    assert stub._next_retry_at == initial_next


@pytest.mark.parametrize(
    "status,should_fire",
    [
        (ConnectivityStatus.FAILED, True),
        (ConnectivityStatus.NOT_CONNECTED, True),
        (ConnectivityStatus.EXPIRED_TOKEN, False),
        (ConnectivityStatus.INVALID_CREDENTIALS, False),
        (ConnectivityStatus.INVALID_ACCOUNT, False),
        (ConnectivityStatus.MISSING_API_KEY, False),
        (ConnectivityStatus.DISCONNECTED, False),
        (ConnectivityStatus.API_NOT_FOUND, False),
        (ConnectivityStatus.CONNECTED, False),
        (ConnectivityStatus.CONNECTING, False),
        (ConnectivityStatus.TEMPORARY_CONNECTED, False),
    ],
    ids=lambda s: str(s) if isinstance(s, ConnectivityStatus) else str(s),
)
@pytest.mark.asyncio
async def test_maybe_reconnect_predicate(status, should_fire):
    """Only FAILED and NOT_CONNECTED trigger a tick retry. In particular
    EXPIRED_TOKEN must not — retry re-hits 'no refresh token stored'
    forever and can interfere with an active OTP reauth flow."""
    stub = _stub_coordinator(status)
    stub._next_retry_at = 0.0  # trivially elapsed
    now = 100.0

    async def keep_status():
        pass  # don't mutate _api.status

    stub._api.initialize.side_effect = keep_status

    await MyDolphinPlusCoordinator._maybe_reconnect(stub, now)

    if should_fire:
        stub._api.initialize.assert_awaited_once()
    else:
        stub._api.initialize.assert_not_awaited()


# ---------------------------------------------------------------------------
# Backoff schedule — 1 → 2 → 4 → 8 → 15 (capped)
# ---------------------------------------------------------------------------


def test_schedule_next_retry_advances_1_2_4_8_15_then_caps():
    """Exponential backoff ``2**n`` capped at 15 min, matching the
    documented 1 → 2 → 4 → 8 → 15 schedule. Called directly against
    ``_schedule_next_retry`` for a hermetic assertion — no wall-clock
    dependency."""
    stub = _stub_coordinator()
    stub._reconnection_attempts = 0

    expected = [1, 2, 4, 8, 15, 15, 15, 15]  # capped from 5th attempt onward
    now = 100.0

    for i, minutes in enumerate(expected, start=1):
        MyDolphinPlusCoordinator._schedule_next_retry(stub, now)
        assert stub._reconnection_attempts == i, f"attempt count wrong at #{i}"
        delta = stub._next_retry_at - now
        assert delta == pytest.approx(minutes * 60, abs=0.01), (
            f"expected {minutes}min backoff at attempt #{i}, got {delta:.1f}s"
        )


# ---------------------------------------------------------------------------
# End-to-end anti-halt — direct-inverse of the smoking-gun scenario
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repeated_failures_keep_advancing_never_halts():
    """The BUG-24 smoking gun was: after backoff #2, subsequent ticks
    produced ``Status is Expired Token`` (no dispatch, no re-arm) and
    the integration stayed dormant. Post-fix, ticks keep firing at
    each backoff boundary until the network heals or the integration
    is unloaded."""
    stub = _stub_coordinator(ConnectivityStatus.FAILED)

    async def keep_failing():
        stub._api.status = ConnectivityStatus.FAILED

    stub._api.initialize.side_effect = keep_failing

    # Seed via the dispatch path (attempt #1 scheduled, +1 min).
    await MyDolphinPlusCoordinator._handle_connection_failure(stub)
    assert stub._reconnection_attempts == 1
    assert stub._api.initialize.await_count == 0  # seed does NOT fire

    # Simulate five ticks — each just past the previously scheduled retry.
    fires = 0
    for _ in range(5):
        # Tick fires exactly at the scheduled time (accepted by `>=` guard).
        now = stub._next_retry_at
        await MyDolphinPlusCoordinator._maybe_reconnect(stub, now)
        fires += 1
        assert stub._api.initialize.await_count == fires, (
            f"tick #{fires} must fire initialize (halt regression)"
        )

    # Attempts have advanced past #2 — this is exactly what the pre-fix
    # code could not do.
    assert stub._reconnection_attempts >= 6


@pytest.mark.asyncio
async def test_recovery_stops_the_retry_loop():
    """Once a tick-retry succeeds, `_api` dispatches CONNECTED; the
    dispatch handler (not exercised here) resets the counters. This
    test validates the observable at the ``_maybe_reconnect`` layer:
    the successful tick does NOT itself schedule a next retry."""
    stub = _stub_coordinator(ConnectivityStatus.FAILED)

    # Seed one attempt.
    await MyDolphinPlusCoordinator._handle_connection_failure(stub)
    assert stub._reconnection_attempts == 1

    # First tick fires, network is back.
    async def healthy_initialize():
        stub._api.status = ConnectivityStatus.CONNECTED

    stub._api.initialize.side_effect = healthy_initialize
    initial_next = stub._next_retry_at

    await MyDolphinPlusCoordinator._maybe_reconnect(stub, initial_next)

    stub._api.initialize.assert_awaited_once()
    # No fresh scheduling because status is CONNECTED after initialize().
    assert stub._reconnection_attempts == 1
    assert stub._next_retry_at == initial_next
