from enum import StrEnum


class CalculatedState(StrEnum):
    OFF = "off"
    PROGRAMMING = "programming"
    ERROR = "error"
    CLEANING = "cleaning"
    INIT = "init"
    HOLD_DELAY = "holddelay"
    HOLD_WEEKLY = "holdweekly"
    # HARD-11 — software-only sub-states masking the firmware echo gap.
    # Surfaced only on `sensor.<robot>_statut` (chip-side feedback), never
    # on `vacuum.activity` (closed HA enum).
    STARTING_PENDING = "startingpending"
    PAUSING_PENDING = "pausingpending"
