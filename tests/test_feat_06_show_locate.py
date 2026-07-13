"""Regression tests for FEAT-06 — vacuum ``Locate`` toggle.

Locked decisions (issue #142, 2026-07-13 17:38 clarification comment):

* Single source of truth = ``entry.options[CONF_SHOW_LOCATE]``.
* Default = ``True`` when the key is absent — preserves existing
  installations (acceptance criterion #1 + #2).
* The vacuum entity computes its ``_attr_supported_features`` mask at
  construction time. When ``show_locate`` is ``False`` the entity
  clears **only** ``VacuumEntityFeature.LOCATE``; every other bit
  from ``entity_description.features`` passes through unchanged.
* No coordinator state, no dedicated ``LocateFlowManager``. The
  options step handler manages persistence and reload directly.
* Toggling triggers **exactly one** config-entry reload; saving the
  same value is a no-op reload-wise (acceptance criteria #6 + #7).
* Shared constants (``VACUUM_FEATURES``) and the shared entity
  description (``entity_description.features``) are never mutated.
"""

from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest

from custom_components.mydolphin_plus.common.consts import (
    CONF_SHOW_LOCATE,
    CONF_VISIBLE_MODES,
    VACUUM_FEATURES,
)
from custom_components.mydolphin_plus.common.entity_descriptions import (
    ENTITY_DESCRIPTIONS,
    MyDolphinPlusLightEntityDescription,
    MyDolphinPlusNumberEntityDescription,
    MyDolphinPlusSelectEntityDescription,
    MyDolphinPlusVacuumEntityDescription,
)
from custom_components.mydolphin_plus.config_flow import DomainOptionsFlowHandler
from custom_components.mydolphin_plus.vacuum import MyDolphinPlusVacuumEntity
from homeassistant.components.vacuum import VacuumEntityFeature

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_coordinator_with_entry_options(entry_options: dict | None):
    """Return a minimal coordinator whose ``config_manager.entry.options``
    matches the caller's dict. The vacuum ``__init__`` only reads
    ``coordinator.config_manager.entry.options`` for FEAT-06, so the rest
    of the coordinator surface is a permissive ``MagicMock``."""
    coord = MagicMock()
    entry = MagicMock()
    entry.options = entry_options if entry_options is not None else {}
    entry.entry_id = "test-entry-id"
    config_manager = MagicMock()
    config_manager.entry = entry
    coord.config_manager = config_manager
    # base_entity uses these three during super().__init__; safe defaults.
    coord.get_device.return_value = {"identifiers": {("mydolphin_plus", "SN")}}
    return coord, entry


def _vacuum_description():
    """The one description with `platform=VACUUM`."""
    matches = [
        d for d in ENTITY_DESCRIPTIONS if isinstance(d, MyDolphinPlusVacuumEntityDescription)
    ]
    assert len(matches) == 1, "expected exactly one vacuum description"
    return matches[0]


def _make_entity(coord, description):
    """Build a ``MyDolphinPlusVacuumEntity`` with just enough hooks to
    reach the ``__init__`` FEAT-06 branch without exercising every
    base-class side effect."""
    with (
        patch(
            "custom_components.mydolphin_plus.common.base_entity.MyDolphinPlusBaseEntity.__init__",
            return_value=None,
        ),
        patch(
            "homeassistant.helpers.update_coordinator.CoordinatorEntity.__init__",
            return_value=None,
        ),
    ):
        entity = MyDolphinPlusVacuumEntity(description, coord)
    entity.entity_description = description
    entity.coordinator = coord
    return entity


# ---------------------------------------------------------------------------
# Group A — vacuum entity supported-feature mask computation
# ---------------------------------------------------------------------------


def test_supported_features_default_when_option_absent_includes_locate():
    """Acceptance criterion #1: Locate is enabled when the option is
    absent. Existing installations preserve current behaviour."""
    coord, _ = _make_coordinator_with_entry_options(entry_options={})
    entity = _make_entity(coord, _vacuum_description())
    assert entity._attr_supported_features & VacuumEntityFeature.LOCATE


def test_supported_features_default_true_option_includes_locate():
    """Explicit ``True`` in options behaves the same as absent."""
    coord, _ = _make_coordinator_with_entry_options(
        entry_options={CONF_SHOW_LOCATE: True}
    )
    entity = _make_entity(coord, _vacuum_description())
    assert entity._attr_supported_features & VacuumEntityFeature.LOCATE


def test_supported_features_disabled_clears_only_locate_bit():
    """Acceptance criteria #3 + #4: disabling removes ``LOCATE`` and
    nothing else."""
    coord, _ = _make_coordinator_with_entry_options(
        entry_options={CONF_SHOW_LOCATE: False}
    )
    entity = _make_entity(coord, _vacuum_description())
    mask = entity._attr_supported_features
    assert not (mask & VacuumEntityFeature.LOCATE), "LOCATE should be cleared"
    # Every other bit set in the description's max mask must remain set.
    other_bits = VACUUM_FEATURES & ~VacuumEntityFeature.LOCATE
    assert (mask & other_bits) == other_bits


def test_supported_features_disabled_preserves_each_named_bit():
    """Explicit bit-for-bit assertion — guards against off-by-one
    mistakes in the bit-mask math."""
    coord, _ = _make_coordinator_with_entry_options(
        entry_options={CONF_SHOW_LOCATE: False}
    )
    entity = _make_entity(coord, _vacuum_description())
    mask = entity._attr_supported_features
    for bit in (
        VacuumEntityFeature.STATE,
        VacuumEntityFeature.FAN_SPEED,
        VacuumEntityFeature.RETURN_HOME,
        VacuumEntityFeature.START,
        VacuumEntityFeature.PAUSE,
    ):
        assert mask & bit, f"expected {bit!r} to remain set"


def test_supported_features_re_enabled_restores_locate():
    """Acceptance criterion #5: re-enabling restores the Locate feature.
    Verified by rebuilding an entity with ``True`` after having built one
    with ``False`` — no shared state should leak."""
    coord_off, _ = _make_coordinator_with_entry_options(
        entry_options={CONF_SHOW_LOCATE: False}
    )
    entity_off = _make_entity(coord_off, _vacuum_description())
    assert not (entity_off._attr_supported_features & VacuumEntityFeature.LOCATE)

    coord_on, _ = _make_coordinator_with_entry_options(
        entry_options={CONF_SHOW_LOCATE: True}
    )
    entity_on = _make_entity(coord_on, _vacuum_description())
    assert entity_on._attr_supported_features & VacuumEntityFeature.LOCATE


# ---------------------------------------------------------------------------
# Group B — invariants: shared constants and descriptions never mutate
# ---------------------------------------------------------------------------


def test_vacuum_features_constant_is_never_mutated_by_disabled_entity():
    """Acceptance criterion (implicit): shared constants remain
    immutable. Snapshot the module-level ``VACUUM_FEATURES`` before and
    after constructing an entity with Locate disabled."""
    before = int(VACUUM_FEATURES)
    coord, _ = _make_coordinator_with_entry_options(
        entry_options={CONF_SHOW_LOCATE: False}
    )
    _ = _make_entity(coord, _vacuum_description())
    after = int(VACUUM_FEATURES)
    assert before == after


def test_vacuum_description_features_not_mutated_by_disabled_entity():
    """Same invariant for the shared entity description: the
    ``features`` attribute on the description must be identical before
    and after the FEAT-06 branch runs."""
    description = _vacuum_description()
    before = int(description.features)
    coord, _ = _make_coordinator_with_entry_options(
        entry_options={CONF_SHOW_LOCATE: False}
    )
    _ = _make_entity(coord, description)
    assert int(description.features) == before


# ---------------------------------------------------------------------------
# Group C — LED entities remain unaffected
# ---------------------------------------------------------------------------


def test_led_light_entity_description_still_present_and_unchanged():
    matches = [
        d for d in ENTITY_DESCRIPTIONS if isinstance(d, MyDolphinPlusLightEntityDescription)
    ]
    assert len(matches) == 1
    assert matches[0].key == "led"


def test_led_intensity_number_description_still_present():
    matches = [
        d
        for d in ENTITY_DESCRIPTIONS
        if isinstance(d, MyDolphinPlusNumberEntityDescription) and d.key == "led_intensity"
    ]
    assert len(matches) == 1


def test_led_mode_select_description_still_present():
    matches = [
        d
        for d in ENTITY_DESCRIPTIONS
        if isinstance(d, MyDolphinPlusSelectEntityDescription) and d.key == "led_mode"
    ]
    assert len(matches) == 1


# ---------------------------------------------------------------------------
# Group D — options-flow handler: menu + submit + reload-on-change
# ---------------------------------------------------------------------------


class _StubHandler(DomainOptionsFlowHandler):
    """Concrete subclass that exposes ``config_entry`` as a plain
    attribute, letting tests set it directly."""

    def __init__(self, entry):
        super().__init__()
        self._entry = entry
        # ``hass`` is normally injected by the flow manager after
        # instantiation; in unit tests we set it explicitly so
        # ``self.hass.async_create_task`` / ``self.hass.config_entries``
        # resolve to MagicMock spies.
        self.hass = MagicMock()
        # Track schedule_reload / async_create_entry / async_show_form
        # invocations without the full HA plumbing.
        self.create_entry_calls: list[dict] = []
        self.show_form_calls: list[dict] = []

    @property
    def config_entry(self):  # type: ignore[override]
        return self._entry

    def async_show_form(self, **kwargs):
        self.show_form_calls.append(kwargs)
        return {"type": "form", **kwargs}

    def async_create_entry(self, **kwargs):
        self.create_entry_calls.append(kwargs)
        return {"type": "create_entry", **kwargs}


@pytest.mark.asyncio
async def test_init_menu_lists_three_branches_including_locate():
    """Acceptance criterion (implicit): the new option is available in
    the integration options menu — alongside the existing two."""
    handler = _StubHandler(entry=MagicMock(options={}))
    handler.async_show_menu = MagicMock(return_value={"type": "menu"})

    await handler.async_step_init()

    handler.async_show_menu.assert_called_once()
    call = handler.async_show_menu.call_args
    assert set(call.kwargs["menu_options"]) == {"reauth", "preferences", "locate"}


@pytest.mark.asyncio
async def test_locate_step_no_input_renders_form_with_current_default_true():
    """Absent option → form default is ``True`` (preserves current
    behaviour)."""
    entry = MagicMock(options={})
    handler = _StubHandler(entry=entry)

    result = await handler.async_step_locate(None)

    assert result["type"] == "form"
    assert result["step_id"] == "locate"
    schema = result["data_schema"]
    parsed = schema({CONF_SHOW_LOCATE: True})
    assert parsed[CONF_SHOW_LOCATE] is True


@pytest.mark.asyncio
async def test_locate_step_no_input_renders_form_with_persisted_default_false():
    """A persisted ``False`` shows up as the form default on re-open."""
    entry = MagicMock(options={CONF_SHOW_LOCATE: False})
    handler = _StubHandler(entry=entry)

    _ = await handler.async_step_locate(None)

    # The vol.Schema built with `default=False` yields False when the
    # required key is omitted at parse time.
    schema = handler.show_form_calls[0]["data_schema"]
    parsed = schema({})  # required field but has a default
    assert parsed[CONF_SHOW_LOCATE] is False


@pytest.mark.asyncio
async def test_locate_step_submit_changed_value_schedules_exactly_one_reload():
    """Acceptance criterion #6: a changed save triggers exactly one
    config-entry reload targeting *this* entry.

    Counting ``async_create_task`` alone is not enough — any task would
    satisfy the counter. Assert the reload target explicitly.
    """
    entry = MagicMock(options={CONF_SHOW_LOCATE: True})
    entry.entry_id = "feat-06-changed-entry"
    handler = _StubHandler(entry=entry)

    await handler.async_step_locate({CONF_SHOW_LOCATE: False})

    assert handler.hass.async_create_task.call_count == 1
    handler.hass.config_entries.async_reload.assert_called_once_with(
        "feat-06-changed-entry"
    )


@pytest.mark.asyncio
async def test_locate_step_submit_unchanged_value_does_not_reload():
    """Acceptance criterion #7: saving the same value does not reload
    the entry."""
    entry = MagicMock(options={CONF_SHOW_LOCATE: False})
    handler = _StubHandler(entry=entry)

    await handler.async_step_locate({CONF_SHOW_LOCATE: False})

    handler.hass.async_create_task.assert_not_called()


@pytest.mark.asyncio
async def test_locate_step_submit_absent_persisted_treated_as_true_no_reload():
    """No prior option + submit True = same value = no reload."""
    entry = MagicMock(options={})
    handler = _StubHandler(entry=entry)

    await handler.async_step_locate({CONF_SHOW_LOCATE: True})

    handler.hass.async_create_task.assert_not_called()


@pytest.mark.asyncio
async def test_locate_step_submit_persists_new_value_merged_with_other_options():
    """Acceptance criterion #8: existing unrelated option keys (notably
    ``CONF_VISIBLE_MODES``) are preserved through the save."""
    entry = MagicMock(
        options={
            CONF_VISIBLE_MODES: ["all", "short"],
            CONF_SHOW_LOCATE: True,
        }
    )
    handler = _StubHandler(entry=entry)

    await handler.async_step_locate({CONF_SHOW_LOCATE: False})

    assert len(handler.create_entry_calls) == 1
    saved = handler.create_entry_calls[0]["data"]
    assert saved[CONF_SHOW_LOCATE] is False
    assert saved[CONF_VISIBLE_MODES] == ["all", "short"]


@pytest.mark.asyncio
async def test_locate_step_roundtrip_true_false_true():
    """Guard against stateful bugs in the handler by simulating three
    consecutive saves. Each toggle counts as one reload targeting
    *this* entry; unchanged save is a no-op."""
    entry = MagicMock(options={CONF_SHOW_LOCATE: True})
    entry.entry_id = "feat-06-roundtrip-entry"
    handler = _StubHandler(entry=entry)

    await handler.async_step_locate({CONF_SHOW_LOCATE: False})  # change
    entry.options = {CONF_SHOW_LOCATE: False}
    await handler.async_step_locate({CONF_SHOW_LOCATE: False})  # same
    await handler.async_step_locate({CONF_SHOW_LOCATE: True})  # change
    entry.options = {CONF_SHOW_LOCATE: True}
    await handler.async_step_locate({CONF_SHOW_LOCATE: True})  # same

    assert handler.hass.async_create_task.call_count == 2
    # Both reloads must target this entry, not any other.
    reload_mock = handler.hass.config_entries.async_reload
    assert reload_mock.call_args_list == [
        call("feat-06-roundtrip-entry"),
        call("feat-06-roundtrip-entry"),
    ]
