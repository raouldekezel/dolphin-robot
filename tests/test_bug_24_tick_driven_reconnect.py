"""Regression tests for BUG-24 — tick-driven reconnect.

Pre-fix ``_handle_connection_failure`` was single-shot per status
transition: it slept the backoff and called ``_api.initialize()``
exactly once, then returned. On a retry that left the status
unchanged (``FAILED`` → ``FAILED``, ``EXPIRED_TOKEN`` →
``EXPIRED_TOKEN``), ``_set_status``'s state-change guard skipped the
dispatch, no new ``_handle_connection_failure`` fired, and the
integration stayed dormant until reload. Confirmed empirically on
2026-07-10 (log timeline in issue #120): reconnection attempts stopped
at #2 and never advanced to #3/#4/#5 despite the network healing.

Post-fix, ``_handle_connection_failure`` no longer sleeps or calls
``initialize`` — it just terminates the AWS client and (when the API
is not in a user-action state) seeds the retry schedule. The
coordinator's ~30 s tick (``_async_update_data``) drives retries via
``_maybe_reconnect``:

* fires ``_api.initialize()`` when the integration is not fully
  connected AND the API is not in a user-action state AND the
  scheduled backoff has elapsed;
* schedules the next attempt (``1 → 2 → 4 → 8 → 15`` min capped) so
  the no-dispatch halt case can never park the integration.

Critically, the predicate considers **both** the API and the AWS
client — an AWS-only failure (MQTT drop with API still CONNECTED)
also triggers a retry, because re-driving ``_api.initialize()``
cascades to ``_aws_client.initialize()`` via the CONNECTED dispatch.
The PR-first version of BUG-24 gated on ``_api.status`` alone and
regressed AWS/MQTT recovery entirely — this test file covers the
combined predicate to prevent that.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.mydolphin_plus.common.connectivity_status import (
    ConnectivityStatus,
)
from custom_components.mydolphin_plus.managers.coordinator import (
    _NEEDS_USER_STATUSES,
    MyDolphinPlusCoordinator,
)


def _stub_coordinator(
    api_status: ConnectivityStatus = ConnectivityStatus.FAILED,
    aws_status: ConnectivityStatus = ConnectivityStatus.DISCONNECTED,
):
    """Minimum coordinator stand-in for the BUG-24 methods under test.

    ``_handle_connection_failure`` / ``_maybe_reconnect`` /
    ``_schedule_next_retry`` / ``_is_fully_connected`` / ``_aws_status``
    only touch ``_api``, ``_aws_client``, ``_reconnection_attempts`` and
    ``_next_retry_at``.
    """
    stub = MagicMock(spec=MyDolphinPlusCoordinator)
    stub._api = MagicMock()
    stub._api.status = api_status
    stub._api.initialize = AsyncMock()
    stub._aws_client = MagicMock()
    stub._aws_client.status = aws_status
    stub._aws_client.terminate = AsyncMock()
    stub._reconnection_attempts = 0
    stub._next_retry_at = 0.0

    # Bind the real helpers so state advances are observable when
    # `_handle_connection_failure` / `_maybe_reconnect` call them via
    # `self`. Without this, MagicMock replaces them and side-effects
    # (counter bumps, predicate reads) vanish.
    stub._schedule_next_retry = (
        lambda now: MyDolphinPlusCoordinator._schedule_next_retry(stub, now)
    )
    stub._aws_status = lambda: MyDolphinPlusCoordinator._aws_status(stub)
    stub._is_fully_connected = lambda: MyDolphinPlusCoordinator._is_fully_connected(
        stub
    )
    return stub


# ---------------------------------------------------------------------------
# Predicate — deliberately narrower than is_disconnected()
# ---------------------------------------------------------------------------


def test_needs_user_statuses_are_correct():
    """User-action states must NOT trigger tick-retry:
    ``EXPIRED_TOKEN`` needs OTP flow, ``INVALID_*`` and
    ``MISSING_API_KEY`` need the operator. Retrying just re-hits the
    same fatal state (and, for EXPIRED_TOKEN, can race the reauth)."""
    assert _NEEDS_USER_STATUSES == frozenset(
        {
            ConnectivityStatus.EXPIRED_TOKEN,
            ConnectivityStatus.INVALID_CREDENTIALS,
            ConnectivityStatus.INVALID_ACCOUNT,
            ConnectivityStatus.MISSING_API_KEY,
        }
    )
    # FAILED and NOT_CONNECTED must be retryable — that's the whole point.
    for s in [ConnectivityStatus.FAILED, ConnectivityStatus.NOT_CONNECTED]:
        assert s not in _NEEDS_USER_STATUSES


# ---------------------------------------------------------------------------
# _handle_connection_failure — seed only, no drive
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_connection_failure_terminates_and_seeds_on_retryable():
    """The dispatch entry point must NOT call ``_api.initialize()``
    itself — doing both here and in the tick would race two concurrent
    initialize() calls. It must terminate and seed when the API status
    is retryable."""
    stub = _stub_coordinator(api_status=ConnectivityStatus.FAILED)

    await MyDolphinPlusCoordinator._handle_connection_failure(stub)

    stub._aws_client.terminate.assert_awaited_once()
    stub._api.initialize.assert_not_awaited()  # ← load-bearing
    assert stub._reconnection_attempts == 1
    assert stub._next_retry_at > 0.0


@pytest.mark.asyncio
async def test_handle_connection_failure_skips_seed_on_needs_user():
    """On EXPIRED_TOKEN / INVALID_* the retry schedule must NOT be
    seeded — the tick can't fix these and the counter would just tick
    up pointlessly, cluttering the log."""
    stub = _stub_coordinator(api_status=ConnectivityStatus.EXPIRED_TOKEN)

    await MyDolphinPlusCoordinator._handle_connection_failure(stub)

    stub._aws_client.terminate.assert_awaited_once()
    stub._api.initialize.assert_not_awaited()
    assert stub._reconnection_attempts == 0
    assert stub._next_retry_at == 0.0


# ---------------------------------------------------------------------------
# _maybe_reconnect — tick-driven retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_maybe_reconnect_skips_when_unseeded():
    """Fresh startup: ``_next_retry_at == 0.0`` before any failure has
    seeded the schedule. Even with API at NOT_CONNECTED the tick must
    NOT fire — the initial `initialize()` is driven by
    ``coordinator.initialize()``, not by the tick."""
    stub = _stub_coordinator(api_status=ConnectivityStatus.NOT_CONNECTED)
    stub._next_retry_at = 0.0

    await MyDolphinPlusCoordinator._maybe_reconnect(stub, now=100.0)

    stub._api.initialize.assert_not_awaited()


@pytest.mark.asyncio
async def test_maybe_reconnect_skips_when_backoff_not_elapsed():
    stub = _stub_coordinator(api_status=ConnectivityStatus.FAILED)
    stub._next_retry_at = 1_000_000.0
    now = 999_999.0  # 1 s before the scheduled retry

    await MyDolphinPlusCoordinator._maybe_reconnect(stub, now)

    stub._api.initialize.assert_not_awaited()


@pytest.mark.asyncio
async def test_maybe_reconnect_fires_on_api_failed():
    stub = _stub_coordinator(api_status=ConnectivityStatus.FAILED)
    stub._next_retry_at = 1_000_000.0
    now = 1_000_001.0

    # Persistent outage: initialize() runs but leaves status FAILED.
    async def failing():
        stub._api.status = ConnectivityStatus.FAILED

    stub._api.initialize.side_effect = failing

    await MyDolphinPlusCoordinator._maybe_reconnect(stub, now)

    stub._api.initialize.assert_awaited_once()
    # Failed retry must schedule the next attempt (this is the load-
    # bearing anti-halt behaviour — pre-fix, the halt happened right
    # here).
    assert stub._reconnection_attempts == 1
    assert stub._next_retry_at > now


@pytest.mark.asyncio
async def test_maybe_reconnect_fires_on_aws_only_failure_with_api_connected():
    """Regression guard for the #122 review finding.

    A pure MQTT drop leaves the API at CONNECTED and only the AWS
    client goes to FAILED. The pre-review version of BUG-24 gated on
    ``_api.status`` alone and never fired here — the integration got
    stuck with API "connected", MQTT dead, no data, until reload.

    Post-review: the tick fires an `_api.initialize()` even though the
    API is CONNECTED, because that's what triggers the CONNECTED
    cascade that re-inits the AWS client.
    """
    stub = _stub_coordinator(
        api_status=ConnectivityStatus.CONNECTED,
        aws_status=ConnectivityStatus.FAILED,
    )
    # AWS-side dispatch would have called `_handle_connection_failure`
    # which seeded the schedule; simulate that seeded state directly.
    stub._reconnection_attempts = 1
    stub._next_retry_at = 1_000_000.0
    now = 1_000_001.0

    # Simulate a healthy re-init: the CONNECTED cascade would then
    # re-init the AWS client via `_on_api_status_changed(CONNECTED)`.
    async def healthy():
        stub._api.status = ConnectivityStatus.CONNECTED

    stub._api.initialize.side_effect = healthy

    await MyDolphinPlusCoordinator._maybe_reconnect(stub, now)

    stub._api.initialize.assert_awaited_once()


@pytest.mark.asyncio
async def test_maybe_reconnect_does_not_reschedule_after_api_becomes_connected():
    """After a successful retry, the API is CONNECTED. The CONNECTED
    dispatch cascades to ``_aws_client.initialize()`` asynchronously
    and its handler will reset the retry schedule. ``_maybe_reconnect``
    must NOT reschedule from here — doing so would fire another
    `initialize()` in ~1 min while AWS is still catching up."""
    stub = _stub_coordinator(api_status=ConnectivityStatus.FAILED)
    stub._next_retry_at = 1_000_000.0
    stub._reconnection_attempts = 3
    initial_next = stub._next_retry_at
    now = 1_000_001.0

    async def healthy():
        stub._api.status = ConnectivityStatus.CONNECTED

    stub._api.initialize.side_effect = healthy

    await MyDolphinPlusCoordinator._maybe_reconnect(stub, now)

    stub._api.initialize.assert_awaited_once()
    assert stub._reconnection_attempts == 3
    assert stub._next_retry_at == initial_next


@pytest.mark.asyncio
async def test_maybe_reconnect_does_not_reschedule_after_needs_user():
    """If the retry surfaces a user-action state (e.g. Cognito
    rejected the refresh token → EXPIRED_TOKEN), the tick must NOT
    reschedule — the recovery path is the OTP reauth flow, not
    another `initialize()`."""
    stub = _stub_coordinator(api_status=ConnectivityStatus.FAILED)
    stub._next_retry_at = 1_000_000.0
    stub._reconnection_attempts = 2
    initial_next = stub._next_retry_at
    now = 1_000_001.0

    async def rejected():
        stub._api.status = ConnectivityStatus.EXPIRED_TOKEN

    stub._api.initialize.side_effect = rejected

    await MyDolphinPlusCoordinator._maybe_reconnect(stub, now)

    stub._api.initialize.assert_awaited_once()
    assert stub._reconnection_attempts == 2
    assert stub._next_retry_at == initial_next


@pytest.mark.parametrize(
    "api_status,should_fire",
    [
        (ConnectivityStatus.FAILED, True),
        (ConnectivityStatus.NOT_CONNECTED, True),
        (ConnectivityStatus.DISCONNECTED, True),  # aws-terminate side-effect
        (ConnectivityStatus.CONNECTING, True),  # incomplete init, seed present
        (ConnectivityStatus.TEMPORARY_CONNECTED, True),  # login mid-way
        (ConnectivityStatus.API_NOT_FOUND, True),  # server hostname, retry harmless
        (ConnectivityStatus.EXPIRED_TOKEN, False),
        (ConnectivityStatus.INVALID_CREDENTIALS, False),
        (ConnectivityStatus.INVALID_ACCOUNT, False),
        (ConnectivityStatus.MISSING_API_KEY, False),
        (ConnectivityStatus.CONNECTED, True),  # w/ aws NOT CONNECTED, must fire
    ],
    ids=lambda s: str(s) if isinstance(s, ConnectivityStatus) else str(s),
)
@pytest.mark.asyncio
async def test_maybe_reconnect_predicate_api_side(api_status, should_fire):
    """Sweep API statuses with AWS held at NOT_CONNECTED (the state
    after a terminate/failure). Only the user-action statuses must
    suppress the tick; every other non-fully-connected combination
    should fire so we cover both API- and AWS-triggered failures."""
    stub = _stub_coordinator(
        api_status=api_status, aws_status=ConnectivityStatus.NOT_CONNECTED
    )
    stub._next_retry_at = 1.0  # seeded, trivially elapsed
    now = 100.0

    async def noop():
        pass  # don't mutate status

    stub._api.initialize.side_effect = noop

    await MyDolphinPlusCoordinator._maybe_reconnect(stub, now)

    if should_fire:
        stub._api.initialize.assert_awaited_once()
    else:
        stub._api.initialize.assert_not_awaited()


@pytest.mark.asyncio
async def test_maybe_reconnect_skips_when_fully_connected():
    """Both sides connected → the tick must be a no-op."""
    stub = _stub_coordinator(
        api_status=ConnectivityStatus.CONNECTED,
        aws_status=ConnectivityStatus.CONNECTED,
    )
    stub._next_retry_at = 1.0  # even seeded, must not fire

    await MyDolphinPlusCoordinator._maybe_reconnect(stub, now=100.0)

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
    stub = _stub_coordinator(
        api_status=ConnectivityStatus.FAILED,
        aws_status=ConnectivityStatus.NOT_CONNECTED,
    )

    async def keep_failing():
        stub._api.status = ConnectivityStatus.FAILED

    stub._api.initialize.side_effect = keep_failing

    # Seed via the dispatch path (attempt #1 scheduled, +1 min).
    await MyDolphinPlusCoordinator._handle_connection_failure(stub)
    assert stub._reconnection_attempts == 1
    assert stub._api.initialize.await_count == 0  # seed does NOT fire

    # Simulate five ticks — each just at the previously scheduled retry.
    fires = 0
    for _ in range(5):
        now = stub._next_retry_at
        await MyDolphinPlusCoordinator._maybe_reconnect(stub, now)
        fires += 1
        assert stub._api.initialize.await_count == fires, (
            f"tick #{fires} must fire initialize (halt regression)"
        )

    assert stub._reconnection_attempts >= 6


@pytest.mark.asyncio
async def test_aws_only_outage_recovers_via_tick():
    """End-to-end for the MQTT-timeout scenario from the #122 review:
    AWS drops with API still CONNECTED, the tick must eventually
    re-drive `_api.initialize()` (which cascades to AWS reconnect via
    the CONNECTED dispatch)."""
    stub = _stub_coordinator(
        api_status=ConnectivityStatus.CONNECTED,
        aws_status=ConnectivityStatus.FAILED,
    )

    # AWS-side dispatch would seed the schedule.
    await MyDolphinPlusCoordinator._handle_connection_failure(stub)
    assert stub._reconnection_attempts == 1
    assert stub._next_retry_at > 0.0

    # Tick fires at the scheduled time — initialize succeeds.
    async def healthy():
        stub._api.status = ConnectivityStatus.CONNECTED  # no change, but signals recovery

    stub._api.initialize.side_effect = healthy
    now = stub._next_retry_at
    await MyDolphinPlusCoordinator._maybe_reconnect(stub, now)

    stub._api.initialize.assert_awaited_once()
    # No further scheduling — the CONNECTED dispatch owns the AWS
    # re-init and the counter reset.
    assert stub._reconnection_attempts == 1


@pytest.mark.asyncio
async def test_recovery_stops_the_retry_loop():
    """Once a tick-retry succeeds and the CONNECTED cascade re-inits
    AWS, the dispatch handler (not exercised here) resets counters.
    ``_maybe_reconnect`` itself must NOT schedule another retry."""
    stub = _stub_coordinator(
        api_status=ConnectivityStatus.FAILED,
        aws_status=ConnectivityStatus.NOT_CONNECTED,
    )

    await MyDolphinPlusCoordinator._handle_connection_failure(stub)
    assert stub._reconnection_attempts == 1

    async def healthy():
        stub._api.status = ConnectivityStatus.CONNECTED

    stub._api.initialize.side_effect = healthy
    initial_next = stub._next_retry_at

    await MyDolphinPlusCoordinator._maybe_reconnect(stub, initial_next)

    stub._api.initialize.assert_awaited_once()
    assert stub._reconnection_attempts == 1
    assert stub._next_retry_at == initial_next
