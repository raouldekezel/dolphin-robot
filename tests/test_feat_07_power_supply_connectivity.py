"""Behavioral tests for FEAT-07 (issue #145).

The RAW-mirror `binary_sensor.{robot}_power_supply` reflects
`reported.isConnected.connected` verbatim (tri-state True / False /
None) plus a `last_seen` attribute from
`reported.LastReceiveData.timestamp`. The design decision — no
integration-side debounce — is pinned by the transitions test at the
bottom: any future "helpful" filter that collapses back-to-back
`False`→`True` flips would flip that test red.

Coverage (per CHORE-02, behavior not source):

- `SystemDetails._get_updated_data` tri-state coercion: True / False /
  section absent / non-bool payload → True / False / None / None.
- `SystemDetails.pws_connected` property surfaces the parsed value.
- Coordinator getter maps the tri-state to `is_on` and lifts
  `LastReceiveData.timestamp` into a UTC `datetime` on `last_seen`
  (absent / 0 / bad type → None).
- Raw-mirroring pin: consecutive flips seconds apart come out as-is.
- Entity description invariants: device class CONNECTIVITY, entity
  category matches the `aws_broker` sibling, translation key
  `power_supply`.
- Translation coverage: FR/EN/IT/strings.json all carry the entity
  under `binary_sensor.power_supply` (FEAT-06 F3 lesson).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
from types import SimpleNamespace

from custom_components.mydolphin_plus.common.consts import (
    ATTR_ATTRIBUTES,
    ATTR_IS_ON,
    ATTR_LAST_SEEN,
    ATTR_PWS_CONNECTED,
    DATA_IS_CONNECTED_CONNECTED,
    DATA_KEY_AWS_BROKER,
    DATA_KEY_POWER_SUPPLY,
    DATA_LAST_RECEIVE_DATA_TIMESTAMP,
    DATA_SECTION_IS_CONNECTED,
    DATA_SECTION_LAST_RECEIVE_DATA,
)
from custom_components.mydolphin_plus.models.system_details import SystemDetails


COMPONENT_ROOT = Path(
    __file__
).resolve().parent.parent / "custom_components" / "mydolphin_plus"


# ---------------------------------------------------------------------------
# Helpers — minimal stubs that bypass __init__ for the units under test.
# ---------------------------------------------------------------------------


def _make_coordinator(aws_data: dict, pws_connected: bool | None):
    """Bypass `MyDolphinPlusCoordinator.__init__` (needs hass + clients)
    and wire only what `_get_power_supply_data` reads: a shadow-carried
    aws_data dict and a `_system_details` whose `.pws_connected`
    returns the wanted tri-state."""
    from custom_components.mydolphin_plus.managers.coordinator import (
        MyDolphinPlusCoordinator,
    )

    coord = object.__new__(MyDolphinPlusCoordinator)
    sd = SystemDetails()
    # Seed the property via the exposed data dict rather than by
    # calling `update()` — keeps the test focused on the getter, not
    # on the parse chain (that is exercised separately below).
    sd._data = {ATTR_PWS_CONNECTED: pws_connected}
    coord._system_details = sd
    coord._aws_client = SimpleNamespace(data=aws_data)
    return coord


# ---------------------------------------------------------------------------
# Parse — SystemDetails tri-state coercion.
# ---------------------------------------------------------------------------


def test_parse_true_yields_true():
    sd = SystemDetails()
    sd.update({DATA_SECTION_IS_CONNECTED: {DATA_IS_CONNECTED_CONNECTED: True}})
    assert sd.pws_connected is True


def test_parse_false_yields_false():
    sd = SystemDetails()
    sd.update({DATA_SECTION_IS_CONNECTED: {DATA_IS_CONNECTED_CONNECTED: False}})
    assert sd.pws_connected is False


def test_parse_missing_section_yields_none():
    sd = SystemDetails()
    sd.update({})  # no isConnected section at all
    assert sd.pws_connected is None


def test_parse_non_bool_payload_yields_none():
    """Strict bool coercion — a stringly-typed `"false"` must not
    collapse to True the way `bool("false")` would. Defensive against
    LWT / lifecycle payload variations across firmware generations."""
    sd = SystemDetails()
    sd.update(
        {DATA_SECTION_IS_CONNECTED: {DATA_IS_CONNECTED_CONNECTED: "false"}}
    )
    assert sd.pws_connected is None


def test_parse_int_1_is_not_true():
    """`1` would satisfy `bool(x) is True` in Python but must not — the
    wire spec is a native JSON bool."""
    sd = SystemDetails()
    sd.update({DATA_SECTION_IS_CONNECTED: {DATA_IS_CONNECTED_CONNECTED: 1}})
    assert sd.pws_connected is None


# ---------------------------------------------------------------------------
# Getter — coordinator maps tri-state → is_on + lifts last_seen.
# ---------------------------------------------------------------------------


def test_getter_true_maps_to_is_on_true():
    coord = _make_coordinator(aws_data={}, pws_connected=True)
    result = coord._get_power_supply_data(None)
    assert result[ATTR_IS_ON] is True


def test_getter_false_maps_to_is_on_false():
    coord = _make_coordinator(aws_data={}, pws_connected=False)
    result = coord._get_power_supply_data(None)
    assert result[ATTR_IS_ON] is False


def test_getter_none_maps_to_is_on_none():
    """Unknown state until the first shadow — BinarySensor renders
    this as `unknown`, which composes with the BUG-16 availability
    latch without special-casing."""
    coord = _make_coordinator(aws_data={}, pws_connected=None)
    result = coord._get_power_supply_data(None)
    assert result[ATTR_IS_ON] is None


def test_getter_last_seen_present_when_timestamp_positive():
    coord = _make_coordinator(
        aws_data={
            DATA_SECTION_LAST_RECEIVE_DATA: {
                DATA_LAST_RECEIVE_DATA_TIMESTAMP: 1_720_000_000
            }
        },
        pws_connected=True,
    )
    result = coord._get_power_supply_data(None)
    last_seen = result[ATTR_ATTRIBUTES][ATTR_LAST_SEEN]
    assert isinstance(last_seen, datetime)
    assert last_seen.tzinfo is not None
    # Convert to UTC and back — the value must round-trip via the
    # epoch it came from.
    assert last_seen == datetime.fromtimestamp(1_720_000_000, tz=timezone.utc)


def test_getter_last_seen_absent_when_section_missing():
    coord = _make_coordinator(aws_data={}, pws_connected=True)
    result = coord._get_power_supply_data(None)
    assert result[ATTR_ATTRIBUTES][ATTR_LAST_SEEN] is None


def test_getter_last_seen_absent_when_timestamp_zero():
    """`0` is the firmware sentinel for "never received" — must not
    surface a 1970-01-01 datetime that would confuse the recorder."""
    coord = _make_coordinator(
        aws_data={
            DATA_SECTION_LAST_RECEIVE_DATA: {DATA_LAST_RECEIVE_DATA_TIMESTAMP: 0}
        },
        pws_connected=True,
    )
    result = coord._get_power_supply_data(None)
    assert result[ATTR_ATTRIBUTES][ATTR_LAST_SEEN] is None


def test_getter_last_seen_absent_when_timestamp_wrong_type():
    coord = _make_coordinator(
        aws_data={
            DATA_SECTION_LAST_RECEIVE_DATA: {
                DATA_LAST_RECEIVE_DATA_TIMESTAMP: "not-a-number"
            }
        },
        pws_connected=True,
    )
    result = coord._get_power_supply_data(None)
    assert result[ATTR_ATTRIBUTES][ATTR_LAST_SEEN] is None


# ---------------------------------------------------------------------------
# Raw-mirroring pin — the design decision, encoded as a test.
# ---------------------------------------------------------------------------


def test_consecutive_flips_are_reported_verbatim():
    """Two `False`→`True` flips ~20 s apart (observed session flaps on
    2026-06-27) must both surface. Encodes the FEAT-07 no-debounce
    decision so a future "helpful" filter cannot land silently."""
    sd = SystemDetails()

    sd.update({DATA_SECTION_IS_CONNECTED: {DATA_IS_CONNECTED_CONNECTED: True}})
    assert sd.pws_connected is True

    sd.update({DATA_SECTION_IS_CONNECTED: {DATA_IS_CONNECTED_CONNECTED: False}})
    assert sd.pws_connected is False

    sd.update({DATA_SECTION_IS_CONNECTED: {DATA_IS_CONNECTED_CONNECTED: True}})
    assert sd.pws_connected is True

    sd.update({DATA_SECTION_IS_CONNECTED: {DATA_IS_CONNECTED_CONNECTED: False}})
    assert sd.pws_connected is False


# ---------------------------------------------------------------------------
# Entity description invariants — mirrored on aws_broker.
# ---------------------------------------------------------------------------


def _entity_descriptions_by_key():
    from homeassistant.components.binary_sensor import BinarySensorDeviceClass  # noqa: F401
    from homeassistant.util import slugify

    from custom_components.mydolphin_plus.common.entity_descriptions import (
        ENTITY_DESCRIPTIONS,
    )

    return {ed.key: ed for ed in ENTITY_DESCRIPTIONS}


def test_entity_description_exists_with_connectivity_device_class():
    from homeassistant.components.binary_sensor import BinarySensorDeviceClass
    from homeassistant.util import slugify

    descs = _entity_descriptions_by_key()
    ed = descs[slugify(DATA_KEY_POWER_SUPPLY)]

    assert ed.device_class is BinarySensorDeviceClass.CONNECTIVITY
    assert ed.translation_key == slugify(DATA_KEY_POWER_SUPPLY)


def test_entity_description_category_matches_aws_broker():
    """Both connectivity entities land in the same block of the device
    page — no divergence in category."""
    from homeassistant.util import slugify

    descs = _entity_descriptions_by_key()
    aws_broker = descs[slugify(DATA_KEY_AWS_BROKER)]
    power_supply = descs[slugify(DATA_KEY_POWER_SUPPLY)]

    assert power_supply.entity_category == aws_broker.entity_category


def test_entity_description_platform_is_binary_sensor():
    from homeassistant.const import Platform
    from homeassistant.util import slugify

    descs = _entity_descriptions_by_key()
    ed = descs[slugify(DATA_KEY_POWER_SUPPLY)]

    assert ed.platform == Platform.BINARY_SENSOR


# ---------------------------------------------------------------------------
# Translation coverage — strings.json + en/fr/it.
# ---------------------------------------------------------------------------


def _load_translation(name: str) -> dict:
    if name == "strings.json":
        path = COMPONENT_ROOT / "strings.json"
    else:
        path = COMPONENT_ROOT / "translations" / name
    return json.loads(path.read_text(encoding="utf-8"))


def test_strings_json_has_power_supply_entry():
    data = _load_translation("strings.json")
    entry = data["entity"]["binary_sensor"]["power_supply"]
    assert entry["name"] == "Power Supply"


def test_en_translation_has_power_supply_entry():
    data = _load_translation("en.json")
    entry = data["entity"]["binary_sensor"]["power_supply"]
    assert entry["name"] == "Power Supply"


def test_fr_translation_has_power_supply_entry():
    """FR uses « Alimentation » — consistent with the sibling
    « État de l'alimentation » (power_supply_status) and « Erreur
    d'alimentation » (power_supply_error)."""
    data = _load_translation("fr.json")
    entry = data["entity"]["binary_sensor"]["power_supply"]
    assert entry["name"] == "Alimentation"


def test_it_translation_has_power_supply_entry():
    """FEAT-06 F3 lesson: `it.json` is covered in this repo."""
    data = _load_translation("it.json")
    entry = data["entity"]["binary_sensor"]["power_supply"]
    assert entry["name"] == "Alimentatore"
