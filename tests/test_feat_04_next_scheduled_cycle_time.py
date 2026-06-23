"""Behavioural tests for ``sensor.{robot}_next_scheduled_cycle_time`` (FEAT-04,
post-BUG-18 repointing).

Before BUG-18 (#88), the sensor sourced its value from
``reported.cleaningModes[next_scheduled_mode]``. Two diag sessions
(``docs/diag/2026-06-22_bug-18_catalog-reset-across-reboot/`` and
``docs/diag/2026-06-23_bug-18_cycletime-vs-nextcycleduration-sync/``)
showed that the firmware actually uses the carried-forward
``reported.cycleInfo.cleaningMode.cycleTime`` (persisted across PWS
reboots) and that the catalog read is structurally wrong: it gets reset
to firmware defaults at every PWS reboot, so the sensor predicts a
value the firmware will never use.

Post-fix: ``_get_next_scheduled_cycle_time_data`` reads
``cycleInfo.cleaningMode.cycleTime`` directly. The schedule
existence guard (``_next_scheduled_data is None`` → state ``None``) is
preserved so the three next-scheduled sensors stay in lockstep.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from custom_components.mydolphin_plus.common.consts import (
    DATA_CYCLE_INFO_CLEANING_MODE,
    DATA_CYCLE_INFO_CLEANING_MODE_DURATION,
    DATA_SECTION_CYCLE_INFO,
)
from custom_components.mydolphin_plus.managers.coordinator import (
    MyDolphinPlusCoordinator,
)

ATTR_STATE = "state"


def _make_coordinator_stub(
    *, aws_data: dict, next_scheduled_data: dict | None
) -> MagicMock:
    """Build a coordinator stub exposing just what the getter reads.

    ``_next_scheduled_data`` gates the sensor (no schedule → state
    ``None``); ``aws_data`` carries the live shadow document.
    """
    stub = MagicMock(spec=MyDolphinPlusCoordinator)
    stub.aws_data = aws_data
    stub._next_scheduled_data = next_scheduled_data
    return stub


def _call(stub) -> dict | None:
    return MyDolphinPlusCoordinator._get_next_scheduled_cycle_time_data(
        stub, SimpleNamespace()
    )


def _cycle_info(cycle_time: object) -> dict:
    return {
        DATA_SECTION_CYCLE_INFO: {
            DATA_CYCLE_INFO_CLEANING_MODE: {
                DATA_CYCLE_INFO_CLEANING_MODE_DURATION: cycle_time,
            },
        },
    }


_SCHEDULE = {"state": "irrelevant"}  # any non-None: schedule exists


# --- Happy path: cycleInfo carries the duration ----------------------------


def test_returns_cycle_info_cycle_time_when_schedule_exists():
    """Schedule active and ``cycleInfo.cleaningMode.cycleTime`` is a positive
    int → sensor returns that value, regardless of the scheduled mode."""
    stub = _make_coordinator_stub(
        aws_data=_cycle_info(150),
        next_scheduled_data=_SCHEDULE,
    )
    assert _call(stub) == {ATTR_STATE: 150}


def test_returns_cycle_info_value_even_when_diverges_from_catalog():
    """The BUG-18 scenario: catalog says one thing, cycleInfo says another.
    Sensor must surface cycleInfo — the firmware uses that one at trigger."""
    aws_data = _cycle_info(150)
    aws_data["cleaningModes"] = {"all": 180}  # catalog disagrees
    stub = _make_coordinator_stub(aws_data=aws_data, next_scheduled_data=_SCHEDULE)
    assert _call(stub) == {ATTR_STATE: 150}


# --- Lockstep with the other next-scheduled sensors ------------------------


def test_returns_none_when_no_schedule_active():
    """No weekly + no delay → ``_next_scheduled_data`` is ``None`` and the
    sensor returns ``None`` (in lockstep with ``next_scheduled_run`` and
    ``next_scheduled_mode``)."""
    stub = _make_coordinator_stub(
        aws_data=_cycle_info(150),
        next_scheduled_data=None,
    )
    assert _call(stub) == {ATTR_STATE: None}


# --- cycleInfo absent / malformed → None -----------------------------------


def test_returns_none_when_cycle_info_section_missing():
    """Cold start before the first AWS shadow message arrives."""
    stub = _make_coordinator_stub(aws_data={}, next_scheduled_data=_SCHEDULE)
    assert _call(stub) == {ATTR_STATE: None}


def test_returns_none_when_cleaning_mode_missing():
    """``cycleInfo`` present but no ``cleaningMode`` subkey yet."""
    stub = _make_coordinator_stub(
        aws_data={DATA_SECTION_CYCLE_INFO: {}},
        next_scheduled_data=_SCHEDULE,
    )
    assert _call(stub) == {ATTR_STATE: None}


def test_returns_none_when_cycle_time_missing():
    """``cleaningMode`` present but no ``cycleTime`` key (firmware has not
    emitted a ``Set cycle time`` yet)."""
    stub = _make_coordinator_stub(
        aws_data={
            DATA_SECTION_CYCLE_INFO: {
                DATA_CYCLE_INFO_CLEANING_MODE: {"mode": "all"},
            },
        },
        next_scheduled_data=_SCHEDULE,
    )
    assert _call(stub) == {ATTR_STATE: None}


def test_returns_none_for_non_positive_cycle_time():
    """Zero and negative durations are rejected — the sensor reports
    unavailable rather than emit a nonsense reading."""
    for bad in (0, -1, -120):
        stub = _make_coordinator_stub(
            aws_data=_cycle_info(bad),
            next_scheduled_data=_SCHEDULE,
        )
        assert _call(stub) == {ATTR_STATE: None}, bad


def test_returns_none_for_non_int_cycle_time():
    """Strings, dicts, floats, None — anything the firmware would not emit
    as a minute count is treated as malformed shadow data."""
    for bad in ("180", 180.0, {"minutes": 180}, [180], None):
        stub = _make_coordinator_stub(
            aws_data=_cycle_info(bad),
            next_scheduled_data=_SCHEDULE,
        )
        assert _call(stub) == {ATTR_STATE: None}, bad


def test_returns_none_for_bool_cycle_time():
    """``bool`` is a subclass of ``int`` in Python — explicitly reject it."""
    stub = _make_coordinator_stub(
        aws_data=_cycle_info(True),
        next_scheduled_data=_SCHEDULE,
    )
    assert _call(stub) == {ATTR_STATE: None}
