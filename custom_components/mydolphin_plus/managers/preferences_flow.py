"""Preferences step of the FEAT-03 options flow.

Shows a `SelectSelector(multiple=True)` over the curated cleaning modes.
On save, pushes the new set through `coordinator.async_set_visible_modes`
(which handles the entity registry `hidden_by` toggle and the
coordinator listeners so `fan_speed_list` / `select.options` re-evaluate
without an entity reload) then persists the same set into
`entry.options` via `async_create_entry`.
"""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowHandler
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from ..common.clean_modes import KNOWN_LABELED_MODES
from ..common.consts import CONF_VISIBLE_MODES, DOMAIN

_LOGGER = logging.getLogger(__name__)


class PreferencesFlowManager:
    """Encapsulates the preferences step of the options flow.

    Held separate from ``IntegrationFlowManager`` (which owns the OTP
    reauth path) because the two flows share no state and mixing them
    would blur which step_ids collide with which method names on the
    ``OptionsFlow`` handler.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        flow_handler: FlowHandler,
        entry: ConfigEntry,
    ):
        self._hass = hass
        self._flow_handler = flow_handler
        self._entry = entry

    async def async_step_preferences(self, user_input: dict | None = None):
        current_visible = self._current_visible_modes()

        if user_input is None:
            return self._show_form(defaults={"visible_modes": sorted(current_visible)})

        new_visible = frozenset(user_input.get(CONF_VISIBLE_MODES) or [])
        # Empty selection falls back to the full curated set — an empty
        # picker locks the operator out of every cleaning mode, so refuse
        # it defensively. The form's help text should discourage this.
        if not new_visible:
            new_visible = frozenset(KNOWN_LABELED_MODES)

        coordinator = self._get_coordinator()
        if coordinator is not None:
            await coordinator.async_set_visible_modes(new_visible)

        # `async_create_entry` on an OptionsFlow REPLACES `entry.options`
        # with `data`. Merge the current options into the write so any
        # unrelated key (present or future) survives. Also self-documents
        # that `visible_modes` is one option among possibly many.
        return self._flow_handler.async_create_entry(
            title="",
            data={**self._entry.options, CONF_VISIBLE_MODES: sorted(new_visible)},
        )

    def _current_visible_modes(self) -> frozenset[str]:
        """Read the current visible set from the coordinator when live,
        or fall back to whatever is in ``entry.options`` (or the full
        curated set)."""
        coordinator = self._get_coordinator()
        if coordinator is not None:
            return coordinator.visible_modes
        stored = self._entry.options.get(CONF_VISIBLE_MODES)
        if stored is None:
            return frozenset(KNOWN_LABELED_MODES)
        clean = {m for m in stored if m in KNOWN_LABELED_MODES}
        return frozenset(clean) if clean else frozenset(KNOWN_LABELED_MODES)

    def _get_coordinator(self):
        domain_data = self._hass.data.get(DOMAIN) or {}
        return domain_data.get(self._entry.entry_id)

    def _show_form(self, defaults: dict):
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_VISIBLE_MODES,
                    default=defaults.get(CONF_VISIBLE_MODES) or [],
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=list(KNOWN_LABELED_MODES),
                        multiple=True,
                        mode=SelectSelectorMode.LIST,
                        translation_key=CONF_VISIBLE_MODES,
                    )
                ),
            }
        )
        return self._flow_handler.async_show_form(
            step_id="preferences",
            data_schema=schema,
        )
