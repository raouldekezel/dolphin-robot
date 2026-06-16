"""Regression tests for MAP-06.

The pre-fix labels for the three lifecycle-state sensors were either
firmware-enum echoes (FR ``Initialisé``/``Analyse en cours`` for ``init`` and
``scanning``) or broken IT machine-translations (``Colpa``, ``Scansione``,
``Finita Finito``, ``Disconnessa Disconnesso``, ``SU``, ``Idle (settimanale)``).
On the S2000 the ``scanning`` state spans the full active cycle (no real
analysis happens — ``navMode.isSmart=false``), so the past-participle ``init``
labels and the analysis-implying ``scanning`` labels were misleading on every
locale they shipped on.

The tests below cover three things:

1. Every member of :class:`RobotState`, :class:`CalculatedState` and
   :class:`PowerSupplyState` has a translation for every shipped file, so a
   future enum addition trips the suite instead of silently rendering the raw
   key in the UI.
2. The exact labels arbitrated in issue #60 are present (parametrised on the
   four shipped files so the spec is enforced uniformly).
3. None of the pre-fix labels survive anywhere in the four files — a single
   blanket check guards against a half-applied fix.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from custom_components.mydolphin_plus.common.calculated_state import CalculatedState
from custom_components.mydolphin_plus.common.power_supply_state import PowerSupplyState
from custom_components.mydolphin_plus.common.robot_state import RobotState

COMPONENT_ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "mydolphin_plus"

TRANSLATION_FILES = (
    COMPONENT_ROOT / "strings.json",
    COMPONENT_ROOT / "translations" / "en.json",
    COMPONENT_ROOT / "translations" / "fr.json",
    COMPONENT_ROOT / "translations" / "it.json",
)

SENSOR_ENUMS = {
    "robot_status": RobotState,
    "status": CalculatedState,
    "power_supply_status": PowerSupplyState,
}


def _load(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def _sensor_state(data: dict, sensor: str) -> dict:
    return data.get("entity", {}).get("sensor", {}).get(sensor, {}).get("state", {})


@pytest.mark.parametrize("path", TRANSLATION_FILES, ids=lambda p: p.name)
@pytest.mark.parametrize(
    ("sensor", "enum"),
    [(name, cls) for name, cls in SENSOR_ENUMS.items()],
    ids=lambda v: v if isinstance(v, str) else v.__name__,
)
def test_every_enum_member_has_translation(path: Path, sensor: str, enum) -> None:
    """Each enum value (lower-cased, the form HA persists) must have a label."""
    states = _sensor_state(_load(path), sensor)
    for member in enum:
        key = member.value.lower()
        assert key in states, f"{path.name}:{sensor}.state missing {key!r}"
        label = states[key]
        assert isinstance(label, str) and label.strip(), (
            f"{path.name}:{sensor}.state.{key} must be a non-empty string"
        )


EXPECTED_LABELS = {
    "strings.json": {
        "robot_status": {"init": "Starting", "scanning": "Cleaning"},
        "status": {"error": "Error"},
    },
    "en.json": {
        "robot_status": {"init": "Starting", "scanning": "Cleaning"},
        "status": {"error": "Error"},
    },
    "fr.json": {
        "robot_status": {
            "fault": "Anomalie",
            "init": "Démarrage",
            "scanning": "Nettoyage",
        },
        "status": {
            "cleaning": "Nettoyage",
            "error": "Erreur",
            "holdweekly": "En attente (hebdomadaire)",
        },
        "power_supply_status": {"holdweekly": "En attente (hebdomadaire)"},
    },
    "it.json": {
        "robot_status": {
            "fault": "Guasto",
            "finished": "Completato",
            "init": "Avvio",
            "notconnected": "Disconnesso",
            "scanning": "Pulizia",
        },
        "status": {
            "error": "Errore",
            "holdweekly": "Inattivo (settimanale)",
            "on": "Acceso",
        },
        "power_supply_status": {
            "holdweekly": "Inattivo (settimanale)",
            "on": "Acceso",
        },
    },
}


@pytest.mark.parametrize("path", TRANSLATION_FILES, ids=lambda p: p.name)
def test_arbitrated_labels_applied(path: Path) -> None:
    """The exact labels signed off in issue #60 must be in place."""
    data = _load(path)
    expected = EXPECTED_LABELS[path.name]
    for sensor, mapping in expected.items():
        states = _sensor_state(data, sensor)
        for key, want in mapping.items():
            got = states.get(key)
            assert got == want, (
                f"{path.name}:{sensor}.state.{key} = {got!r}, expected {want!r}"
            )


FORBIDDEN_LABELS = {
    # Past-participle / firmware-echo labels for an ongoing phase (all locales).
    "Initialized",
    "Initialisé",
    "Inizializzato",
    # ``scanning`` implied analysis the S2000 does not perform.
    "Scanning",
    "Analyse en cours",
    "Scansione",
    # FR ``fault`` was ambiguous.
    "Défaut",
    # FR ``cleaning`` carried a "en cours" suffix the spec dropped for brevity.
    "Nettoyage en cours",
    # FR ``holdweekly`` reused "programmation" which collides with the
    # ``programming`` state — the spec aligns on "hebdomadaire".
    "En attente (programmation)",
    # IT garbage (machine-translation breakage).
    "Colpa",
    "Finita Finito",
    "Disconnessa Disconnesso",
    "SU",
    "Idle (settimanale)",
}


@pytest.mark.parametrize("path", TRANSLATION_FILES, ids=lambda p: p.name)
def test_no_pre_fix_label_survives(path: Path) -> None:
    """None of the pre-fix labels may appear in the three lifecycle sensors.

    Scope is intentionally the three sensors MAP-06 touches; collateral IT
    breakage on ``filter_status`` / ``robot_error`` / ``power_supply_error`` is
    out of scope per the issue arbitration and left for a follow-up.
    """
    data = _load(path)
    leaks: list[str] = []
    for sensor in SENSOR_ENUMS:
        states = _sensor_state(data, sensor)
        for label in states.values():
            if label in FORBIDDEN_LABELS:
                leaks.append(f"{sensor}.state:{label!r}")
    assert not leaks, f"{path.name} still contains pre-fix label(s): {sorted(leaks)}"
