"""Compute the next scheduled cleaning cycle from the AWS shadow data.

Pure module — no Home Assistant or AWS imports — so the logic stays unit-testable
in isolation. The coordinator wraps it with the live data and the current time.

Inputs:
    weeklySettings (per-day with isInRepeatMode global gate),
    delay (one-shot, strict semantics: no roll-to-tomorrow),
    timeZoneName / timeZone (offset in minutes) from systemState,
    a tz-aware UTC datetime for "now".

Output: a dict with a tz-aware UTC datetime and attributes, or None.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .consts import (
    DATA_DELAY_START_TIME,
    DATA_SCHEDULE_CLEANING_MODE,
    DATA_SCHEDULE_IS_ENABLED,
    DATA_SCHEDULE_TIME,
    DATA_SCHEDULE_TIME_HOURS,
    DATA_SCHEDULE_TIME_MINUTES,
    DATA_WEEKLY_IS_IN_REPEAT_MODE,
)

ATTR_NSR_STATE = "state"
ATTR_NSR_CLEANING_MODE = "cleaning_mode"
ATTR_NSR_SOURCE = "source"
ATTR_NSR_DAY_OF_WEEK = "day_of_week"
ATTR_NSR_CYCLE_TIME_MINUTES = "cycle_time_minutes"

SOURCE_WEEKLY = "weekly"
SOURCE_DELAY = "delay"

_MODE_KEY = "mode"

_WEEKDAY_KEYS: list[tuple[str, int]] = [
    ("monday", 0),
    ("tuesday", 1),
    ("wednesday", 2),
    ("thursday", 3),
    ("friday", 4),
    ("saturday", 5),
    ("sunday", 6),
]


def _resolve_tz(time_zone_name: str | None, time_zone_offset_min: int | None) -> tzinfo:
    if time_zone_name:
        try:
            return ZoneInfo(time_zone_name)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    if isinstance(time_zone_offset_min, int):
        try:
            return timezone(timedelta(minutes=time_zone_offset_min))
        except (OverflowError, ValueError):
            pass
    return timezone.utc


def _extract_hh_mm(slot_time: object) -> tuple[int, int] | None:
    if not isinstance(slot_time, dict):
        return None
    hh = slot_time.get(DATA_SCHEDULE_TIME_HOURS)
    mm = slot_time.get(DATA_SCHEDULE_TIME_MINUTES)
    if not isinstance(hh, int) or not isinstance(mm, int):
        return None
    if isinstance(hh, bool) or isinstance(mm, bool):
        return None
    if not (0 <= hh < 24 and 0 <= mm < 60):
        return None
    return hh, mm


def _extract_mode(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    cleaning_mode = payload.get(DATA_SCHEDULE_CLEANING_MODE)
    if not isinstance(cleaning_mode, dict):
        return None
    mode = cleaning_mode.get(_MODE_KEY)
    if isinstance(mode, str):
        return mode
    return None


def _localize(target_date: date, hh: int, mm: int, tz: tzinfo) -> datetime:
    return datetime.combine(target_date, time(hh, mm), tzinfo=tz)


def _resolve_cycle_time_minutes(
    cleaning_modes: object, mode: str | None
) -> int | None:
    """Return ``cleaning_modes[mode]`` if it is a positive int, else ``None``.

    ``cleaningModes`` is the firmware's per-mode duration catalogue (e.g.
    ``{"all": 180, "stairs": 150, "pickup": 12, ...}``). When the scheduler
    fires, the firmware adopts ``cleaningModes[mode]`` as the cycle's
    duration; see ``docs/diag/2026-06-20_feat-04_cleaningmodes-source-confirmation/``.
    """
    if not isinstance(cleaning_modes, dict) or not isinstance(mode, str):
        return None
    value = cleaning_modes.get(mode)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value <= 0:
        return None
    return value


def compute_next_scheduled_run(
    weekly_settings: dict | None,
    delay: dict | None,
    time_zone_name: str | None,
    time_zone_offset_min: int | None,
    now_utc: datetime,
    cleaning_modes: dict | None = None,
) -> dict | None:
    tz = _resolve_tz(time_zone_name, time_zone_offset_min)

    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    now_local = now_utc.astimezone(tz)

    candidates: list[tuple[datetime, str | None, str | None, str]] = []

    if (
        isinstance(weekly_settings, dict)
        and weekly_settings.get(DATA_WEEKLY_IS_IN_REPEAT_MODE) is True
    ):
        for day_key, weekday_idx in _WEEKDAY_KEYS:
            slot = weekly_settings.get(day_key)
            if not isinstance(slot, dict):
                continue
            if slot.get(DATA_SCHEDULE_IS_ENABLED) is not True:
                continue
            hh_mm = _extract_hh_mm(slot.get(DATA_SCHEDULE_TIME))
            if hh_mm is None:
                continue
            hh, mm = hh_mm

            days_ahead = (weekday_idx - now_local.weekday()) % 7
            candidate_local = _localize(
                now_local.date() + timedelta(days=days_ahead), hh, mm, tz
            )
            if candidate_local <= now_local:
                candidate_local = _localize(
                    candidate_local.date() + timedelta(days=7), hh, mm, tz
                )

            candidates.append(
                (candidate_local, day_key, _extract_mode(slot), SOURCE_WEEKLY)
            )

    if isinstance(delay, dict) and delay.get(DATA_SCHEDULE_IS_ENABLED) is True:
        hh_mm = _extract_hh_mm(delay.get(DATA_DELAY_START_TIME))
        if hh_mm is not None:
            hh, mm = hh_mm
            candidate_local = _localize(now_local.date(), hh, mm, tz)
            if candidate_local > now_local:
                candidates.append(
                    (candidate_local, None, _extract_mode(delay), SOURCE_DELAY)
                )

    if not candidates:
        return None

    candidates.sort(key=lambda c: c[0])
    chosen_local, day_of_week, cleaning_mode, source = candidates[0]

    return {
        ATTR_NSR_STATE: chosen_local.astimezone(timezone.utc),
        ATTR_NSR_CLEANING_MODE: cleaning_mode,
        ATTR_NSR_SOURCE: source,
        ATTR_NSR_DAY_OF_WEEK: day_of_week,
        ATTR_NSR_CYCLE_TIME_MINUTES: _resolve_cycle_time_minutes(
            cleaning_modes, cleaning_mode
        ),
    }
