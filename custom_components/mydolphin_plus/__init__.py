"""
This component provides support for MyDolphin Plus.
For more details about this component, please refer to the documentation at
https://home-assistant.io/components/mydolphin_plus/
"""

import logging
import sys

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, EVENT_HOMEASSISTANT_START
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryError

from .common.consts import (
    DEFAULT_NAME,
    DOMAIN,
    INITIAL_TOKENS_KEY,
    PLATFORMS,
    STORAGE_DATA_ID_TOKEN,
    STORAGE_DATA_ID_TOKEN_EXPIRES_AT,
    STORAGE_DATA_MOTOR_UNIT_SERIAL,
    STORAGE_DATA_REFRESH_TOKEN,
    STORAGE_DATA_SERIAL_NUMBER,
)
from .managers.config_manager import ConfigManager
from .managers.coordinator import MyDolphinPlusCoordinator
from .models.exceptions import LoginError

_LOGGER = logging.getLogger(__name__)


async def async_setup(_hass, _config):
    return True


async def _async_cleanup_failed_setup(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: MyDolphinPlusCoordinator | None,
) -> None:
    """Release integration-owned resources when setup aborts (issue #137).

    Scope of this helper — deliberately narrow:

    * terminate the coordinator (drops the BUG-27 persistent listener +
      closes the REST/AWS side);
    * drop the partially initialised coordinator from ``hass.data``.

    Explicitly does **not** touch ``entry._async_process_on_unload`` or
    any other HA-private lifecycle machinery. HA already runs the
    ``entry.async_on_unload`` callbacks itself on the three public
    exception paths (``ConfigEntryError``, ``ConfigEntryAuthFailed``,
    ``ConfigEntryNotReady``) — the correct pattern for a custom
    integration is to clean up its own resources here, then raise the
    appropriate public exception; HA will process the on-unload list
    through its supported lifecycle.

    Strict idempotence: the ``hass.data`` slot is used as the guard.
    A second call with the same coordinator sees the slot already
    empty and skips the termination step.
    """
    domain_data = hass.data.get(DOMAIN, {})
    still_registered = domain_data.get(entry.entry_id) is coordinator

    if coordinator is not None and still_registered:
        try:
            await coordinator.terminate()
        except Exception:  # pragma: no cover — hygiene, do not mask the outer failure
            _LOGGER.exception("terminate() raised during failed-setup cleanup")

    domain_data.pop(entry.entry_id, None)
    if not domain_data:
        hass.data.pop(DOMAIN, None)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a MyDolphin Plus config entry."""
    initialized = False
    coordinator: MyDolphinPlusCoordinator | None = None

    try:
        entry_config = dict(entry.data)
        initial_tokens = entry_config.pop(INITIAL_TOKENS_KEY, None)

        # BUG-03 / BUG-05: strip INITIAL_TOKENS_KEY from entry.data BEFORE we
        # persist the tokens to storage. The previous order — write tokens,
        # then strip — was non-transactional: any exception in between (slow
        # API on the serial fetch, HA killed mid-setup, etc.) left the initial
        # tokens in entry.data on disk. On the next restart they were replayed
        # by this very function on top of the freshly refreshed storage, which
        # is the root cause of the recurring "lost authentication" symptom.
        #
        # CONF_PASSWORD is also stripped here as a one-shot migration for
        # entries created before the Cognito switch (the old code's final
        # async_update_entry(data={CONF_USERNAME: ...}) used to wipe it as a
        # side effect; now that the strip is targeted, we must clear it
        # explicitly so legacy-upgraded entries don't keep a stale encrypted
        # password forever).
        _LEGACY_KEYS = (INITIAL_TOKENS_KEY, CONF_PASSWORD)
        if any(k in entry.data for k in _LEGACY_KEYS):
            stripped_data = {
                k: v for k, v in entry.data.items() if k not in _LEGACY_KEYS
            }
            hass.config_entries.async_update_entry(entry, data=stripped_data)

        config_manager = ConfigManager(hass, entry)
        await config_manager.initialize(entry_config)

        if initial_tokens is not None:
            await config_manager.update_tokens(
                initial_tokens.get(STORAGE_DATA_ID_TOKEN),
                initial_tokens.get(STORAGE_DATA_REFRESH_TOKEN),
                initial_tokens.get(STORAGE_DATA_ID_TOKEN_EXPIRES_AT),
            )
            serial = initial_tokens.get(STORAGE_DATA_SERIAL_NUMBER)
            if serial:
                await config_manager.update_serial_number(serial)
            motor_unit_serial = initial_tokens.get(STORAGE_DATA_MOTOR_UNIT_SERIAL)
            if motor_unit_serial:
                await config_manager.update_motor_unit_serial(motor_unit_serial)

        is_initialized = config_manager.is_initialized

        if is_initialized:
            coordinator = MyDolphinPlusCoordinator(hass, config_manager)

            hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

            if hass.is_running:
                await coordinator.initialize()
            else:
                hass.bus.async_listen_once(
                    EVENT_HOMEASSISTANT_START, coordinator.on_home_assistant_start
                )

            _LOGGER.info("Finished loading integration")

        initialized = is_initialized

    except LoginError as ex:
        _LOGGER.info(f"Failed to login {DEFAULT_NAME} API, cannot log integration")
        # Issue #137 — clean owned resources first, then hand off to HA's
        # public auth-failed path. HA will process entry.async_on_unload
        # (dispatchers + DataUpdateCoordinator.async_shutdown) and open the
        # reauth flow. Do not swallow the failure: silently returning False
        # was the source of the original leak (HA does not process on_unload
        # on the plain-False/unclassified-Exception paths).
        await _async_cleanup_failed_setup(hass, entry, coordinator)
        raise ConfigEntryAuthFailed(f"{DEFAULT_NAME} authentication failed") from ex

    except Exception as ex:
        exc_type, exc_obj, tb = sys.exc_info()
        line_number = tb.tb_lineno

        _LOGGER.error(
            f"Failed to load {DEFAULT_NAME}, error: {ex}, line: {line_number}"
        )
        # Issue #137 — clean owned resources first, then re-raise as
        # ConfigEntryError so HA marks the entry SETUP_ERROR and processes
        # entry.async_on_unload through its supported lifecycle. Using
        # ConfigEntryError rather than ConfigEntryNotReady is deliberate:
        # we cannot classify the failure as transient, so an auto-retry
        # loop would be inappropriate.
        await _async_cleanup_failed_setup(hass, entry, coordinator)
        raise ConfigEntryError(
            f"Failed to load {DEFAULT_NAME}: {ex} (line {line_number})"
        ) from ex

    return initialized


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Unload a config entry."""
    _LOGGER.info(f"Unloading {DOMAIN} integration, Entry ID: {entry.entry_id}")

    entry_id = entry.entry_id
    domain_data = hass.data.get(DOMAIN, {})
    coordinator: MyDolphinPlusCoordinator | None = domain_data.get(entry_id)

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        if coordinator is not None:
            await coordinator.terminate()

        domain_data.pop(entry_id, None)

        if not domain_data:
            hass.data.pop(DOMAIN, None)

    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Remove a config entry."""
    _LOGGER.info(f"Removing {DOMAIN} integration, Entry ID: {entry.entry_id}")

    entry_id = entry.entry_id
    config_manager = ConfigManager(hass, entry)

    await config_manager.remove(entry_id)
