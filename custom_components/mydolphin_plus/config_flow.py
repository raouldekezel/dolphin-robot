"""Config flow to configure."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntry
from homeassistant.core import callback

from .common.consts import DOMAIN
from .managers.flow_manager import IntegrationFlowManager
from .managers.preferences_flow import PreferencesFlowManager

_LOGGER = logging.getLogger(__name__)


@config_entries.HANDLERS.register(DOMAIN)
class DomainFlowHandler(config_entries.ConfigFlow):
    """Handle a domain config flow."""

    VERSION = 1

    def __init__(self):
        super().__init__()

    @staticmethod
    @callback
    def async_get_options_flow(_config_entry: ConfigEntry):
        """Get the options flow for this handler."""
        return DomainOptionsFlowHandler()

    async def async_step_user(self, user_input=None):
        """Handle a flow start (email step)."""
        flow_manager = IntegrationFlowManager(self.hass, self)
        return await flow_manager.async_step_user(user_input)

    async def async_step_otp(self, user_input=None):
        """Handle the OTP confirmation step."""
        flow_manager = IntegrationFlowManager(
            self.hass,
            self,
            source=self.source,
        )
        return await flow_manager.async_step_otp(user_input)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]):
        """Start reauthentication flow linked to current config entry."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        """Confirm reauthentication and continue with OTP login flow."""
        if user_input is None:
            return self.async_show_form(
                step_id="reauth_confirm",
                data_schema=vol.Schema({}),
            )

        entry = self._get_reauth_entry()
        flow_manager = IntegrationFlowManager(
            self.hass,
            self,
            entry=entry,
            source=SOURCE_REAUTH,
        )
        return await flow_manager.async_step_user(None)


class DomainOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle domain options.

    FEAT-03 — the entry point is a menu with two branches:

    - ``reauth`` — the existing OTP flow (unchanged behaviour, just
      routed through a dedicated step_id so its form submissions don't
      collide with the menu's ``async_step_init``).
    - ``preferences`` — the new visible-modes picker.
    """

    def __init__(self):
        """Initialize domain options flow."""
        super().__init__()

    async def async_step_init(self, user_input=None):
        """Show the top-level options menu."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["reauth", "preferences"],
        )

    async def async_step_reauth(self, user_input=None):
        """OTP reauth path, kept behaviourally identical to the pre-FEAT-03
        options flow. ``flow_id_override="reauth"`` avoids collision
        with ``async_step_init`` (the menu) on form submissions."""
        flow_manager = IntegrationFlowManager(
            self.hass,
            self,
            self.config_entry,
            flow_id_override="reauth",
        )
        return await flow_manager.async_step_user(user_input)

    async def async_step_otp(self, user_input=None):
        # Same `flow_id_override="reauth"` as `async_step_reauth` so a
        # lost-state fallback (`async_step_user(None)` inside
        # `flow_manager.async_step_otp`) re-shows the email form with
        # step_id="reauth" instead of "init" — landing on the menu
        # would be a dead-end from the OTP form.
        flow_manager = IntegrationFlowManager(
            self.hass,
            self,
            self.config_entry,
            flow_id_override="reauth",
        )
        return await flow_manager.async_step_otp(user_input)

    async def async_step_preferences(self, user_input=None):
        """FEAT-03 — visible cleaning modes picker."""
        preferences = PreferencesFlowManager(self.hass, self, self.config_entry)
        return await preferences.async_step_preferences(user_input)
