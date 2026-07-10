from abc import ABC
import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_STATE, SERVICE_SELECT_OPTION, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.util import slugify

from .common.base_entity import MyDolphinPlusBaseEntity, async_setup_entities
from .common.clean_modes import KNOWN_LABELED_MODES
from .common.consts import (
    ATTR_ATTRIBUTES,
    DATA_KEY_DESIRED_CLEAN_MODE,
    SIGNAL_DEVICE_NEW,
)
from .common.entity_descriptions import MyDolphinPlusSelectEntityDescription
from .managers.coordinator import MyDolphinPlusCoordinator

_DESIRED_CLEAN_MODE_KEY = slugify(DATA_KEY_DESIRED_CLEAN_MODE)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
):
    @callback
    def _async_device_new(entry_id: str):
        if entry.entry_id != entry_id:
            return

        async_setup_entities(
            hass,
            entry,
            Platform.SELECT,
            MyDolphinPlusSelectEntity,
            async_add_entities,
        )

    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_DEVICE_NEW, _async_device_new)
    )


class MyDolphinPlusSelectEntity(MyDolphinPlusBaseEntity, SelectEntity, ABC):
    """Representation of a sensor."""

    def __init__(
        self,
        entity_description: MyDolphinPlusSelectEntityDescription,
        coordinator: MyDolphinPlusCoordinator,
    ):
        super().__init__(entity_description, coordinator)

        self.entity_description = entity_description

        self._attr_current_option = entity_description.options[0]

    @property
    def options(self) -> list[str]:
        """Return the selectable options for this select entity.

        FEAT-03 — for the `desired_clean_mode` select, filter the
        options dynamically over `coordinator.visible_modes` so the
        picker matches the vacuum's `fan_speed_list`. Every other select
        entity (LED mode, …) keeps the static list from its entity
        description.
        """
        if self.entity_description.key == _DESIRED_CLEAN_MODE_KEY:
            visible = self._local_coordinator.visible_modes
            return [m for m in KNOWN_LABELED_MODES if m in visible]
        return list(self.entity_description.options)

    async def async_select_option(self, option: str) -> None:
        """Change the selected option."""
        await self.async_execute_device_action(SERVICE_SELECT_OPTION, option)

    def update_component(self, data):
        """Fetch new state parameters for the sensor."""
        if data is not None:
            state = data.get(ATTR_STATE)
            attributes = data.get(ATTR_ATTRIBUTES)

            self._attr_current_option = state
            self._attr_extra_state_attributes = attributes

        else:
            self._attr_current_option = self.entity_description.options[0]
