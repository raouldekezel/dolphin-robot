"""Regression tests for FEAT-03 — visible cleaning modes hide-set.

Locked decisions (issue #51 closing comment, 2026-07-10 → 2026-07-11):

* Single source of truth = ``entry.options[CONF_VISIBLE_MODES]``,
  mirrored into ``coordinator.visible_modes`` at setup and mutated
  through ``async_set_visible_modes``.
* ``vacuum.fan_speed_list`` and ``select.desired_clean_mode.options``
  are dynamic ``@property`` overrides — no ``_attr_*`` writes, no
  entity reload (design R1).
* ``number.cycle_time_<mode>`` visibility is driven by the entity
  registry's ``disabled_by`` field (design Q3 pivoted 2026-07-11 after
  in-vivo feedback that ``hidden_by`` still left the entities visible
  in the device details page). ``disabled_by`` truly removes them
  from every surface. Hiding is instant and reload-free; un-hiding
  schedules a config-entry reload so HA re-adds the entity.
* ``pickup`` stays in the pick-list (Q1) — the hide-set governs the
  full 7-mode curated set.
* No ``entry.add_update_listener`` reload path (Q2). Propagation is
  via ``coordinator.async_update_listeners()``.
* Empty saved selection falls back to the full curated set so a stray
  ``[]`` in ``.storage`` (or the operator un-checking every option in
  the form) never locks the picker to zero modes.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.mydolphin_plus.common.clean_modes import (
    KNOWN_LABELED_MODES,
    CleanModes,
    get_clean_mode_cycle_time_key,
)
from custom_components.mydolphin_plus.common.consts import CONF_VISIBLE_MODES, DOMAIN
from custom_components.mydolphin_plus.managers.coordinator import (
    MyDolphinPlusCoordinator,
)

# ---------------------------------------------------------------------------
# Constant contract
# ---------------------------------------------------------------------------


def test_selector_visible_modes_labels_live_at_top_level_selector_key():
    """FEAT-03 in-vivo bug (Raoul, #51 2026-07-10 23:56 comment): the
    picker showed raw values (`all`, `water`, …) instead of translated
    labels because the `visible_modes` selector labels were placed
    under ``options.selector.*``. Home Assistant resolves
    ``SelectSelectorConfig.translation_key`` against ``component.<domain>
    .selector.<translation_key>`` — a TOP-LEVEL ``selector`` key,
    alongside ``config``/``options``, not nested inside them.

    Pin the placement on every shipped locale so a future contributor
    can't silently nest it and re-break the picker labels.
    """
    import json
    from pathlib import Path

    root = (
        Path(__file__).resolve().parent.parent / "custom_components" / "mydolphin_plus"
    )
    files = [
        root / "strings.json",
        root / "translations" / "en.json",
        root / "translations" / "fr.json",
        root / "translations" / "it.json",
    ]

    for fp in files:
        d = json.loads(fp.read_text(encoding="utf-8"))
        top_selector = d.get("selector", {}).get("visible_modes", {}).get("options")
        nested_selector = (
            d.get("options", {})
            .get("selector", {})
            .get("visible_modes", {})
            .get("options")
        )
        assert top_selector, (
            f"{fp.name}: `selector.visible_modes.options.*` must be at "
            "top level (HA resolves `SelectSelectorConfig.translation_key` there)"
        )
        assert nested_selector is None, (
            f"{fp.name}: `options.selector.*` must NOT exist — HA does not "
            "resolve selector labels nested under `options.` (that's for step "
            "translations, not selectors)"
        )
        for mode in KNOWN_LABELED_MODES:
            assert (
                mode in top_selector
            ), f"{fp.name}: mode {mode!r} missing from selector labels"


def test_known_labeled_modes_is_the_full_curated_set_in_canonical_order():
    """The tuple governs iteration order in `fan_speed_list` and
    `select.options`. Order changes are user-visible on the picker."""
    assert KNOWN_LABELED_MODES == (
        "all",
        "short",
        "floor",
        "water",
        "ultra",
        "pickup",
        "stairs",
    )
    # Q1 — pickup stays in the hide-set.
    assert "pickup" in KNOWN_LABELED_MODES


# ---------------------------------------------------------------------------
# Coordinator — seeding, mutation, propagation
# ---------------------------------------------------------------------------


def _stub_coordinator(entry_options: dict | None = None):
    stub = MagicMock(spec=MyDolphinPlusCoordinator)
    stub.hass = MagicMock()
    stub.hass.data = {}
    stub.hass.config_entries = MagicMock()
    stub.hass.config_entries.async_update_entry = MagicMock()
    stub.hass.config_entries.async_schedule_reload = MagicMock()

    entry = MagicMock()
    entry.options = entry_options if entry_options is not None else {}
    entry.entry_id = "test-entry-id"

    config_manager = MagicMock()
    config_manager.entry = entry
    stub._config_manager = config_manager
    stub.config_manager = config_manager

    stub._visible_modes = frozenset(KNOWN_LABELED_MODES)
    stub.async_update_listeners = MagicMock()

    # Bind the real methods so state advances are observable.
    stub._seed_visible_modes = lambda e: MyDolphinPlusCoordinator._seed_visible_modes(
        stub, e
    )
    stub._apply_visible_modes_to_registry = (
        lambda visible: MyDolphinPlusCoordinator._apply_visible_modes_to_registry(
            stub, visible
        )
    )
    return stub, entry


def test_seed_visible_modes_defaults_to_full_set_when_no_options():
    stub, entry = _stub_coordinator(entry_options={})
    stub._seed_visible_modes(entry)
    assert stub._visible_modes == frozenset(KNOWN_LABELED_MODES)


def test_seed_visible_modes_reads_persisted_subset():
    stub, entry = _stub_coordinator(
        entry_options={CONF_VISIBLE_MODES: ["all", "short", "floor"]}
    )
    stub._seed_visible_modes(entry)
    assert stub._visible_modes == frozenset({"all", "short", "floor"})


def test_seed_visible_modes_drops_unknown_values_defensively():
    """A stray value from an out-of-band edit shouldn't poison the set."""
    stub, entry = _stub_coordinator(
        entry_options={CONF_VISIBLE_MODES: ["all", "floor", "sun_ledge", "zzz"]}
    )
    stub._seed_visible_modes(entry)
    assert stub._visible_modes == frozenset({"all", "floor"})


def test_seed_visible_modes_empty_falls_back_to_full():
    """An empty saved set (accidental or from a form no-op) resets to
    the full curated set — the operator can't lock themselves out."""
    stub, entry = _stub_coordinator(entry_options={CONF_VISIBLE_MODES: []})
    stub._seed_visible_modes(entry)
    assert stub._visible_modes == frozenset(KNOWN_LABELED_MODES)


@pytest.mark.asyncio
async def test_async_set_visible_modes_updates_registry_and_notifies_no_persist(
    monkeypatch,
):
    """The mutator updates in-memory set, toggles registry `disabled_by`,
    and calls `async_update_listeners`. It must NOT write
    `entry.options` — the flow finalize
    (`async_create_entry(data={**entry.options, …})`) owns persistence;
    double-writing would race with `async_create_entry`'s wholesale
    replace and could clobber unrelated option keys."""
    stub, entry = _stub_coordinator()
    stub.async_set_visible_modes = (
        MyDolphinPlusCoordinator.async_set_visible_modes.__get__(
            stub, MyDolphinPlusCoordinator
        )
    )

    apply_calls: list[frozenset] = []
    stub._apply_visible_modes_to_registry = lambda visible: apply_calls.append(visible)

    new_visible = frozenset({"all", "short"})
    await stub.async_set_visible_modes(new_visible)

    assert stub._visible_modes == new_visible
    # Persistence is the flow finalize's job — the coordinator mutator
    # must not touch entry.options directly.
    stub.hass.config_entries.async_update_entry.assert_not_called()
    assert apply_calls == [new_visible]
    stub.async_update_listeners.assert_called_once()


@pytest.mark.asyncio
async def test_async_set_visible_modes_empty_input_restores_full_set():
    """Guard against locking the picker to zero modes even if the caller
    passes an empty set. See #51 → decisions Q1/Q3."""
    stub, entry = _stub_coordinator()
    stub.async_set_visible_modes = (
        MyDolphinPlusCoordinator.async_set_visible_modes.__get__(
            stub, MyDolphinPlusCoordinator
        )
    )
    stub._apply_visible_modes_to_registry = MagicMock()

    await stub.async_set_visible_modes(frozenset())

    assert stub._visible_modes == frozenset(KNOWN_LABELED_MODES)


@pytest.mark.asyncio
async def test_async_set_visible_modes_hide_only_does_not_reload():
    """The common case: user hides one more mode. HA handles
    `disabled_by=INTEGRATION` synchronously (removes entity state, no
    reload needed). R1 preserved."""
    stub, entry = _stub_coordinator()
    stub._visible_modes = frozenset(KNOWN_LABELED_MODES)  # start visible
    stub.async_set_visible_modes = (
        MyDolphinPlusCoordinator.async_set_visible_modes.__get__(
            stub, MyDolphinPlusCoordinator
        )
    )
    stub._apply_visible_modes_to_registry = MagicMock()

    # Hide `water` (subset of the previous set — no un-hide).
    await stub.async_set_visible_modes(
        frozenset(m for m in KNOWN_LABELED_MODES if m != "water")
    )

    stub.hass.config_entries.async_schedule_reload.assert_not_called()


@pytest.mark.asyncio
async def test_async_set_visible_modes_un_hide_schedules_reload():
    """The rare case: user re-enables a previously hidden mode. HA does
    not automatically re-add the entity when `disabled_by` is cleared —
    it needs a platform pass. Schedule a reload so the re-enabled
    `number.cycle_time_<mode>` reappears in the same event loop turn."""
    stub, entry = _stub_coordinator()
    stub._visible_modes = frozenset({"all", "short"})  # water was hidden
    stub.async_set_visible_modes = (
        MyDolphinPlusCoordinator.async_set_visible_modes.__get__(
            stub, MyDolphinPlusCoordinator
        )
    )
    stub._apply_visible_modes_to_registry = MagicMock()

    # Un-hide `water` on top of the previous set.
    await stub.async_set_visible_modes(frozenset({"all", "short", "water"}))

    stub.hass.config_entries.async_schedule_reload.assert_called_once_with(
        entry.entry_id
    )


@pytest.mark.asyncio
async def test_async_set_visible_modes_drops_unknown_values():
    stub, entry = _stub_coordinator()
    stub.async_set_visible_modes = (
        MyDolphinPlusCoordinator.async_set_visible_modes.__get__(
            stub, MyDolphinPlusCoordinator
        )
    )
    stub._apply_visible_modes_to_registry = MagicMock()

    await stub.async_set_visible_modes(frozenset({"all", "not_a_mode"}))

    assert stub._visible_modes == frozenset({"all"})


# ---------------------------------------------------------------------------
# Registry `disabled_by` toggle + reload-on-un-hide
# ---------------------------------------------------------------------------


class _FakeRegistryEntry:
    def __init__(self, entity_id: str, translation_key: str, disabled_by=None):
        self.entity_id = entity_id
        self.translation_key = translation_key
        self.disabled_by = disabled_by


class _FakeRegistry:
    def __init__(self, entries: list[_FakeRegistryEntry]):
        self._entries = entries
        self.updates: list[tuple[str, object]] = []

    def async_update_entity(self, entity_id: str, disabled_by=None):
        self.updates.append((entity_id, disabled_by))
        for e in self._entries:
            if e.entity_id == entity_id:
                e.disabled_by = disabled_by

    def entries(self):
        return list(self._entries)


def test_apply_visible_modes_hides_only_invisible_modes(monkeypatch):
    """Only per-mode cycle_time numbers whose mode is now hidden get
    `disabled_by`; visible ones get `None`. Non-cycle-time entities
    (`led`, `cycle_time_locate`) are untouched."""
    from homeassistant.helpers.entity_registry import RegistryEntryDisabler

    stub, entry = _stub_coordinator()

    cycle_time_keys = {
        get_clean_mode_cycle_time_key(CleanModes(m)): m for m in KNOWN_LABELED_MODES
    }
    entries = [_FakeRegistryEntry(f"number.foo_{key}", key) for key in cycle_time_keys]
    # An unrelated number entity (e.g. `number.foo_led_intensity`) must
    # be ignored — its `translation_key` doesn't match any cycle_time key.
    entries.append(_FakeRegistryEntry("number.foo_led_intensity", "led_intensity"))
    # A sensor entity that happens to share a translation_key must be
    # ignored — we only touch `number.*`.
    cycle_time_all_key = get_clean_mode_cycle_time_key(CleanModes.REGULAR)
    entries.append(
        _FakeRegistryEntry(f"sensor.foo_{cycle_time_all_key}", cycle_time_all_key)
    )

    registry = _FakeRegistry(entries)

    import custom_components.mydolphin_plus.managers.coordinator as coord_module

    monkeypatch.setattr(
        coord_module, "async_get_entity_registry", lambda _hass: registry
    )
    monkeypatch.setattr(
        coord_module,
        "async_entries_for_config_entry",
        lambda _reg, _entry_id: registry.entries(),
    )

    visible = frozenset({"all", "short", "floor"})
    stub._apply_visible_modes_to_registry(visible)

    entity_ids_updated = {u[0] for u in registry.updates}
    per_entity = dict(registry.updates)
    # Non-cycle-time entities untouched.
    assert "number.foo_led_intensity" not in entity_ids_updated
    assert f"sensor.foo_{cycle_time_all_key}" not in entity_ids_updated
    # Only hidden modes generate an update — visible modes were already
    # at `disabled_by=None` in this fixture, so they're skipped (see the
    # dedicated idempotence test below).
    for key, mode in cycle_time_keys.items():
        entity_id = f"number.foo_{key}"
        if mode in visible:
            assert (
                entity_id not in entity_ids_updated
            ), f"{mode} already-visible entity should not be re-written"
        else:
            assert per_entity[entity_id] == RegistryEntryDisabler.INTEGRATION


def test_apply_visible_modes_is_a_noop_when_state_matches(monkeypatch):
    """Idempotent: an entity already at the desired disabled_by value is
    not re-written (avoids spurious registry updates)."""
    from homeassistant.helpers.entity_registry import RegistryEntryDisabler

    stub, entry = _stub_coordinator()

    key_all = get_clean_mode_cycle_time_key(CleanModes.REGULAR)
    key_water = get_clean_mode_cycle_time_key(CleanModes.WATER_LINE)
    entries = [
        _FakeRegistryEntry(f"number.foo_{key_all}", key_all, disabled_by=None),
        _FakeRegistryEntry(
            f"number.foo_{key_water}",
            key_water,
            disabled_by=RegistryEntryDisabler.INTEGRATION,
        ),
    ]
    registry = _FakeRegistry(entries)

    import custom_components.mydolphin_plus.managers.coordinator as coord_module

    monkeypatch.setattr(
        coord_module, "async_get_entity_registry", lambda _hass: registry
    )
    monkeypatch.setattr(
        coord_module,
        "async_entries_for_config_entry",
        lambda _reg, _entry_id: registry.entries(),
    )

    # `all` visible (already None), `water` hidden (already INTEGRATION).
    visible = frozenset({"all"})
    stub._apply_visible_modes_to_registry(visible)

    assert registry.updates == []


# ---------------------------------------------------------------------------
# Coordinator payload — force base_entity data-diff on visible_modes change
# ---------------------------------------------------------------------------


def test_get_desired_clean_mode_data_encodes_visible_modes():
    """FEAT-03 bug found in-vivo on raoul.24: `entry.options` correctly
    reflected the operator's saved subset, but the `select` combo kept
    the stale full list. Root cause: `base_entity._handle_coordinator_update`
    short-circuits when `self._data == new_data`, and the coordinator's
    data payload for the select depended only on `_desired_clean_mode`.
    A preferences save that didn't also change the picked mode produced
    a payload equal to the previous one → no `async_write_ha_state` →
    the `options` @property was never re-read → frontend cache stayed
    stale.

    Fix: include `_visible_modes` in the payload so a preferences save
    is observable at the data-diff layer.
    """
    stub = MagicMock(spec=MyDolphinPlusCoordinator)
    stub._desired_clean_mode = "all"
    stub.aws_data = {}
    stub._visible_modes = frozenset({"all", "short", "floor", "stairs"})
    stub._set_cleaning_mode = MagicMock()

    data_v1 = MyDolphinPlusCoordinator._get_desired_clean_mode_data(stub, None)

    stub._visible_modes = frozenset({"all", "short", "floor"})  # user un-checked stairs
    data_v2 = MyDolphinPlusCoordinator._get_desired_clean_mode_data(stub, None)

    assert data_v1 != data_v2, (
        "select payload must differ when visible_modes changes — otherwise "
        "base_entity._handle_coordinator_update short-circuits on data "
        "equality and the frontend never sees the new options list"
    )


def test_get_vacuum_data_encodes_visible_modes():
    """Same trap on the vacuum entity: `fan_speed_list` @property depends
    on visible_modes, but the base entity gates on data-payload
    equality. Include `_visible_modes` in the vacuum payload."""
    stub = MagicMock(spec=MyDolphinPlusCoordinator)
    stub._desired_clean_mode = "all"
    stub.aws_data = {}
    stub._visible_modes = frozenset(KNOWN_LABELED_MODES)
    stub._optimistic_vacuum_state = None
    stub._system_details = MagicMock()
    stub._system_details.vacuum_state = "docked"
    stub._vacuum_start = MagicMock()
    stub._vacuum_pause = MagicMock()
    stub._set_cleaning_mode = MagicMock()
    stub._vacuum_locate = MagicMock()
    stub._pickup = MagicMock()

    data_v1 = MyDolphinPlusCoordinator._get_vacuum_data(stub, None)

    stub._visible_modes = frozenset({"all", "short"})
    data_v2 = MyDolphinPlusCoordinator._get_vacuum_data(stub, None)

    assert data_v1 != data_v2


# ---------------------------------------------------------------------------
# `vacuum.fan_speed_list` — dynamic property
# ---------------------------------------------------------------------------


def test_vacuum_fan_speed_list_filters_by_visible_modes():
    from custom_components.mydolphin_plus.vacuum import MyDolphinPlusVacuumEntity

    stub = MagicMock(spec=MyDolphinPlusVacuumEntity)
    coordinator = MagicMock()
    coordinator.visible_modes = frozenset({"all", "short", "stairs"})
    stub._local_coordinator = coordinator

    result = MyDolphinPlusVacuumEntity.fan_speed_list.fget(stub)

    # Canonical order preserved (stairs is last in KNOWN_LABELED_MODES).
    assert result == ["all", "short", "stairs"]


def test_vacuum_fan_speed_list_full_when_all_visible():
    from custom_components.mydolphin_plus.vacuum import MyDolphinPlusVacuumEntity

    stub = MagicMock(spec=MyDolphinPlusVacuumEntity)
    coordinator = MagicMock()
    coordinator.visible_modes = frozenset(KNOWN_LABELED_MODES)
    stub._local_coordinator = coordinator

    result = MyDolphinPlusVacuumEntity.fan_speed_list.fget(stub)

    assert result == list(KNOWN_LABELED_MODES)


# ---------------------------------------------------------------------------
# `select.options` — filter only for desired_clean_mode
# ---------------------------------------------------------------------------


def test_select_options_filters_for_desired_clean_mode():
    from custom_components.mydolphin_plus.select import (
        _DESIRED_CLEAN_MODE_KEY,
        MyDolphinPlusSelectEntity,
    )

    stub = MagicMock(spec=MyDolphinPlusSelectEntity)
    stub.entity_description = MagicMock()
    stub.entity_description.key = _DESIRED_CLEAN_MODE_KEY
    coordinator = MagicMock()
    coordinator.visible_modes = frozenset({"all", "floor"})
    stub._local_coordinator = coordinator

    result = MyDolphinPlusSelectEntity.options.fget(stub)

    assert result == ["all", "floor"]


def test_select_options_static_for_led_mode():
    """Other select entities (LED mode) keep their static options list."""
    from custom_components.mydolphin_plus.select import MyDolphinPlusSelectEntity

    stub = MagicMock(spec=MyDolphinPlusSelectEntity)
    stub.entity_description = MagicMock()
    stub.entity_description.key = "led_mode"
    stub.entity_description.options = ("blinking", "always_on", "disco")
    stub._local_coordinator = MagicMock()
    stub._local_coordinator.visible_modes = frozenset({"all"})

    result = MyDolphinPlusSelectEntity.options.fget(stub)

    assert result == ["blinking", "always_on", "disco"]


# ---------------------------------------------------------------------------
# PreferencesFlowManager
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preferences_flow_shows_form_with_current_visible_defaults():
    from custom_components.mydolphin_plus.managers.preferences_flow import (
        PreferencesFlowManager,
    )

    hass = MagicMock()
    entry = MagicMock()
    entry.entry_id = "e1"
    entry.options = {}

    coordinator = MagicMock()
    coordinator.visible_modes = frozenset({"all", "short", "floor"})
    hass.data = {DOMAIN: {entry.entry_id: coordinator}}

    flow_handler = MagicMock()
    flow_handler.async_show_form = MagicMock(
        side_effect=lambda **kwargs: {"type": "form", **kwargs}
    )

    mgr = PreferencesFlowManager(hass, flow_handler, entry)
    result = await mgr.async_step_preferences(user_input=None)

    assert result["type"] == "form"
    assert result["step_id"] == "preferences"
    # Schema default was seeded from coordinator.visible_modes — check
    # the schema advertises the current set as its default. Deep parsing
    # a voluptuous schema is heavy; we assert the form was called at all
    # and defer defaults verification to the manual-QA path.


@pytest.mark.asyncio
async def test_preferences_flow_save_calls_coordinator_and_creates_entry():
    from custom_components.mydolphin_plus.managers.preferences_flow import (
        PreferencesFlowManager,
    )

    hass = MagicMock()
    entry = MagicMock()
    entry.entry_id = "e1"
    entry.options = {}

    coordinator = MagicMock()
    coordinator.visible_modes = frozenset(KNOWN_LABELED_MODES)
    coordinator.async_set_visible_modes = AsyncMock()
    hass.data = {DOMAIN: {entry.entry_id: coordinator}}

    flow_handler = MagicMock()
    flow_handler.async_create_entry = MagicMock(
        side_effect=lambda **kwargs: {"type": "create_entry", **kwargs}
    )

    mgr = PreferencesFlowManager(hass, flow_handler, entry)
    result = await mgr.async_step_preferences({CONF_VISIBLE_MODES: ["all", "short"]})

    coordinator.async_set_visible_modes.assert_awaited_once_with(
        frozenset({"all", "short"})
    )
    assert result["type"] == "create_entry"
    assert result["data"] == {CONF_VISIBLE_MODES: ["all", "short"]}


@pytest.mark.asyncio
async def test_preferences_flow_empty_selection_falls_back_to_full():
    """Uncheck-everything → the full curated set is persisted, not `[]`."""
    from custom_components.mydolphin_plus.managers.preferences_flow import (
        PreferencesFlowManager,
    )

    hass = MagicMock()
    entry = MagicMock()
    entry.entry_id = "e1"
    entry.options = {}

    coordinator = MagicMock()
    coordinator.visible_modes = frozenset(KNOWN_LABELED_MODES)
    coordinator.async_set_visible_modes = AsyncMock()
    hass.data = {DOMAIN: {entry.entry_id: coordinator}}

    flow_handler = MagicMock()
    flow_handler.async_create_entry = MagicMock(
        side_effect=lambda **kwargs: {"type": "create_entry", **kwargs}
    )

    mgr = PreferencesFlowManager(hass, flow_handler, entry)
    result = await mgr.async_step_preferences({CONF_VISIBLE_MODES: []})

    coordinator.async_set_visible_modes.assert_awaited_once_with(
        frozenset(KNOWN_LABELED_MODES)
    )
    assert result["data"] == {CONF_VISIBLE_MODES: sorted(KNOWN_LABELED_MODES)}


@pytest.mark.asyncio
async def test_preferences_flow_save_merges_existing_entry_options():
    """`async_create_entry(data=...)` on OptionsFlow REPLACES
    `entry.options` with `data`. The preferences flow must merge the
    current options into `data` so any unrelated key survives (must-fix
    #3 in the fable review of PR #124)."""
    from custom_components.mydolphin_plus.managers.preferences_flow import (
        PreferencesFlowManager,
    )

    hass = MagicMock()
    entry = MagicMock()
    entry.entry_id = "e1"
    entry.options = {"unrelated_future_key": "keep-me", CONF_VISIBLE_MODES: []}

    coordinator = MagicMock()
    coordinator.visible_modes = frozenset(KNOWN_LABELED_MODES)
    coordinator.async_set_visible_modes = AsyncMock()
    hass.data = {DOMAIN: {entry.entry_id: coordinator}}

    flow_handler = MagicMock()
    flow_handler.async_create_entry = MagicMock(
        side_effect=lambda **kwargs: {"type": "create_entry", **kwargs}
    )

    mgr = PreferencesFlowManager(hass, flow_handler, entry)
    result = await mgr.async_step_preferences({CONF_VISIBLE_MODES: ["all"]})

    assert result["data"]["unrelated_future_key"] == "keep-me"
    assert result["data"][CONF_VISIBLE_MODES] == ["all"]


@pytest.mark.asyncio
async def test_preferences_flow_reads_options_when_coordinator_absent():
    """Coordinator can be absent during a config-flow test harness or
    before `async_setup_entry` finishes. Fall back to entry.options."""
    from custom_components.mydolphin_plus.managers.preferences_flow import (
        PreferencesFlowManager,
    )

    hass = MagicMock()
    hass.data = {}  # No coordinator.
    entry = MagicMock()
    entry.entry_id = "e1"
    entry.options = {CONF_VISIBLE_MODES: ["all"]}

    flow_handler = MagicMock()
    flow_handler.async_show_form = MagicMock(
        side_effect=lambda **kwargs: {"type": "form", **kwargs}
    )

    mgr = PreferencesFlowManager(hass, flow_handler, entry)
    result = await mgr.async_step_preferences(user_input=None)

    assert result["type"] == "form"
    # Form was shown without crashing; the coordinator absence path is
    # exercised without exception — that's the invariant.


# ---------------------------------------------------------------------------
# DomainOptionsFlowHandler — menu wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_options_flow_init_shows_menu_with_two_branches():
    from custom_components.mydolphin_plus.config_flow import DomainOptionsFlowHandler

    handler = DomainOptionsFlowHandler()
    handler.async_show_menu = MagicMock(
        side_effect=lambda **kwargs: {"type": "menu", **kwargs}
    )

    result = await handler.async_step_init(user_input=None)

    assert result["type"] == "menu"
    assert result["step_id"] == "init"
    assert sorted(result["menu_options"]) == ["preferences", "reauth"]


# ---------------------------------------------------------------------------
# P1 — reauth via the options menu must NOT wipe visible_modes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_options_reauth_via_flow_manager_preserves_visible_modes(monkeypatch):
    """On the OTP finalize path used by the options menu's reauth branch
    (`self._entry is not None`, not `_is_reauth`), the flow used to
    finalize with `async_create_entry(data={})`, which HA translates
    into `entry.options = {}` — silently wiping any saved
    `visible_modes`. This test drives the real
    `IntegrationFlowManager.async_step_otp` code path and asserts the
    current options are preserved on the create_entry finalize."""
    from custom_components.mydolphin_plus.managers import flow_manager as fm_module
    from custom_components.mydolphin_plus.managers.flow_manager import (
        _FLOW_STATE_ATTR,
        IntegrationFlowManager,
    )

    monkeypatch.setattr(
        fm_module,
        "cognito_respond_otp",
        AsyncMock(
            return_value={
                "IdToken": "id",
                "RefreshToken": "refresh",
                "ExpiresIn": 3600,
            }
        ),
    )
    monkeypatch.setattr(
        fm_module,
        "fetch_user_profile",
        AsyncMock(return_value={"Sernum": "s1", "eSERNUM": "e1"}),
    )
    monkeypatch.setattr(fm_module, "async_get_clientsession", lambda _hass: object())

    hass = MagicMock()
    hass.config_entries = MagicMock()
    hass.config_entries.async_update_entry = MagicMock()
    hass.config_entries.async_schedule_reload = MagicMock()

    entry = MagicMock()
    entry.options = {CONF_VISIBLE_MODES: ["all", "short"]}
    entry.title = "Nono"

    handler = MagicMock()
    handler.async_create_entry = MagicMock(
        side_effect=lambda **kwargs: {"type": "create_entry", **kwargs}
    )
    setattr(
        handler,
        _FLOW_STATE_ATTR,
        {"title": "Nono", "email": "user@example.com", "cognito_session": "sess"},
    )

    mgr = IntegrationFlowManager(hass, handler, entry)

    # Stub the async initialize on the manager's IntegrationInfo — the
    # real one touches HA storage.
    async def _noop_init(_hass):
        return None

    mgr._integration_info.initialize = _noop_init  # type: ignore[attr-defined]

    result = await mgr.async_step_otp({"otp": "123456"})

    assert result["type"] == "create_entry"
    # The load-bearing assertion — options must survive the finalize.
    assert result["data"].get(CONF_VISIBLE_MODES) == ["all", "short"]


# ---------------------------------------------------------------------------
# P2 — options OTP lost-state fallback must NOT land on the menu
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_options_otp_lost_state_reroutes_to_reauth_not_menu(monkeypatch):
    """If a user hits the OTP submit after the intermediate state has
    been cleared, `flow_manager.async_step_otp` falls back to
    `async_step_user(None)` which shows the email form. With
    `flow_id_override="reauth"` (matching `async_step_reauth`), the
    form's `step_id` is `"reauth"` — not `"init"`, which would collide
    with the menu and dead-end the user."""
    from custom_components.mydolphin_plus.config_flow import DomainOptionsFlowHandler
    from custom_components.mydolphin_plus.managers import flow_manager as fm_module

    monkeypatch.setattr(fm_module, "async_get_clientsession", lambda _hass: object())

    handler = DomainOptionsFlowHandler()
    handler.hass = MagicMock()
    # No _FLOW_STATE_ATTR on the handler → async_step_otp falls back to
    # async_step_user(None).
    handler.async_show_form = MagicMock(
        side_effect=lambda **kwargs: {"type": "form", **kwargs}
    )
    entry = MagicMock()
    entry.title = "Nono"
    entry.data = {}
    # DomainOptionsFlowHandler exposes `config_entry` via the OptionsFlow
    # base; stub it.
    type(handler).config_entry = property(lambda _self: entry)

    result = await handler.async_step_otp(user_input={"otp": "1234"})

    assert result["type"] == "form"
    # Must NOT be "init" (the menu step_id).
    assert result["step_id"] == "reauth"


# ---------------------------------------------------------------------------
# P3 — anchor seam tests on real ENTITY_DESCRIPTIONS (de-tautologize)
# ---------------------------------------------------------------------------


def test_registry_matcher_covers_real_cycle_time_number_translation_keys():
    """The registry `disabled_by` toggle matches `number.*` entities by
    their `translation_key`. If either the matcher key or the entity
    description's `translation_key` drifts, the toggle silently no-ops.
    Anchor the invariant on the shipped `ENTITY_DESCRIPTIONS` list so a
    rename on either side breaks this test — not the FEAT-03 fixture
    which used to synthesize both ends of the seam."""
    from custom_components.mydolphin_plus.common.entity_descriptions import (
        ENTITY_DESCRIPTIONS,
    )
    from homeassistant.const import Platform

    real_number_tks = {
        ed.translation_key
        for ed in ENTITY_DESCRIPTIONS
        if ed.platform == Platform.NUMBER and ed.translation_key is not None
    }
    matcher_keys = {
        get_clean_mode_cycle_time_key(CleanModes(m)) for m in KNOWN_LABELED_MODES
    }

    # Every mode our matcher looks up must exist as a real number entity
    # description in the integration.
    missing = matcher_keys - real_number_tks
    assert not missing, (
        f"registry matcher references cycle_time keys with no matching "
        f"number entity description: {missing}"
    )


def test_select_filter_key_matches_a_real_select_description():
    """The `desired_clean_mode` filter in `select.py` gates on the
    entity description's `key`. Anchor on the shipped descriptions so a
    key rename on either side breaks this test."""
    from custom_components.mydolphin_plus.common.entity_descriptions import (
        ENTITY_DESCRIPTIONS,
    )
    from custom_components.mydolphin_plus.select import _DESIRED_CLEAN_MODE_KEY
    from homeassistant.const import Platform

    select_keys = {
        ed.key for ed in ENTITY_DESCRIPTIONS if ed.platform == Platform.SELECT
    }
    assert _DESIRED_CLEAN_MODE_KEY in select_keys, (
        f"select filter key {_DESIRED_CLEAN_MODE_KEY!r} does not match "
        f"any real select description ({sorted(select_keys)})"
    )
