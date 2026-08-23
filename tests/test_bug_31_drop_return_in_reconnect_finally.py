"""Regression tests for BUG-31 — no `return` inside `_maybe_reconnect`'s finally.

A `return` executed inside a `finally` discards a propagating exception,
and `except Exception` around `initialize()` does not catch
`asyncio.CancelledError`. These tests pin that a cancellation propagates
out of `_maybe_reconnect`, and that the non-user-action path still
reschedules the next attempt.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.mydolphin_plus.common.connectivity_status import (
    ConnectivityStatus,
)
from custom_components.mydolphin_plus.managers.coordinator import (
    MyDolphinPlusCoordinator,
)


def _stub():
    """Coordinator stub — same wiring as `test_bug_24_followup_hardening.py`."""
    stub = MagicMock(spec=MyDolphinPlusCoordinator)
    stub._api = MagicMock()
    stub._api.status = ConnectivityStatus.CONNECTED
    stub._api.initialize = AsyncMock()
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
    stub._device_ready_dispatched = True

    stub._schedule_next_retry = (
        lambda now_mono=None: MyDolphinPlusCoordinator._schedule_next_retry(
            stub, now_mono
        )
    )
    stub._ensure_retry_scheduled = (
        lambda now_mono=None: MyDolphinPlusCoordinator._ensure_retry_scheduled(
            stub, now_mono
        )
    )
    stub._aws_status = lambda: MyDolphinPlusCoordinator._aws_status(stub)
    stub._is_fully_connected = lambda: MyDolphinPlusCoordinator._is_fully_connected(
        stub
    )
    return stub


@pytest.mark.asyncio
async def test_cancellation_in_failed_keeps_the_schedule(monkeypatch):
    """Cancellation on the FAILED path still reschedules and propagates."""
    stub = _stub()
    stub._api.status = ConnectivityStatus.FAILED
    stub._aws_client.status = ConnectivityStatus.FAILED
    stub._next_retry_at = 1_000_000.0
    now = 1_000_001.0
    end_mono = 1_000_050.0
    monkeypatch.setattr(
        "custom_components.mydolphin_plus.managers.coordinator.time.monotonic",
        lambda: end_mono,
    )

    async def cancelled():
        raise asyncio.CancelledError()

    stub._api.initialize.side_effect = cancelled

    with pytest.raises(asyncio.CancelledError):
        await MyDolphinPlusCoordinator._maybe_reconnect(stub, now)

    stub._api.initialize.assert_awaited_once()
    # The finally cleared the in-flight guard and rescheduled from the
    # end-of-attempt monotonic time; the counter bumped once.
    assert stub._reconnect_in_progress is False
    assert stub._reconnection_attempts == 1
    assert stub._next_retry_at == pytest.approx(end_mono + 60, abs=0.01)


@pytest.mark.asyncio
async def test_cancellation_into_user_action_state_schedules_nothing(monkeypatch):
    """Cancellation into a user-action state schedules nothing and propagates.

    Start from ``FAILED`` so the attempt fires past the entry guard, then
    flip to ``EXPIRED_TOKEN`` and raise ``CancelledError`` inside
    ``initialize()``.
    """
    stub = _stub()
    stub._api.status = ConnectivityStatus.FAILED
    stub._aws_client.status = ConnectivityStatus.FAILED
    stub._next_retry_at = 1_000_000.0
    stub._reconnection_attempts = 3
    now = 1_000_001.0
    monkeypatch.setattr(
        "custom_components.mydolphin_plus.managers.coordinator.time.monotonic",
        lambda: 1_000_050.0,
    )

    async def token_expired_then_cancelled():
        stub._api.status = ConnectivityStatus.EXPIRED_TOKEN
        raise asyncio.CancelledError()

    stub._api.initialize.side_effect = token_expired_then_cancelled

    with pytest.raises(asyncio.CancelledError):
        await MyDolphinPlusCoordinator._maybe_reconnect(stub, now)

    stub._api.initialize.assert_awaited_once()
    # OTP flow owns recovery: nothing scheduled. The deadline was consumed
    # at the top of the attempt and stays cleared; the counter is untouched.
    assert stub._reconnect_in_progress is False
    assert stub._next_retry_at == 0.0
    assert stub._reconnection_attempts == 3
