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

_LOGGER = logging.getLogger(__name__)


@config_entries.HANDLERS.register(DOMAIN)
class DomainFlowHandler(config_entries.ConfigFlow):
    """Handle a domain config flow."""

    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_POLL

    def __init__(self):
        super().__init__()

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get the options flow for this handler."""
        return DomainOptionsFlowHandler(config_entry)

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
    """Handle domain options."""

    _config_entry: ConfigEntry

    def __init__(self, config_entry: ConfigEntry):
        """Initialize domain options flow."""
        super().__init__()
        self._config_entry = config_entry

    async def async_step_init(self, user_input=None):
        """Manage the domain options (re-trigger OTP login)."""
        flow_manager = IntegrationFlowManager(self.hass, self, self._config_entry)
        return await flow_manager.async_step_user(user_input)

    async def async_step_otp(self, user_input=None):
        flow_manager = IntegrationFlowManager(self.hass, self, self._config_entry)
        return await flow_manager.async_step_otp(user_input)
