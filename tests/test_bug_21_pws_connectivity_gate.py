"""Regression tests for BUG-21 Fix 1 — PWS connectivity gate on Run / Pickup.

The Maytronics firmware consumes a ``desired.cleaningMode.mode`` delta on
reconnect if one is sitting in the shadow. So a ``vacuum.start`` service
call fired while the PWS↔cloud session is down does not fail: the write
lands in the shadow as a pending ``desired`` and is replayed as a start
when the robot reconnects (Scenario B in #112). The observable
behaviour is the same class of surprise as BUG-13's "start without a
Run", but with a different trigger — reconnect vs. mode-pick.

The Fix 1 gate refuses to write a start toward a PWS whose cloud
session is known-down. Signal source is the raw ``isConnected.connected``
flag (parsed by FEAT-07 into ``SystemDetails.pws_connected``), read
directly, with **no debounce**. The cost asymmetry established in the
BUG-21 Q1 analysis favours fail-safe refusal: a false refusal during a
~20 s session flap costs the operator one retry; a false allowance
during the first minute of a real outage queues a ``desired`` and
recreates Scenario B.

Tri-state policy: refuse only on explicit ``False``. ``None`` (cold
start, section absent, non-bool payload) falls through — the existing
``_publish`` no-op when the integration's own AWS link is not
``CONNECTED`` covers that window.

Ordering is load-bearing (D2 of the #112 design):

* Precedes the HARD-11 start-serialization guard — a refused start
  must surface a *connectivity* error, not a "previous pause not
  acknowledged" one, when the shadow already tells us the robot is
  gone.
* Precedes ``_pause_issued_at = None`` — the HARD-11 guard's
  bookkeeping must not be dropped by a write that will not happen.
* Precedes ``_arm_optimistic_start`` — never arm the CLEANING /
  RETURNING overlay for a write that raises; the UI would otherwise
  show ``cleaning`` for the full TTL on a dead robot.

These tests target the public handlers (``_vacuum_start``, ``_pickup``)
through a ``MagicMock``-backed coordinator stub. No source-text grep
(CHORE-02).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from custom_components.mydolphin_plus.common.calculated_state import CalculatedState
from custom_components.mydolphin_plus.common.consts import DOMAIN
from custom_components.mydolphin_plus.managers import coordinator as coord_mod
from custom_components.mydolphin_plus.managers.coordinator import (
    MyDolphinPlusCoordinator,
)
from homeassistant.components.vacuum import VacuumActivity
from homeassistant.exceptions import ServiceValidationError

# ---------------------------------------------------------------------------
# Stub builder — same shape as HARD-11's, plus the FEAT-07 tri-state slot.
# ---------------------------------------------------------------------------


def _make_coordinator_stub(*, pws_connected: bool | None = True):
    """Coordinator stub that exposes just the surface the two handlers
    read: ``_system_details.pws_connected``, ``_aws_client`` (``pickup``
    / ``set_cleaning_mode``), the HARD-11 overlay + guard slots, and
    the BUG-13 staged mode.
    """
    stub = MyDolphinPlusCoordinator.__new__(MyDolphinPlusCoordinator)
    stub._desired_clean_mode = "all"
    stub._last_seen_reported_clean_mode = "all"
    stub._system_details = SimpleNamespace(
        vacuum_state=VacuumActivity.DOCKED,
        calculated_state=CalculatedState.HOLD_WEEKLY,
        is_active=False,
        data={},
        pws_connected=pws_connected,
    )
    stub._has_real_data = True
    stub._aws_client = MagicMock()
    stub._aws_client.data = {}
    stub.async_update_listeners = MagicMock()

    # HARD-11 overlay slots — initially unarmed.
    stub._optimistic_vacuum_state = None
    stub._optimistic_statut = None
    stub._optimistic_origin_vacuum_state = None
    stub._optimistic_deadline = None
    stub._pause_issued_at = None
    stub._last_observed_calculated_state = None

    return stub


# ---------------------------------------------------------------------------
# Section 1 — Refusal on explicit False
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vacuum_start_refused_when_pws_disconnected():
    """Explicit ``pws_connected=False`` → the handler raises
    ``ServiceValidationError`` with the ``power_supply_disconnected``
    translation key, and does not reach any of the write paths."""
    stub = _make_coordinator_stub(pws_connected=False)

    with pytest.raises(ServiceValidationError) as excinfo:
        await MyDolphinPlusCoordinator._vacuum_start(stub, None, None)

    assert excinfo.value.translation_domain == DOMAIN
    assert excinfo.value.translation_key == "power_supply_disconnected"

    # Zero side effects: no AWS write, no listener bump, no overlay.
    stub._aws_client.set_cleaning_mode.assert_not_called()
    stub.async_update_listeners.assert_not_called()
    assert stub._optimistic_vacuum_state is None
    assert stub._optimistic_statut is None


@pytest.mark.asyncio
async def test_pickup_refused_when_pws_disconnected():
    """Same gate on the pickup path — both write via
    ``set_cleaning_mode`` and both would queue a stale ``desired`` on an
    offline PWS."""
    stub = _make_coordinator_stub(pws_connected=False)

    with pytest.raises(ServiceValidationError) as excinfo:
        await MyDolphinPlusCoordinator._pickup(stub, None)

    assert excinfo.value.translation_domain == DOMAIN
    assert excinfo.value.translation_key == "power_supply_disconnected"

    stub._aws_client.pickup.assert_not_called()
    stub.async_update_listeners.assert_not_called()
    assert stub._optimistic_vacuum_state is None
    assert stub._optimistic_statut is None


# ---------------------------------------------------------------------------
# Section 2 — Tri-state: True and None fall through
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vacuum_start_passes_through_when_pws_connected():
    """Nominal path — connected robot, gate is transparent."""
    stub = _make_coordinator_stub(pws_connected=True)

    await MyDolphinPlusCoordinator._vacuum_start(stub, None, None)

    stub._aws_client.set_cleaning_mode.assert_called_once_with("all")
    assert stub._optimistic_vacuum_state == VacuumActivity.CLEANING


@pytest.mark.asyncio
async def test_pickup_passes_through_when_pws_connected():
    stub = _make_coordinator_stub(pws_connected=True)

    await MyDolphinPlusCoordinator._pickup(stub, None)

    stub._aws_client.pickup.assert_called_once()
    assert stub._optimistic_vacuum_state == VacuumActivity.RETURNING


@pytest.mark.asyncio
async def test_vacuum_start_passes_through_on_none():
    """Cold-start / section-absent / non-bool → ``None``. The gate must
    not refuse — the ``_publish`` layer already no-ops when the
    integration's own AWS link is not ``CONNECTED``. Refusing here would
    reject legitimate starts made moments after HA restart."""
    stub = _make_coordinator_stub(pws_connected=None)

    await MyDolphinPlusCoordinator._vacuum_start(stub, None, None)

    stub._aws_client.set_cleaning_mode.assert_called_once_with("all")


@pytest.mark.asyncio
async def test_pickup_passes_through_on_none():
    stub = _make_coordinator_stub(pws_connected=None)

    await MyDolphinPlusCoordinator._pickup(stub, None)

    stub._aws_client.pickup.assert_called_once()


# ---------------------------------------------------------------------------
# Section 3 — Ordering: connectivity error wins over HARD-11 guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connectivity_check_precedes_hard11_guard_on_start():
    """When both refusal conditions are present (disconnected PWS *and*
    the HARD-11 pause guard is still active), the connectivity error
    surfaces — it is the honest cause. The HARD-11 guard's silent
    return would swallow the operator's real problem."""
    stub = _make_coordinator_stub(pws_connected=False)
    # Seed the HARD-11 guard as active: a recent pause bookkeeping.
    stub._pause_issued_at = 1000.0

    with patch.object(coord_mod.time, "monotonic", return_value=1001.0):
        with pytest.raises(ServiceValidationError) as excinfo:
            await MyDolphinPlusCoordinator._vacuum_start(stub, None, None)

    assert excinfo.value.translation_key == "power_supply_disconnected"
    # The HARD-11 bookkeeping must NOT be dropped: the gate ran before
    # the ``_pause_issued_at = None`` line and did not fall through.
    assert stub._pause_issued_at == 1000.0
    stub._aws_client.set_cleaning_mode.assert_not_called()


@pytest.mark.asyncio
async def test_connectivity_check_precedes_hard11_guard_on_pickup():
    stub = _make_coordinator_stub(pws_connected=False)
    stub._pause_issued_at = 1000.0

    with patch.object(coord_mod.time, "monotonic", return_value=1001.0):
        with pytest.raises(ServiceValidationError) as excinfo:
            await MyDolphinPlusCoordinator._pickup(stub, None)

    assert excinfo.value.translation_key == "power_supply_disconnected"
    assert stub._pause_issued_at == 1000.0
    stub._aws_client.pickup.assert_not_called()


# ---------------------------------------------------------------------------
# Section 4 — Overlay is NOT armed on refusal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refused_start_does_not_arm_optimistic_overlay():
    """A refused start must not leave the UI showing CLEANING for the
    full HARD-11 TTL. The overlay slots are unchanged after the raise."""
    stub = _make_coordinator_stub(pws_connected=False)

    with pytest.raises(ServiceValidationError):
        await MyDolphinPlusCoordinator._vacuum_start(stub, None, None)

    assert stub._optimistic_vacuum_state is None
    assert stub._optimistic_statut is None
    assert stub._optimistic_origin_vacuum_state is None
    assert stub._optimistic_deadline is None


@pytest.mark.asyncio
async def test_refused_pickup_does_not_arm_optimistic_overlay():
    stub = _make_coordinator_stub(pws_connected=False)

    with pytest.raises(ServiceValidationError):
        await MyDolphinPlusCoordinator._pickup(stub, None)

    assert stub._optimistic_vacuum_state is None
    assert stub._optimistic_statut is None
    assert stub._optimistic_origin_vacuum_state is None
    assert stub._optimistic_deadline is None


# ---------------------------------------------------------------------------
# Section 5 — Translation key parity (en/fr/it, per FEAT-06 F3 lesson)
# ---------------------------------------------------------------------------


_TRANSLATIONS_DIR = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "mydolphin_plus"
    / "translations"
)
_STRINGS_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "mydolphin_plus"
    / "strings.json"
)


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_exception_translation_key_present_in_strings():
    """The base ``strings.json`` carries the source of truth for the
    exception key. Missing it means HA falls back to the raw key on the
    UI toast."""
    data = _load_json(_STRINGS_PATH)
    assert "exceptions" in data, "strings.json missing exceptions block"
    assert "power_supply_disconnected" in data["exceptions"]
    assert "message" in data["exceptions"]["power_supply_disconnected"]


@pytest.mark.parametrize("lang", ["en", "fr", "it"])
def test_exception_translation_key_present_per_language(lang: str):
    """en/fr/it parity: `it.json` is in-repo (FEAT-06 F3) and any new
    translation-key surface must cover all three. A missing key would
    silently render the raw ``power_supply_disconnected`` string in
    that locale."""
    data = _load_json(_TRANSLATIONS_DIR / f"{lang}.json")
    assert "exceptions" in data, f"{lang}.json missing exceptions block"
    entry = data["exceptions"].get("power_supply_disconnected")
    assert entry is not None, f"{lang}.json missing power_supply_disconnected"
    message = entry.get("message")
    assert isinstance(message, str) and message.strip(), (
        f"{lang}.json power_supply_disconnected message must be non-empty"
    )
