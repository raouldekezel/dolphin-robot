"""Tests for BUG-06 + BUG-07 — clean unload of AWS IoT + cancellable backoff.

BUG-06: AWSClient.terminate previously registered a done callback on the
disconnect future but did not await it, flipping status to DISCONNECTED
immediately. With clean_session=False and client_id=entry_id, a fresh
setup() right after unload would evict the previous AWS IoT session and
the old client's callbacks could try to act on dead objects. The fix
awaits the disconnect future with a 10s timeout, then transitions
status.

BUG-07: coordinator._handle_connection_failure did ``await sleep(backoff)``
without any CancelledError handling. A config entry unload during the
sleep let the sleep finish, then _api.initialize() ran after the unload
completed, racing the next setup_entry. The fix re-raises
CancelledError so HA Core can actually cancel the task.
"""

from __future__ import annotations

import asyncio
import inspect
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


# --- BUG-06: AWSClient.terminate awaits the disconnect future --------------


@pytest.mark.asyncio
async def test_bug06_terminate_waits_for_disconnect_future_to_complete():
    """terminate() must await the disconnect future before flipping DISCONNECTED."""
    from custom_components.mydolphin_plus.common.connectivity_status import (
        ConnectivityStatus,
    )
    from custom_components.mydolphin_plus.managers.aws_client import AWSClient

    stub = MagicMock(spec=AWSClient)
    # _set_status is called at the end — capture the call.
    stub._set_status = MagicMock()

    # Disconnect returns a concurrent.futures.Future that takes a real moment
    # to complete. We control its completion via a small future_done flag.
    import concurrent.futures

    fut = concurrent.futures.Future()
    stub._awsiot_client = MagicMock()
    stub._awsiot_client.disconnect = MagicMock(return_value=fut)

    async def _resolver():
        await asyncio.sleep(0.05)  # let terminate() reach the await
        fut.set_result(None)

    resolver_task = asyncio.create_task(_resolver())
    await AWSClient.terminate(stub)
    await resolver_task

    # awsiot_client cleared, DISCONNECTED set.
    assert stub._awsiot_client is None, "client should be cleared after disconnect"
    stub._set_status.assert_called_once()
    args, _kwargs = stub._set_status.call_args
    assert args[0] == ConnectivityStatus.DISCONNECTED


@pytest.mark.asyncio
async def test_bug06_terminate_times_out_gracefully():
    """If disconnect never resolves, terminate must still complete within ~10s."""
    from custom_components.mydolphin_plus.common.connectivity_status import (
        ConnectivityStatus,
    )
    from custom_components.mydolphin_plus.managers import aws_client as aws_module
    from custom_components.mydolphin_plus.managers.aws_client import AWSClient

    import concurrent.futures

    fut = concurrent.futures.Future()  # never resolved
    stub = MagicMock(spec=AWSClient)
    stub._set_status = MagicMock()
    stub._awsiot_client = MagicMock()
    stub._awsiot_client.disconnect = MagicMock(return_value=fut)

    # Speed up the timeout for the test.
    import contextlib

    @contextlib.asynccontextmanager
    async def _patched_wait_for(coro, timeout):  # noqa: ARG001
        raise asyncio.TimeoutError

    # Easier: monkeypatch asyncio.wait_for at the module level.
    import unittest.mock as mock

    async def _raise_timeout(_coro, timeout):  # noqa: ARG001
        raise asyncio.TimeoutError

    with mock.patch.object(aws_module.asyncio, "wait_for", _raise_timeout):
        await AWSClient.terminate(stub)

    # awsiot_client cleared, status DISCONNECTED, no exception leaked.
    assert stub._awsiot_client is None
    stub._set_status.assert_called_once()
    assert stub._set_status.call_args.args[0] == ConnectivityStatus.DISCONNECTED


# --- BUG-07: backoff sleep is cancellation-safe ----------------------------


@pytest.mark.asyncio
async def test_bug07_handle_connection_failure_re_raises_cancellation():
    """A cancel during the backoff sleep must propagate, not be swallowed."""
    from custom_components.mydolphin_plus.managers.coordinator import (
        MyDolphinPlusCoordinator,
    )

    stub = MagicMock(spec=MyDolphinPlusCoordinator)
    stub._reconnection_attempts = 0
    stub._aws_client = MagicMock()
    stub._aws_client.terminate = AsyncMock()
    stub._api = MagicMock()
    stub._api.initialize = AsyncMock()

    task = asyncio.create_task(
        MyDolphinPlusCoordinator._handle_connection_failure(stub)
    )
    await asyncio.sleep(0.01)  # let the task reach the sleep call
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    # _api.initialize must NOT have been called after cancellation
    stub._api.initialize.assert_not_called()


# --- Source-level regressions ----------------------------------------------


def _aws_client_source() -> str:
    from custom_components.mydolphin_plus.managers import aws_client as mod

    return Path(inspect.getfile(mod)).read_text(encoding="utf-8")


def _coordinator_source() -> str:
    from custom_components.mydolphin_plus.managers import coordinator as mod

    return Path(inspect.getfile(mod)).read_text(encoding="utf-8")


def test_bug06_source_awaits_disconnect_future():
    """terminate() body must await the disconnect future (via asyncio.wait_for / wrap_future)."""
    src = _aws_client_source()
    body_match = re.search(
        r"async def terminate\(self\):.*?(?=\n    (?:async )?def )",
        src,
        re.DOTALL,
    )
    assert body_match is not None
    body = body_match.group(0)
    assert "await" in body, "terminate() does not contain any await"
    assert "wrap_future" in body or "wait_for" in body, (
        "terminate() should await the disconnect future via "
        "asyncio.wrap_future / asyncio.wait_for"
    )


def test_bug07_source_handles_cancellation_in_backoff():
    """_handle_connection_failure must catch and re-raise CancelledError."""
    src = _coordinator_source()
    body_match = re.search(
        r"async def _handle_connection_failure\(self\):.*?(?=\n    (?:async )?def )",
        src,
        re.DOTALL,
    )
    assert body_match is not None
    body = body_match.group(0)
    assert "CancelledError" in body, (
        "_handle_connection_failure does not mention CancelledError — "
        "the sleep is not cancellation-safe"
    )
