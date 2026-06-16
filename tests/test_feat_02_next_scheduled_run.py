"""Tests for the FEAT-02 next_scheduled_run computation.

Pure unit tests against `compute_next_scheduled_run` — no Home Assistant runtime
needed. Covers every case from the FEAT-02 spec review:

- weekly-only, delay-only, both-set min-pick, nothing scheduled
- `weeklySettings.isInRepeatMode == False` short-circuits the weekly branch
- delay strict semantics: passed-today is dropped, not rolled to tomorrow
- invalid timezone name falls back to numeric `systemState.timeZone` offset
- returned `state` is always a tz-aware datetime
- rollover: now just past today's slot → advances to the next valid occurrence
- DST spring-forward smoke test
"""

from __future__ import annotations

from datetime import datetime, timezone

from custom_components.mydolphin_plus.common.next_scheduled_run import (
    ATTR_NSR_CLEANING_MODE,
    ATTR_NSR_DAY_OF_WEEK,
    ATTR_NSR_SOURCE,
    ATTR_NSR_STATE,
    SOURCE_DELAY,
    SOURCE_WEEKLY,
    compute_next_scheduled_run,
)

# --- Helpers ---------------------------------------------------------------


def _weekly_slot(
    enabled: bool, hours: int = 11, minutes: int = 0, mode: str = "all"
) -> dict:
    return {
        "isEnabled": enabled,
        "time": {"hours": hours, "minutes": minutes},
        "cleaningMode": {"mode": mode},
    }


def _full_weekly(
    *,
    is_in_repeat_mode: bool = True,
    enabled_days: list[str] | None = None,
    hours: int = 11,
    minutes: int = 0,
    mode: str = "all",
) -> dict:
    days = [
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
    ]
    enabled_set = set(days) if enabled_days is None else set(enabled_days)
    out: dict = {"isInRepeatMode": is_in_repeat_mode, "triggeredBy": 1}
    for day in days:
        out[day] = _weekly_slot(
            enabled=day in enabled_set, hours=hours, minutes=minutes, mode=mode
        )
    return out


def _delay(enabled: bool, hours: int = 14, minutes: int = 0, mode: str = "all") -> dict:
    return {
        "isEnabled": enabled,
        "triggeredBy": 0 if enabled else 255,
        "startTime": {"hours": hours, "minutes": minutes},
        "cleaningMode": {"mode": mode},
    }


# --- Tests ----------------------------------------------------------------


def test_weekly_only_picks_next_enabled_day():
    """Wednesday-only weekly slot, now is Monday → returns Wednesday 11:00 local."""
    # Monday 2026-06-15 09:00 UTC = 11:00 Europe/Brussels (CEST, +02:00)
    now_utc = datetime(2026, 6, 15, 9, 0, tzinfo=timezone.utc)
    weekly = _full_weekly(
        enabled_days=["wednesday"], hours=11, minutes=0, mode="stairs"
    )

    result = compute_next_scheduled_run(weekly, None, "Europe/Brussels", 120, now_utc)

    assert result is not None
    assert result[ATTR_NSR_SOURCE] == SOURCE_WEEKLY
    assert result[ATTR_NSR_DAY_OF_WEEK] == "wednesday"
    assert result[ATTR_NSR_CLEANING_MODE] == "stairs"
    # Wednesday 2026-06-17 11:00 Brussels = 09:00 UTC
    assert result[ATTR_NSR_STATE] == datetime(2026, 6, 17, 9, 0, tzinfo=timezone.utc)


def test_delay_only_picks_today_if_future():
    """Delay armed for today 14:00 local, now is 13:00 local → returns 14:00."""
    # 2026-06-15 11:00 UTC = 13:00 Europe/Brussels (CEST)
    now_utc = datetime(2026, 6, 15, 11, 0, tzinfo=timezone.utc)
    delay = _delay(enabled=True, hours=14, minutes=0, mode="floor")

    result = compute_next_scheduled_run(None, delay, "Europe/Brussels", 120, now_utc)

    assert result is not None
    assert result[ATTR_NSR_SOURCE] == SOURCE_DELAY
    assert result[ATTR_NSR_DAY_OF_WEEK] is None  # always present, None for delay
    assert result[ATTR_NSR_CLEANING_MODE] == "floor"
    assert result[ATTR_NSR_STATE] == datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)


def test_both_set_min_pick_delay_wins():
    """Weekly Wed 11:00 + delay today 14:00 (today is Mon) → delay 14:00 today is earlier."""
    now_utc = datetime(2026, 6, 15, 11, 0, tzinfo=timezone.utc)  # Mon 13:00 local
    weekly = _full_weekly(enabled_days=["wednesday"], hours=11, minutes=0)
    delay = _delay(enabled=True, hours=14, minutes=0)

    result = compute_next_scheduled_run(weekly, delay, "Europe/Brussels", 120, now_utc)

    assert result[ATTR_NSR_SOURCE] == SOURCE_DELAY


def test_both_set_min_pick_weekly_wins():
    """Weekly today 11:30 (still future) + delay today 14:00 → weekly wins."""
    # Monday 2026-06-15 09:00 UTC = 11:00 Europe/Brussels
    now_utc = datetime(2026, 6, 15, 9, 0, tzinfo=timezone.utc)
    weekly = _full_weekly(enabled_days=["monday"], hours=11, minutes=30)
    delay = _delay(enabled=True, hours=14, minutes=0)

    result = compute_next_scheduled_run(weekly, delay, "Europe/Brussels", 120, now_utc)

    assert result[ATTR_NSR_SOURCE] == SOURCE_WEEKLY
    assert result[ATTR_NSR_DAY_OF_WEEK] == "monday"


def test_nothing_scheduled_returns_none():
    """Empty weekly + delay disabled → None."""
    now_utc = datetime(2026, 6, 15, 9, 0, tzinfo=timezone.utc)
    weekly = _full_weekly(enabled_days=[])  # 7 disabled slots, but isInRepeatMode true
    delay = _delay(enabled=False)

    result = compute_next_scheduled_run(weekly, delay, "Europe/Brussels", 120, now_utc)

    assert result is None


def test_is_in_repeat_mode_false_short_circuits_weekly():
    """All 7 days enabled but isInRepeatMode == False → weekly branch ignored."""
    now_utc = datetime(2026, 6, 15, 9, 0, tzinfo=timezone.utc)
    weekly = _full_weekly(is_in_repeat_mode=False)  # all 7 days enabled

    result = compute_next_scheduled_run(weekly, None, "Europe/Brussels", 120, now_utc)

    assert result is None


def test_delay_passed_today_is_dropped_not_rolled():
    """Delay armed at 10:00 local, now is 11:00 local → drop (strict semantics)."""
    # Mon 2026-06-15 09:00 UTC = 11:00 Brussels
    now_utc = datetime(2026, 6, 15, 9, 0, tzinfo=timezone.utc)
    delay = _delay(enabled=True, hours=10, minutes=0)

    result = compute_next_scheduled_run(None, delay, "Europe/Brussels", 120, now_utc)

    assert result is None  # not rolled to tomorrow


def test_invalid_tz_name_falls_back_to_numeric_offset():
    """Invalid tz name → use timeZone offset (minutes)."""
    now_utc = datetime(2026, 6, 15, 9, 0, tzinfo=timezone.utc)
    weekly = _full_weekly(enabled_days=["monday"], hours=11, minutes=30)

    # +120 min = same effective offset as Brussels CEST → 11:30 local = 09:30 UTC
    result = compute_next_scheduled_run(weekly, None, "Not/A/Real/Zone", 120, now_utc)

    assert result is not None
    assert result[ATTR_NSR_STATE] == datetime(2026, 6, 15, 9, 30, tzinfo=timezone.utc)


def test_missing_tz_falls_back_to_utc():
    """No tz name, no offset → UTC."""
    now_utc = datetime(2026, 6, 15, 9, 0, tzinfo=timezone.utc)
    weekly = _full_weekly(enabled_days=["monday"], hours=11, minutes=30)

    result = compute_next_scheduled_run(weekly, None, None, None, now_utc)

    # 11:30 UTC same day
    assert result[ATTR_NSR_STATE] == datetime(2026, 6, 15, 11, 30, tzinfo=timezone.utc)


def test_state_is_tz_aware():
    """The returned datetime is always tz-aware (HA core requirement)."""
    now_utc = datetime(2026, 6, 15, 9, 0, tzinfo=timezone.utc)
    weekly = _full_weekly(enabled_days=["wednesday"])

    result = compute_next_scheduled_run(weekly, None, "Europe/Brussels", 120, now_utc)

    assert result[ATTR_NSR_STATE].tzinfo is not None


def test_rollover_when_now_just_past_todays_slot():
    """Now is 1 minute past today's slot → advance 7 days, not return None."""
    # Monday 2026-06-15 09:01 UTC = 11:01 Europe/Brussels, slot was 11:00
    now_utc = datetime(2026, 6, 15, 9, 1, tzinfo=timezone.utc)
    weekly = _full_weekly(enabled_days=["monday"], hours=11, minutes=0)

    result = compute_next_scheduled_run(weekly, None, "Europe/Brussels", 120, now_utc)

    assert result is not None
    # Next Monday 2026-06-22 11:00 Brussels = 09:00 UTC
    assert result[ATTR_NSR_STATE] == datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc)


def test_dst_smoke_spring_forward_brussels():
    """DST spring forward 2026: 2026-03-29 02:00→03:00 in Brussels.

    A slot scheduled at 02:30 on the spring-forward day does not exist locally;
    `datetime.combine(..., tzinfo=ZoneInfo)` resolves it consistently. Asserts the
    function does not crash and returns a tz-aware result. Exact wall-clock
    semantics are zoneinfo's call; we just want determinism, not surprises.
    """
    # Saturday 2026-03-28 12:00 UTC = 13:00 Brussels CET
    now_utc = datetime(2026, 3, 28, 12, 0, tzinfo=timezone.utc)
    weekly = _full_weekly(enabled_days=["sunday"], hours=2, minutes=30)

    result = compute_next_scheduled_run(weekly, None, "Europe/Brussels", 60, now_utc)

    assert result is not None
    assert result[ATTR_NSR_STATE].tzinfo is not None
    # Should land Sunday 2026-03-29 in UTC, regardless of how the missing hour
    # is resolved (zoneinfo on Python 3.12 produces a consistent value).
    assert result[ATTR_NSR_STATE].date() in {
        datetime(2026, 3, 29).date(),
        datetime(2026, 3, 28).date(),
    }


# --- Edge case sentinels ---------------------------------------------------


def test_default_time_part_sentinel_is_rejected():
    """hours=255 / minutes=255 sentinel from disabled-firmware → slot ignored."""
    now_utc = datetime(2026, 6, 15, 9, 0, tzinfo=timezone.utc)
    weekly = _full_weekly(enabled_days=["monday"], hours=255, minutes=255)

    result = compute_next_scheduled_run(weekly, None, "Europe/Brussels", 120, now_utc)

    assert result is None


def test_delay_sentinel_is_rejected():
    """delay.startTime hours=255 → drop even if isEnabled is True."""
    now_utc = datetime(2026, 6, 15, 9, 0, tzinfo=timezone.utc)
    delay = _delay(enabled=True, hours=255, minutes=255)

    result = compute_next_scheduled_run(None, delay, "Europe/Brussels", 120, now_utc)

    assert result is None


def test_handles_missing_or_malformed_inputs():
    """None inputs, missing keys, wrong types: never raise."""
    now_utc = datetime(2026, 6, 15, 9, 0, tzinfo=timezone.utc)

    assert compute_next_scheduled_run(None, None, None, None, now_utc) is None
    assert compute_next_scheduled_run({}, {}, "", None, now_utc) is None
    assert (
        compute_next_scheduled_run(
            {"isInRepeatMode": True, "monday": "not-a-dict"},
            {"isEnabled": True, "startTime": "wrong"},
            "Europe/Brussels",
            120,
            now_utc,
        )
        is None
    )


def test_naive_now_is_treated_as_utc():
    """A naive datetime passed as now_utc is interpreted as UTC, no crash."""
    now_naive = datetime(2026, 6, 15, 9, 0)  # no tzinfo
    weekly = _full_weekly(enabled_days=["monday"], hours=11, minutes=30)

    result = compute_next_scheduled_run(weekly, None, "Europe/Brussels", 120, now_naive)

    assert result is not None
    assert result[ATTR_NSR_STATE] == datetime(2026, 6, 15, 9, 30, tzinfo=timezone.utc)
