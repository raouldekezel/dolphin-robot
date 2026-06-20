"""Tests for the FEAT-04 cycle-time extension to ``compute_next_scheduled_run``.

Pure unit tests against the extended ``compute_next_scheduled_run`` —
no Home Assistant runtime needed. Covers:

- mode present in ``cleaningModes`` → returns the minute count
- mode absent from ``cleaningModes`` → ``None`` (no fallback)
- ``cleaningModes`` section entirely absent from shadow → ``None``
- non-positive / non-int / bool values are rejected → ``None``
- when there is no next slot at all, the result dict itself is ``None``
"""

from __future__ import annotations

from datetime import datetime, timezone

from custom_components.mydolphin_plus.common.next_scheduled_run import (
    ATTR_NSR_CLEANING_MODE,
    ATTR_NSR_CYCLE_TIME_MINUTES,
    compute_next_scheduled_run,
)


def _full_weekly(*, mode: str = "all", hours: int = 11, minutes: int = 0) -> dict:
    days = [
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
    ]
    out: dict = {"isInRepeatMode": True, "triggeredBy": 1}
    for day in days:
        out[day] = {
            "isEnabled": True,
            "time": {"hours": hours, "minutes": minutes},
            "cleaningMode": {"mode": mode},
        }
    return out


# Monday 2026-06-15 09:00 UTC = 11:00 Brussels (CEST); next slot is today at 11:00
# local since we're exactly on the boundary — rollover advances to the next
# occurrence, which for a daily-enabled weekly is tomorrow 11:00 Brussels.
_NOW = datetime(2026, 6, 15, 9, 0, tzinfo=timezone.utc)


# --- Mode present in cleaningModes -----------------------------------------


def test_cycle_time_resolved_from_cleaning_modes():
    """Mode in cleaningModes → returns the minute count for that mode."""
    weekly = _full_weekly(mode="all")
    cleaning_modes = {
        "all": 180,
        "short": 60,
        "stairs": 150,
        "pickup": 12,
    }

    result = compute_next_scheduled_run(
        weekly, None, "Europe/Brussels", 120, _NOW, cleaning_modes
    )

    assert result is not None
    assert result[ATTR_NSR_CLEANING_MODE] == "all"
    assert result[ATTR_NSR_CYCLE_TIME_MINUTES] == 180


def test_cycle_time_resolved_for_stairs():
    """Independent confirmation on the canary mode `stairs` (firmware
    decouples it from the long-mode group — `stairs` carries its own value
    inside `cleaningModes`)."""
    weekly = _full_weekly(mode="stairs")
    cleaning_modes = {"all": 180, "stairs": 150}

    result = compute_next_scheduled_run(
        weekly, None, "Europe/Brussels", 120, _NOW, cleaning_modes
    )

    assert result is not None
    assert result[ATTR_NSR_CLEANING_MODE] == "stairs"
    assert result[ATTR_NSR_CYCLE_TIME_MINUTES] == 150


# --- Missing or malformed → None (no fallback) -----------------------------


def test_cycle_time_none_when_mode_missing_from_catalogue():
    """Mode absent from cleaningModes → None (no fallback to a hardcoded
    default)."""
    weekly = _full_weekly(mode="custom")
    cleaning_modes = {"all": 180}  # custom missing

    result = compute_next_scheduled_run(
        weekly, None, "Europe/Brussels", 120, _NOW, cleaning_modes
    )

    assert result is not None
    assert result[ATTR_NSR_CLEANING_MODE] == "custom"
    assert result[ATTR_NSR_CYCLE_TIME_MINUTES] is None


def test_cycle_time_none_when_cleaning_modes_section_absent():
    """Cold-start before first AWS message: cleaningModes dict not passed →
    None."""
    weekly = _full_weekly(mode="all")

    result = compute_next_scheduled_run(
        weekly, None, "Europe/Brussels", 120, _NOW
    )

    assert result is not None
    assert result[ATTR_NSR_CYCLE_TIME_MINUTES] is None


def test_cycle_time_none_when_cleaning_modes_is_not_a_dict():
    """Defensive: a non-dict cleaningModes payload is treated as absent."""
    weekly = _full_weekly(mode="all")

    result = compute_next_scheduled_run(
        weekly, None, "Europe/Brussels", 120, _NOW, "not-a-dict"  # type: ignore[arg-type]
    )

    assert result is not None
    assert result[ATTR_NSR_CYCLE_TIME_MINUTES] is None


def test_cycle_time_none_for_non_positive_values():
    """Zero and negative durations are rejected — the sensor reports
    unavailable rather than emit a nonsense reading."""
    weekly = _full_weekly(mode="all")

    for bad_value in (0, -1, -120):
        result = compute_next_scheduled_run(
            weekly,
            None,
            "Europe/Brussels",
            120,
            _NOW,
            {"all": bad_value},
        )
        assert result is not None
        assert result[ATTR_NSR_CYCLE_TIME_MINUTES] is None, bad_value


def test_cycle_time_none_for_non_int_values():
    """Strings, dicts, floats are not durations the firmware would emit, so
    treat them as malformed shadow data → None."""
    weekly = _full_weekly(mode="all")

    for bad_value in ("180", 180.0, {"minutes": 180}, [180], None):
        result = compute_next_scheduled_run(
            weekly,
            None,
            "Europe/Brussels",
            120,
            _NOW,
            {"all": bad_value},
        )
        assert result is not None
        assert result[ATTR_NSR_CYCLE_TIME_MINUTES] is None, bad_value


def test_cycle_time_none_when_value_is_a_bool():
    """`bool` is a subclass of `int` in Python — explicitly reject it."""
    weekly = _full_weekly(mode="all")

    result = compute_next_scheduled_run(
        weekly,
        None,
        "Europe/Brussels",
        120,
        _NOW,
        {"all": True},
    )
    assert result is not None
    assert result[ATTR_NSR_CYCLE_TIME_MINUTES] is None


# --- Other axes still honored ---------------------------------------------


def test_no_slot_returns_none_result_overall():
    """When `isInRepeatMode=False` and no delay, the whole result is None —
    cleaning_modes is irrelevant in this case."""
    weekly = _full_weekly(mode="all")
    weekly["isInRepeatMode"] = False  # short-circuit

    result = compute_next_scheduled_run(
        weekly, None, "Europe/Brussels", 120, _NOW, {"all": 180}
    )

    assert result is None
