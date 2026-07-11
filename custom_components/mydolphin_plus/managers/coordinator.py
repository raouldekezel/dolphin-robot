from datetime import datetime, timedelta
import logging
import sys
import time
from typing import Callable

from homeassistant.components.number.const import SERVICE_SET_VALUE
from homeassistant.components.remote import ATTR_ACTIVITY, SERVICE_SEND_COMMAND
from homeassistant.components.vacuum import (
    SERVICE_LOCATE,
    SERVICE_PAUSE,
    SERVICE_RETURN_TO_BASE,
    SERVICE_SET_FAN_SPEED,
    SERVICE_START,
    VacuumActivity,
)
from homeassistant.const import (
    ATTR_ICON,
    ATTR_MODE,
    ATTR_STATE,
    CONF_STATE,
    SERVICE_SELECT_OPTION,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
)
from homeassistant.core import Event, callback
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo, EntityDescription
from homeassistant.helpers.entity_registry import (
    RegistryEntryDisabler,
    async_entries_for_config_entry,
    async_get as async_get_entity_registry,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import slugify
import homeassistant.util.dt as dt_util

from ..common.calculated_state import CalculatedState
from ..common.clean_modes import (
    KNOWN_LABELED_MODES,
    CleanModes,
    get_clean_mode_cycle_time_key,
)
from ..common.connectivity_status import ConnectivityStatus
from ..common.consts import (
    ATTR_ACTIONS,
    ATTR_ATTRIBUTES,
    ATTR_EXPECTED_END_TIME,
    ATTR_IS_ON,
    ATTR_RESET_FBI,
    ATTR_START_TIME,
    ATTR_STATUS,
    CLOCK_HOURS_ICON,
    CLOCK_HOURS_NONE,
    CLOCK_HOURS_TEXT,
    CONF_VISIBLE_MODES,
    CONFIGURATION_URL,
    DATA_CYCLE_INFO_CLEANING_MODE,
    DATA_CYCLE_INFO_CLEANING_MODE_DURATION,
    DATA_CYCLE_INFO_CLEANING_MODE_START_TIME,
    DATA_DEBUG_WIFI_RSSI,
    DATA_ERROR_CODE,
    DATA_ERROR_TURN_ON_COUNT,
    DATA_FILTER_BAG_INDICATION_RESET_FBI,
    DATA_KEY_AWS_BROKER,
    DATA_KEY_BATTERY,
    DATA_KEY_BUSY,
    DATA_KEY_CLEAN_MODE,
    DATA_KEY_CYCLE_COUNT,
    DATA_KEY_CYCLE_TIME,
    DATA_KEY_CYCLE_TIME_LEFT,
    DATA_KEY_DESIRED_CLEAN_MODE,
    DATA_KEY_FILTER_STATUS,
    DATA_KEY_LED,
    DATA_KEY_LED_INTENSITY,
    DATA_KEY_LED_MODE,
    DATA_KEY_NETWORK_NAME,
    DATA_KEY_NEXT_SCHEDULED_CYCLE_TIME,
    DATA_KEY_NEXT_SCHEDULED_MODE,
    DATA_KEY_NEXT_SCHEDULED_RUN,
    DATA_KEY_POWER_SUPPLY_STATUS,
    DATA_KEY_PWS_ERROR,
    DATA_KEY_REMOTE,
    DATA_KEY_ROBOT_ERROR,
    DATA_KEY_ROBOT_STATUS,
    DATA_KEY_ROBOT_TYPE,
    DATA_KEY_RSSI,
    DATA_KEY_STATUS,
    DATA_KEY_VACUUM,
    DATA_LED_ENABLE,
    DATA_LED_INTENSITY,
    DATA_LED_MODE,
    DATA_ROBOT_NAME,
    DATA_SECTION_CYCLE_INFO,
    DATA_SECTION_DEBUG,
    DATA_SECTION_DELAY,
    DATA_SECTION_DYNAMIC,
    DATA_SECTION_FILTER_BAG_INDICATION,
    DATA_SECTION_LED,
    DATA_SECTION_PWS_ERROR,
    DATA_SECTION_ROBOT_ERROR,
    DATA_SECTION_SYSTEM_STATE,
    DATA_SECTION_WEEKLY_SETTINGS,
    DATA_SECTION_WIFI,
    DATA_SYSTEM_STATE_TIME_ZONE,
    DATA_SYSTEM_STATE_TIME_ZONE_NAME,
    DATA_SYSTEM_STATE_TURN_ON_COUNT,
    DATA_WIFI_NETWORK_NAME,
    DEFAULT_ENABLE,
    DEFAULT_LED_INTENSITY,
    DEFAULT_NAME,
    DOMAIN,
    DYNAMIC_DESCRIPTION_TEMPERATURE,
    DYNAMIC_TYPE_IOT_RESPONSE,
    ERROR_CLEAN_CODES,
    FILTER_BAG_ICONS,
    FILTER_BAG_STATUS,
    ICON_LED_MODES,
    LED_MODE_BLINKING,
    LED_MODE_ICON_DEFAULT,
    MANUFACTURER,
    PLATFORMS,
    RECONNECT_BACKOFF_MAX,
    SIGNAL_API_STATUS,
    SIGNAL_AWS_CLIENT_STATUS,
    UPDATE_API_INTERVAL,
    UPDATE_WS_INTERVAL,
)
from ..common.joystick_direction import JoystickDirection
from ..common.next_scheduled_run import (
    ATTR_NSR_CLEANING_MODE,
    ATTR_NSR_DAY_OF_WEEK,
    ATTR_NSR_SOURCE,
    ATTR_NSR_STATE,
    compute_next_scheduled_run,
)
from ..models.system_details import SystemDetails
from .aws_client import AWSClient
from .config_manager import ConfigManager
from .rest_api import RestAPI

_LOGGER = logging.getLogger(__name__)

# HARD-11 — optimistic-overlay TTL and start-serialization guard TTL.
#
# Overlay TTL covers the worst observed echo gap (~60 s to `pwsState=on`,
# ~9 s to `holdWeekly` after pause) with margin.
#
# Guard TTL covers the worst observed post-pause acknowledgement latency
# under contention (9.2 s in T3 pick #5, 10.5 s in T4 — see
# `docs/diag/2026-06-26_bug-19_e5a-...`). A single value is used: when it
# elapses, the Start block lifts AND the guard bookkeeping clears at the
# same instant — so a Run cannot land in a window where it is allowed but
# the cap is still scheduled to wipe its fresh overlay (v1.1's
# block-window vs. cap-timeout split was the source of that fragility).
_OPTIMISTIC_TTL_S: float = 120.0
_PAUSE_GUARD_TTL_S: float = 15.0

# HARD-11 — the calculated_state values the firmware emits when the
# system is *at rest* (cycle not running). The pause-acknowledgement
# edge fires on entering ANY of them — not only `HOLD_WEEKLY` — so the
# guard resolves correctly for robots that are not on an active weekly
# schedule (which settle to `HOLD_DELAY` or `OFF` instead). All three
# values are produced by `models/system_details._get_updated_data`:
# `OFF` is the default fallback when no other branch matches,
# `HOLD_DELAY` and `HOLD_WEEKLY` come from the matching power-supply
# branches.
_PAUSE_ACK_REST_STATES: frozenset[CalculatedState] = frozenset(
    {
        CalculatedState.HOLD_WEEKLY,
        CalculatedState.HOLD_DELAY,
        CalculatedState.OFF,
    }
)

# BUG-24 — statuses whose recovery requires user action, not tick-driven
# reconnection. EXPIRED_TOKEN needs the OTP flow (retrying just re-hits
# "no refresh token stored" forever and can race the reauth); INVALID_*
# and MISSING_API_KEY need the operator to fix credentials. Neither the
# seed (`_handle_connection_failure`) nor the tick driver
# (`_maybe_reconnect`) should schedule or fire retries while the API is
# in one of these states.
_NEEDS_USER_STATUSES: frozenset[ConnectivityStatus] = frozenset(
    {
        ConnectivityStatus.EXPIRED_TOKEN,
        ConnectivityStatus.INVALID_CREDENTIALS,
        ConnectivityStatus.INVALID_ACCOUNT,
        ConnectivityStatus.MISSING_API_KEY,
    }
)


class MyDolphinPlusCoordinator(DataUpdateCoordinator):
    """My custom coordinator."""

    _api: RestAPI
    _aws_client: AWSClient | None

    _data_mapping: dict[str, Callable[[EntityDescription], dict | None]] | None
    _system_details: SystemDetails

    _last_update_api: float
    _last_update_ws: float

    # FEAT-03 — class-level default is required so
    # `MagicMock(spec=MyDolphinPlusCoordinator)` includes this attribute
    # in its allow-list. `spec=` uses `dir()` on the class, which lists
    # class attributes with a default but NOT bare annotations, so the
    # sentinel `frozenset()` is load-bearing for existing HARD-11 /
    # HARD-13 tests that access `_get_desired_clean_mode_data` /
    # `_get_vacuum_data` via a spec'd stub.
    _visible_modes: frozenset[str] = frozenset()

    # BUG-27 — dropped in `terminate()`. Class-level default so
    # `MagicMock(spec=…)` in tests sees the attribute even before
    # `initialize()` has set it (spec is derived from `dir()`).
    _no_op_unsub: Callable[[], None] | None = None

    def __init__(self, hass, config_manager: ConfigManager):
        """Initialize my coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=config_manager.name,
            update_interval=UPDATE_WS_INTERVAL,
            update_method=self._async_update_data,
        )

        self._api = RestAPI(hass, config_manager)
        self._aws_client = AWSClient(hass, config_manager, self._on_mqtt_data_update)

        self._config_manager = config_manager

        self._data_mapping = None
        self._system_details = SystemDetails()
        self._has_real_data = False

        self._last_update_api = 0
        self._last_update_ws = 0
        self._reconnection_attempts = 0
        # FEAT-03 — visible cleaning modes. Single source of truth for
        # `vacuum.fan_speed_list`, `select.desired_clean_mode.options`,
        # and the `disabled_by` state of the per-mode
        # `number.cycle_time_<mode>` entities. Seeded from
        # `entry.options[CONF_VISIBLE_MODES]` at setup
        # (`_seed_visible_modes`) and mutated only via
        # `async_set_visible_modes`, which updates the registry and
        # calls `async_update_listeners` to propagate the new
        # `fan_speed_list` / `select.options` via `cached_properties`.
        # R1 (no reload) is preserved for the common case (hiding a
        # mode); only UN-hiding schedules a config-entry reload because
        # HA needs a fresh platform pass to re-add the previously
        # disabled entity.
        self._visible_modes: frozenset[str] = frozenset(KNOWN_LABELED_MODES)
        # BUG-24 (follow-up) — monotonic deadline of the next scheduled
        # reconnect. The tick in `_async_update_data` drives retries via
        # `_maybe_reconnect`: fires `_api.initialize()` when the integration
        # is not fully connected, the API is not in a user-action state
        # (`_NEEDS_USER_STATUSES`), `_next_retry_at > 0` (seed present), and
        # `now_mono >= _next_retry_at`. Seeded by `_handle_connection_failure`
        # on entering-disconnected dispatches (except user-action ones) and
        # bumped inside `_maybe_reconnect`'s `finally` after every attempt
        # that did not recover — from the **end-of-attempt** monotonic clock,
        # so a slow `initialize()` cannot collapse the backoff below the
        # scheduled interval. Reset to `0.0` only when both sides are
        # fully connected.
        self._next_retry_at: float = 0.0

        # BUG-24 (follow-up) — single-writer guard for the retry schedule.
        # Set to True around the `_api.initialize()` call inside
        # `_maybe_reconnect`. While True, `_schedule_next_retry` no-ops so
        # a status callback fired during the attempt cannot double-schedule
        # (the `finally` block is the sole writer during an attempt). Also
        # prevents overlapping tick-driven retries when the current attempt
        # is slower than the tick interval.
        self._reconnect_in_progress: bool = False

        # MQTT debouncing
        self._mqtt_debouncer = Debouncer(
            hass,
            _LOGGER,
            cooldown=1.0,
            immediate=False,
            function=self._debounced_mqtt_refresh,
        )

        # Safety net for maximum MQTT delay
        self._last_mqtt_refresh = 0
        self._max_mqtt_delay = 5.0

        # FEAT-04 — atomic per-tick memo so the three next-scheduled sensors
        # (run / mode / cycle time) read the same computed dict and cannot
        # disagree on which slot won when the wall clock crosses a slot
        # boundary between two getters. Recomputed at the end of every
        # `_async_update_data` cycle.
        self._next_scheduled_data: dict | None = None

        # BUG-13 (write-on-commit) — staged cleaning mode held in coordinator
        # memory, never persisted. While docked, picking a mode updates this
        # field only; the firmware is written at Run. Seeded from the shadow's
        # reported mode on the first refresh and re-seeded any time the
        # firmware-reported mode changes outside an HA-initiated write (app /
        # scheduler / running live-swap echo). Lost on HA restart by design —
        # a reboot shows the robot's real mode, not a stale unlaunched pick.
        self._desired_clean_mode: str | None = None
        self._last_seen_reported_clean_mode: str | None = None

        # HARD-11 — optimistic overlay masking the firmware echo gap.
        # vacuum side covers Run only (honest-linger on Stop); statut side
        # carries the click acknowledgement for both Run and Stop. A single
        # monotonic deadline applies to both — when it clears, both clear.
        self._optimistic_vacuum_state: VacuumActivity | None = None
        self._optimistic_statut: CalculatedState | None = None
        self._optimistic_origin_vacuum_state: VacuumActivity | None = None
        self._optimistic_deadline: float | None = None

        # HARD-11 — start-serialization guard. Set when `pause()` is written,
        # cleared by either a *transition* into `holdWeekly` (firmware
        # acknowledged) or a cap timeout. New `set_cleaning_mode` writes
        # are refused while the guard is armed and the window has not
        # elapsed — this is the load-bearing protection against the BUG-19
        # / BUG-20 cascade.
        self._pause_issued_at: float | None = None
        # HARD-11 v1.1 — edge tracker for the pause-acknowledgement signal.
        # Level-triggering on `calculated_state == HOLD_WEEKLY` would clear
        # the guard on the very next tick whenever the firmware was *already*
        # at HOLD_WEEKLY at click time (i.e. pause clicked during the start
        # echo gap, before `pwsState=on` flipped) — defeating the guard in
        # exactly the window it exists to cover. The edge predicate fires
        # only on the *entering* transition, which cannot pre-exist.
        self._last_observed_calculated_state: CalculatedState | None = None

        self._load_signal_handlers()

    @property
    def robot_name(self):
        robot_name = self.api_data.get(DATA_ROBOT_NAME)

        if robot_name is None or robot_name == "":
            robot_name = DEFAULT_NAME

        return robot_name

    @property
    def api_data(self) -> dict:
        data = self._api.data

        return data

    @property
    def aws_data(self) -> dict:
        data = self._aws_client.data

        return data

    @property
    def config_manager(self) -> ConfigManager:
        config_manager = self._config_manager

        return config_manager

    async def on_home_assistant_start(self, _event_data: Event):
        await self.initialize()

    async def terminate(self):
        # BUG-27 — release the no-op listener registered in `initialize()`
        # for lifecycle hygiene. Not required for correctness: HA's
        # `DataUpdateCoordinator.__init__` self-wires
        # `config_entry.async_on_unload(self.async_shutdown)`, so the
        # scheduled refresh is cancelled on unload regardless. But
        # dropping the listener explicitly here keeps `_listeners` empty
        # on the shutdown path, matching the pre-BUG-27 lifecycle.
        if self._no_op_unsub is not None:
            self._no_op_unsub()
            self._no_op_unsub = None
        await self._aws_client.terminate()

    async def initialize(self):
        self._build_data_mapping()

        entry = self.config_manager.entry
        self._seed_visible_modes(entry)
        # BUG-27 — DataUpdateCoordinator only reschedules its periodic
        # refresh when at least one listener is registered. Entities
        # register on `SIGNAL_DEVICE_NEW`, which fires only after
        # `_api.update()` — reachable only from a CONNECTED status. If
        # the initial connection fails (e.g. Maytronics `getToken`
        # refusing during a backend outage), status goes FAILED without
        # ever reaching CONNECTED, no entities are added, no listeners
        # exist, and the coordinator's tick never runs → the BUG-24
        # tick-driven retry loop never fires and the integration stays
        # dormant until manual reload. Register a no-op listener here so
        # the tick keeps running regardless of connection state. Stored
        # so `terminate` can drop it cleanly. Guard on `None` so a second
        # `initialize()` call (defensive — not part of the normal
        # lifecycle, but not something to silently double-register on
        # either) doesn't stack two listeners and orphan the previous
        # unsub handle.
        if self._no_op_unsub is None:
            self._no_op_unsub = self.async_add_listener(lambda: None)
        await self.hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

        _LOGGER.info(f"Start loading {DOMAIN} integration, Entry ID: {entry.entry_id}")

        await self.async_request_refresh()

        await self._api.initialize()

    @property
    def visible_modes(self) -> frozenset[str]:
        """Cleaning modes the operator has chosen to expose.

        FEAT-03 — read by `vacuum.fan_speed_list` and
        `select.desired_clean_mode.options` every tick; also gates the
        `hidden_by` flag of the per-mode `number.cycle_time_<mode>`
        entities on options-flow save. Defaults to the full curated set
        (`KNOWN_LABELED_MODES`) when the user has not yet saved a
        preference — matches R2 (first-boot shows everything).
        """
        return self._visible_modes

    def _seed_visible_modes(self, entry) -> None:
        """Seed `_visible_modes` from persisted options at setup time.

        Any value stored under `CONF_VISIBLE_MODES` that is not part of
        the curated `KNOWN_LABELED_MODES` set is dropped defensively.
        An empty saved set falls back to the full curated set so the
        operator can never lock themselves into an empty pick-list by
        accident (a stray `[]` in `.storage` should not brick the
        integration).
        """
        stored = entry.options.get(CONF_VISIBLE_MODES) if entry is not None else None
        if stored is None:
            self._visible_modes = frozenset(KNOWN_LABELED_MODES)
            return
        clean = {m for m in stored if m in KNOWN_LABELED_MODES}
        self._visible_modes = (
            frozenset(clean) if clean else frozenset(KNOWN_LABELED_MODES)
        )

    async def async_set_visible_modes(self, new_visible: frozenset[str]) -> None:
        """Propagate a new visible-modes set to the running coordinator.

        Called from the options-flow preferences step (which owns the
        persistence). Does four things:

        1. Updates the in-memory `_visible_modes` set.
        2. Toggles `disabled_by = RegistryEntryDisabler.INTEGRATION` on
           every `number.cycle_time_<mode>` whose mode is now hidden,
           clears it on every mode that is now visible. `disabled_by`
           is stronger than `hidden_by`: the entity disappears from the
           device details page too (which was the FEAT-03 in-vivo
           feedback — `hidden_by` still listed them there). Trade-off:
           un-hiding requires HA to re-add the entity, so we
           `async_schedule_reload` only when at least one mode was
           un-hidden. Hiding-only saves stay reload-free (R1 preserved
           for the common case).
        3. Refreshes coordinator listeners so `vacuum.fan_speed_list`
           and `select.desired_clean_mode.options` re-evaluate on the
           next tick via `cached_properties`.
        4. If any mode was UN-hidden compared to the previous set,
           schedules a config-entry reload so HA's platforms re-run
           `async_setup_entry` and re-add the newly-enabled entity.
           No-op when only hiding.

        Does NOT write `entry.options` — the flow finalize
        (`async_create_entry(data=...)`) owns persistence. Doing both
        would clobber unrelated option keys, because
        `async_create_entry` REPLACES options with `data` wholesale.
        """
        clean = frozenset(m for m in new_visible if m in KNOWN_LABELED_MODES)
        if not clean:
            clean = frozenset(KNOWN_LABELED_MODES)
        previous = self._visible_modes
        self._visible_modes = clean

        self._apply_visible_modes_to_registry(clean)
        self.async_update_listeners()

        # Reload only when a mode was newly RE-enabled — HA needs a
        # platform pass to re-add the entity whose `disabled_by` we just
        # cleared. Newly-disabled modes are handled instantly by the
        # registry write above (state removed, no reload needed).
        newly_visible = clean - previous
        if newly_visible:
            entry = self.config_manager.entry
            if entry is not None:
                self.hass.config_entries.async_schedule_reload(entry.entry_id)

    def _apply_visible_modes_to_registry(self, visible: frozenset[str]) -> None:
        """Toggle `disabled_by` on each `number.cycle_time_<mode>` entity.

        FEAT-03 (post-review pivot 2026-07-11) — use
        `RegistryEntryDisabler.INTEGRATION` so hidden modes truly
        disappear from the device details page, not only from
        dashboards/entity pickers. `hidden_by` (the initial Q3 choice)
        turned out to leave the per-mode numbers visible in the
        "Capteurs de configuration" section of the device details page
        — Raoul's #51 2026-07-11 feedback.

        Entities are matched by their `translation_key`, which we
        control (it's the entity description's `key` at construction
        time). That avoids reconstructing the unique_id, which depends
        on the user's currently-active display name.
        """
        entry = self.config_manager.entry
        if entry is None:
            return
        registry = async_get_entity_registry(self.hass)

        # translation_key -> mode string
        per_mode_translation_keys = {
            get_clean_mode_cycle_time_key(CleanModes(m)): m for m in KNOWN_LABELED_MODES
        }

        for reg_entry in async_entries_for_config_entry(registry, entry.entry_id):
            if not reg_entry.entity_id.startswith("number."):
                continue
            mode = per_mode_translation_keys.get(reg_entry.translation_key or "")
            if mode is None:
                continue
            target = None if mode in visible else RegistryEntryDisabler.INTEGRATION
            if reg_entry.disabled_by == target:
                continue
            registry.async_update_entity(reg_entry.entity_id, disabled_by=target)

    def _load_signal_handlers(self):
        # BUG-09: the previous code called `.__await__()` on a freshly created
        # task and discarded the resulting iterator, firing the task without
        # any reference — exceptions disappeared silently and the task was
        # not cancelled on entry unload. `ConfigEntry.async_create_task(hass,
        # coro)` ties the task to the entry lifecycle (cancelled on reload),
        # which is strictly better than upstream PR #287's
        # `hass.async_create_task` (loop-scoped, cancelled only on full HA
        # shutdown).
        entry = self.config_entry

        @callback
        def on_api_status_changed(entry_id: str, status: ConnectivityStatus):
            entry.async_create_task(
                self.hass, self._on_api_status_changed(entry_id, status)
            )

        @callback
        def on_aws_client_status_changed(entry_id: str, status: ConnectivityStatus):
            entry.async_create_task(
                self.hass, self._on_aws_client_status_changed(entry_id, status)
            )

        entry.async_on_unload(
            async_dispatcher_connect(
                self.hass, SIGNAL_API_STATUS, on_api_status_changed
            )
        )

        entry.async_on_unload(
            async_dispatcher_connect(
                self.hass, SIGNAL_AWS_CLIENT_STATUS, on_aws_client_status_changed
            )
        )

    def get_device_debug_data(self) -> dict:
        config_data = self._config_manager.get_debug_data()

        data = {
            "config": config_data,
            "api": self.api_data,
            "aws_client": self._aws_client.data,
        }

        return data

    def get_device(self) -> DeviceInfo:
        data = self.api_data
        device_name = self.robot_name
        model = data.get("Product Description")
        versions = data.get("versions", {})
        pws_version = versions.get("pwsVersion", {})
        sw_version = pws_version.get("pwsSwVersion")
        hw_version = pws_version.get("pwsHwVersion")

        serial_number = self.config_manager.serial_number

        device_info = DeviceInfo(
            identifiers={(DEFAULT_NAME, serial_number)},
            name=device_name,
            model=model,
            manufacturer=MANUFACTURER,
            hw_version=hw_version,
            sw_version=sw_version,
            configuration_url=CONFIGURATION_URL,
        )

        return device_info

    async def _on_api_status_changed(self, entry_id: str, status: ConnectivityStatus):
        if entry_id != self._config_manager.entry_id:
            return

        if status == ConnectivityStatus.CONNECTED:
            await self._api.update()

            await self._aws_client.update_api_data(self.api_data)

            await self._aws_client.initialize()

            # BUG-24 (review r2) — reset the retry state only after the
            # complete cascade lands on `_is_fully_connected()`.
            # `aws_client.initialize()` returns as soon as its awscrt
            # connect is dispatched; AWS may still be CONNECTING. If we
            # cleared `_next_retry_at` preemptively here and the AWS
            # connection then stalled without a terminal callback, the
            # tick watchdog would be silently disarmed and no further
            # attempt would fire. When the compound state is not yet
            # healthy after the awaited cascade, keep or seed the retry
            # deadline via the idempotent helper — the tick will re-drive
            # if AWS never reaches CONNECTED.
            if self._is_fully_connected():
                self._reconnection_attempts = 0
                self._next_retry_at = 0.0
            else:
                self._ensure_retry_scheduled()

        elif status in [
            ConnectivityStatus.FAILED,
            ConnectivityStatus.INVALID_CREDENTIALS,
            ConnectivityStatus.EXPIRED_TOKEN,
        ]:
            if status == ConnectivityStatus.EXPIRED_TOKEN:
                await self._start_reauth_if_needed()
            await self._handle_connection_failure()

    async def _start_reauth_if_needed(self):
        """Start a HA reauthentication flow.

        ``ConfigEntry.async_start_reauth`` is synchronous in HA Core and is
        idempotent: calling it while a reauth flow is already in progress
        re-focuses the existing flow rather than creating a duplicate. There
        is therefore no need for our own ``_reauth_in_progress`` guard, which
        used to stick to ``True`` after a dismissed flow and lock the
        integration in a retry loop (BUG-02). The previous ``await`` on the
        synchronous call also raised ``TypeError`` swallowed by the surrounding
        except, masking failures (BUG-01).
        """
        entry = self.config_manager.entry
        if entry is None:
            return

        try:
            entry.async_start_reauth(self.hass)
            _LOGGER.warning("Started Home Assistant reauthentication flow")
        except Exception:
            _LOGGER.exception("Failed to start Home Assistant reauthentication flow")

    async def _on_aws_client_status_changed(
        self, entry_id: str, status: ConnectivityStatus
    ):
        if entry_id != self._config_manager.entry_id:
            return

        if status == ConnectivityStatus.CONNECTED:
            # BUG-24 (follow-up) — reset the retry state only when the
            # compound state is actually healthy. AWS is CONNECTED right
            # now; if the API is also CONNECTED we can safely wipe the
            # counter and deadline. If not (rare, transient race), leave
            # them alone and let the next-side transition trigger the
            # reset — otherwise a next AWS failure would restart backoff
            # from #0 while the API is still limping.
            if self._is_fully_connected():
                self._reconnection_attempts = 0
                self._next_retry_at = 0.0
            await self._aws_client.update()

        if status in [ConnectivityStatus.FAILED, ConnectivityStatus.NOT_CONNECTED]:
            await self._handle_connection_failure()

    def _on_mqtt_data_update(self):
        """Callback when MQTT data is updated - with max delay safety net."""
        if self.hass is None:
            return

        now = datetime.now().timestamp()
        time_since_last = now - self._last_mqtt_refresh

        # Safety net: force refresh if waited too long
        if time_since_last >= self._max_mqtt_delay:
            self._last_mqtt_refresh = now
            # Use call_soon_threadsafe to schedule from a different thread
            self.hass.loop.call_soon_threadsafe(
                lambda: self.hass.async_create_task(self.async_request_refresh())
            )

        else:
            # Normal debounced call
            self.hass.loop.call_soon_threadsafe(
                lambda: self.hass.async_create_task(self._mqtt_debouncer.async_call())
            )

    async def _debounced_mqtt_refresh(self):
        """Execute coordinator refresh - called by debouncer after cooldown."""
        self._last_mqtt_refresh = datetime.now().timestamp()
        await self.async_request_refresh()
        _LOGGER.debug("Executed debounced MQTT refresh")

    def _ensure_retry_scheduled(self, now_mono: float | None = None) -> None:
        """Idempotent seed for the retry schedule.

        BUG-24 (review r2) — the single-writer property of the retry
        state machine cannot rely on ``_reconnect_in_progress`` alone
        because HA dispatcher callbacks are **deferred** (queued on the
        event loop, not called synchronously from ``_set_status``). A
        callback queued during ``_api.initialize()`` can drain *after*
        ``_maybe_reconnect``'s ``finally`` has cleared the guard and
        scheduled its own deadline — resulting in the counter bumping
        twice for a single actual attempt and the ``1 → 2 → 4 → 8 →
        15`` sequence collapsing.

        The idempotent seed closes this gap. Every external caller
        (``_handle_connection_failure``, ``_maybe_reconnect``'s
        ``finally``, ``_on_api_status_changed(CONNECTED)`` watchdog
        path) goes through here rather than calling
        ``_schedule_next_retry`` directly. If a deadline is already
        armed (``_next_retry_at > 0``) OR an attempt is in flight
        (``_reconnect_in_progress``), this method is a no-op.

        ``_schedule_next_retry`` remains the unconditional bump path,
        called only from here.
        """
        if self._reconnect_in_progress:
            return
        if self._next_retry_at > 0:
            return
        self._schedule_next_retry(now_mono)

    def _schedule_next_retry(self, now_mono: float | None = None) -> None:
        """Bump the attempt counter and set the next retry deadline.

        BUG-24 (follow-up) — single-writer semantics. Since BUG-24
        (review r2), this method is normally reached through
        ``_ensure_retry_scheduled`` — that idempotent helper is the
        entry point for every external caller
        (``_handle_connection_failure`` on the entering-disconnected
        dispatch, ``_maybe_reconnect``'s ``finally`` when the tick-
        driven retry has just fired and the integration is still not
        fully connected, and the ``_on_api_status_changed(CONNECTED)``
        watchdog path when the compound state is not yet healthy after
        the awaited cascade). The pre-r2 layout had those sites call
        here directly; they no longer do. This method remains the
        unconditional bump path called only from the helper.

        While a retry attempt is in flight (`_reconnect_in_progress`),
        this method no-ops. This prevents the failure-callback path
        (`_on_api_status_changed(FAILED)` → `_handle_connection_failure`
        → `_ensure_retry_scheduled` → here) from double-counting the
        same attempt when the `finally` block is also about to
        schedule. The `finally` block clears `_reconnect_in_progress`
        before it calls into the helper, so it is the sole scheduler
        for its own attempt.

        Uses `time.monotonic()` when no explicit clock is passed:
        wall-clock jumps (NTP correction, DST) must not skip retries or
        fire them early. Callers that already captured a monotonic
        instant (typically end-of-attempt) pass it in to avoid a second
        clock read.
        """
        if self._reconnect_in_progress:
            _LOGGER.debug(
                "Skipping callback-driven retry schedule while an attempt "
                "is in flight; the attempt's finally will reschedule."
            )
            return

        if now_mono is None:
            now_mono = time.monotonic()

        # Exponential backoff: 1, 2, 4, 8, 15 min (capped).
        backoff_minutes = min(
            2**self._reconnection_attempts,
            RECONNECT_BACKOFF_MAX.total_seconds() / 60,
        )
        self._reconnection_attempts += 1
        self._next_retry_at = now_mono + backoff_minutes * 60

        _LOGGER.warning(
            f"Connection failure - reconnection attempt "
            f"#{self._reconnection_attempts} scheduled in "
            f"{backoff_minutes:g} minute(s)"
        )

    def _aws_status(self) -> ConnectivityStatus:
        """Return the AWS client's status if the client exists.

        `_aws_client` is typed as ``AWSClient | None`` and can be ``None``
        during the very early setup window before ``__init__`` finishes.
        Treat that as `NOT_CONNECTED` for the purposes of the
        fully-connected predicate.
        """
        if self._aws_client is None:
            return ConnectivityStatus.NOT_CONNECTED
        return self._aws_client.status

    def _is_fully_connected(self) -> bool:
        """Both the API and the AWS client report `CONNECTED`."""
        return (
            self._api.status == ConnectivityStatus.CONNECTED
            and self._aws_status() == ConnectivityStatus.CONNECTED
        )

    async def _handle_connection_failure(self):
        """Dispatch-driven entry point on a disconnected transition.

        BUG-24 — no longer sleeps or calls `_api.initialize()`; those
        live in the tick (`_maybe_reconnect`) so a subsequent failed
        retry that leaves the status unchanged (no dispatch) still gets
        a new attempt at the next tick boundary. Always terminates the
        AWS client; seeds the retry schedule only when the API status
        is retryable — i.e. not one of the statuses that need user
        action, which would just bump the attempt counter uselessly.

        BUG-24 (review r2) — goes through the idempotent
        ``_ensure_retry_scheduled`` seed rather than
        ``_schedule_next_retry`` directly. This dedupes both against
        (a) synchronous callback paths fired during
        ``_maybe_reconnect`` (guard on ``_reconnect_in_progress``) and
        (b) deferred dispatcher callbacks that drain after
        ``_maybe_reconnect``'s ``finally`` has already scheduled
        (guard on ``_next_retry_at > 0``).
        """
        await self._aws_client.terminate()
        if self._api.status in _NEEDS_USER_STATUSES:
            return
        self._ensure_retry_scheduled()

    async def _maybe_reconnect(self, now: float) -> None:
        """Tick-driven retry driver (BUG-24, sole driver).

        Fires `_api.initialize()` when the integration is **not fully
        connected** and the scheduled backoff has elapsed. Considers
        both sides:

        - **API-side FAILED / NOT_CONNECTED** — the direct case: the
          coordinator kicks the login pipeline.
        - **AWS/MQTT-side FAILED with API still CONNECTED** — the
          MQTT-timeout case that started the #120 incident: gating on
          `_api.status` alone would miss it (regression flagged in the
          #122 review). Re-driving `_api.initialize()` re-runs
          `_login`, which flips API through TEMPORARY_CONNECTED →
          CONNECTED, which cascades to `_aws_client.initialize()` via
          the CONNECTED dispatch — that's how AWS recovers.

        Skips when:

        - the integration is fully connected;
        - the API is in a user-action state (`_NEEDS_USER_STATUSES`);
        - the schedule was never seeded (`_next_retry_at <= 0`);
        - the scheduled retry time has not yet been reached; or
        - another attempt is already in flight
          (`_reconnect_in_progress`) — prevents overlapping tick-driven
          retries when `initialize()` outlives the tick interval.

        BUG-24 (follow-up) — three hardening properties over the
        original tick driver:

        1. **Exceptions never bypass the backoff.** The `finally`
           block schedules whenever the integration is not fully
           connected and the API is not in a user-action state — even
           if `initialize()` raised. Pre-fix, the reschedule condition
           was `api.status != CONNECTED`, so an exception on the
           API-CONNECTED / AWS-FAILED path (where the status is
           unchanged) skipped the reschedule and the next tick fired
           immediately with `_next_retry_at` still in the past.
        2. **The next deadline uses end-of-attempt monotonic time.**
           `initialize()` can take seconds; the pre-fix code passed
           the start-of-attempt wall-clock timestamp, so a slow
           `initialize()` shortened the effective backoff. The
           `end_mono` sample is captured after the awaited call.
        3. **No double scheduling — including across deferred
           dispatcher callbacks.** The current deadline is *consumed*
           (set to 0) atomically with arming `_reconnect_in_progress`,
           before yielding to `_api.initialize()`. Any synchronous
           failure callback fired during the attempt is suppressed by
           the guard. Any *deferred* callback (HA dispatcher queues
           callbacks on the event loop rather than calling them
           synchronously from `_set_status`) that drains after the
           `finally` has already scheduled sees `_next_retry_at > 0`
           and is no-op'd by the idempotent `_ensure_retry_scheduled`
           seed — the sequence `1 → 2 → 4 → 8 → 15` cannot skip
           stages.
        """
        if self._is_fully_connected():
            return
        if self._api.status in _NEEDS_USER_STATUSES:
            return
        if self._next_retry_at <= 0 or now < self._next_retry_at:
            return
        if self._reconnect_in_progress:
            _LOGGER.debug("Skipping tick-driven retry: an attempt is already in flight")
            return

        _LOGGER.info(f"Firing reconnection attempt #{self._reconnection_attempts}")
        # BUG-24 (review r2) — consume the current deadline AND arm
        # the in-flight guard atomically (no `await` between the two)
        # before yielding to `_api.initialize()`. Consuming turns the
        # `finally`-side `_ensure_retry_scheduled` into a real schedule
        # (idempotent seed sees `_next_retry_at == 0` and proceeds).
        # Any deferred failure callback that drains later sees a
        # deadline armed by the `finally` and is no-op'd.
        self._next_retry_at = 0.0
        self._reconnect_in_progress = True
        try:
            await self._api.initialize()
        except Exception as ex:
            # Most `_login` paths swallow their exceptions and set
            # `FAILED` themselves, but not all (config-manager storage
            # writes and unexpected errors can escape). Swallow here so
            # the coordinator tick keeps ticking rather than reporting
            # `UpdateFailed` to HA; the `finally` block below still
            # schedules the next attempt, keeping the backoff intact.
            _LOGGER.warning(f"Reconnection attempt raised: {ex}")
        finally:
            # Clear the in-flight guard before scheduling: this method
            # is the sole scheduler for its own attempt (dispatch-driven
            # scheduling was suppressed while the guard was set, and
            # any deferred callback that drains later will find a
            # deadline already armed and no-op via the idempotent seed).
            self._reconnect_in_progress = False
            # Reschedule from end-of-attempt monotonic time so a slow
            # `initialize()` cannot shorten the interval to the next
            # retry (pre-fix the deadline was computed from the
            # start-of-attempt wall clock).
            end_mono = time.monotonic()
            if self._api.status in _NEEDS_USER_STATUSES:
                # OTP flow owns recovery; do not tick again. The
                # deadline stays cleared (consumed at the top of the
                # attempt) so a later successful reauth sees a clean
                # slate.
                return
            # Reschedule while the compound state is not fully healthy
            # — this catches the API-CONNECTED / AWS-FAILED path where
            # the pre-fix predicate skipped the reschedule and let the
            # next tick fire immediately. Idempotent seed dedupes
            # against any deferred callback that also raced here.
            if not self._is_fully_connected():
                self._ensure_retry_scheduled(end_mono)

    async def _async_update_data(self):
        """Fetch parameters from API endpoint.

        This is the place to pre-process the parameters to lookup tables
        so entities can quickly look up their parameters.
        """
        try:
            # BUG-24 — the coordinator tick is the sole retry driver.
            # Runs before the normal ready-branch so a healing network is
            # picked up as soon as the backoff elapses.
            # BUG-24 (follow-up) — monotonic clock so wall-clock jumps
            # (NTP correction, DST) cannot skip retries or fire them
            # early.
            await self._maybe_reconnect(time.monotonic())

            api_connected = self._api.status == ConnectivityStatus.CONNECTED
            aws_client_connected = (
                self._aws_client.status == ConnectivityStatus.CONNECTED
            )

            is_ready = api_connected and aws_client_connected

            if is_ready:
                now = datetime.now().timestamp()

                if now - self._last_update_api >= UPDATE_API_INTERVAL.total_seconds():
                    await self._api.update()

                    self._last_update_api = now

                if now - self._last_update_ws >= UPDATE_WS_INTERVAL.total_seconds():
                    await self._aws_client.update()

                    self._last_update_ws = now

            # HARD-11 — `_set_system_status_details` is now called every tick
            # so the optimistic-overlay TTL (and the pause-guard cap) fire even
            # while the firmware is silent or the connection is down. The
            # existing systemState-shape early-return inside keeps the
            # pre-HARD-11 behaviour for `_system_details` updates.
            self._set_system_status_details()

            self._refresh_next_scheduled_data()

            return {}

        except Exception as err:
            raise UpdateFailed(f"Error communicating with API: {err}")

    def _build_data_mapping(self):
        data_mapping = {
            slugify(DATA_KEY_STATUS): self._get_status_data,
            slugify(DATA_KEY_RSSI): self._get_rssi_data,
            slugify(DATA_KEY_NETWORK_NAME): self._get_network_name_data,
            slugify(DATA_KEY_CLEAN_MODE): self._get_clean_mode_data,
            slugify(DATA_KEY_DESIRED_CLEAN_MODE): self._get_desired_clean_mode_data,
            slugify(DATA_KEY_POWER_SUPPLY_STATUS): self._get_power_supply_status_data,
            slugify(DATA_KEY_ROBOT_STATUS): self._get_robot_status_data,
            slugify(DATA_KEY_ROBOT_TYPE): self._get_robot_type_data,
            slugify(DATA_KEY_BUSY): self._get_busy_data,
            slugify(DATA_KEY_CYCLE_COUNT): self._get_cycle_count_data,
            slugify(DATA_KEY_VACUUM): self._get_vacuum_data,
            slugify(DATA_KEY_REMOTE): self._get_remote_data,
            slugify(DATA_KEY_LED_MODE): self._get_led_mode_data,
            slugify(DATA_KEY_LED): self._get_led_data,
            slugify(DATA_KEY_LED_INTENSITY): self._get_led_intensity_data,
            slugify(DATA_KEY_FILTER_STATUS): self._get_filter_status_data,
            slugify(DATA_KEY_CYCLE_TIME): self._get_cycle_time_data,
            slugify(DATA_KEY_CYCLE_TIME_LEFT): self._get_cycle_time_left_data,
            slugify(DATA_KEY_AWS_BROKER): self._get_aws_broker_data,
            slugify(DATA_KEY_ROBOT_ERROR): self._get_robot_error_data,
            slugify(DATA_KEY_PWS_ERROR): self._get_pws_error_data,
            slugify(DATA_KEY_BATTERY): self._get_battery_data,
            slugify(DATA_KEY_NEXT_SCHEDULED_RUN): self._get_next_scheduled_run_data,
            slugify(DATA_KEY_NEXT_SCHEDULED_MODE): self._get_next_scheduled_mode_data,
            slugify(
                DATA_KEY_NEXT_SCHEDULED_CYCLE_TIME
            ): self._get_next_scheduled_cycle_time_data,
            slugify(DYNAMIC_DESCRIPTION_TEMPERATURE): self._get_temperature_data,
        }

        for clean_mode in list(CleanModes):
            key = get_clean_mode_cycle_time_key(CleanModes(clean_mode))

            data_mapping[key] = self._get_clean_mode_cycle_time_data

        self._data_mapping = data_mapping

        _LOGGER.debug(f"Data retrieval mapping created, Mapping: {self._data_mapping}")

    def get_data(self, entity_description: EntityDescription) -> dict | None:
        result = None

        try:
            handler = self._data_mapping.get(entity_description.key)

            if handler is None:
                _LOGGER.error(
                    f"Handler was not found for {entity_description.key}, Entity Description: {entity_description}"
                )

            else:
                if self._system_details.is_updated:
                    result = handler(entity_description)

        except Exception as ex:
            exc_type, exc_obj, tb = sys.exc_info()
            line_number = tb.tb_lineno

            _LOGGER.error(
                f"Failed to extract data for {entity_description}, Error: {ex}, Line: {line_number}"
            )

        return result

    def get_device_action(
        self, entity_description: EntityDescription, action_key: str
    ) -> Callable:
        device_data = self.get_data(entity_description)
        actions = device_data.get(ATTR_ACTIONS)
        async_action = actions.get(action_key)

        return async_action

    def _get_status_data(self, _entity_description) -> dict | None:
        # HARD-11 — overlay wins while armed. Pure read; the reconcile in
        # `_set_system_status_details` is the only path that clears it.
        state = self._optimistic_statut or self._system_details.calculated_state

        result = {
            ATTR_STATE: None if state is None else state.lower(),
            ATTR_ATTRIBUTES: self._system_details.data,
        }

        return result

    def _get_rssi_data(self, _entity_description) -> dict | None:
        debug = self.aws_data.get(DATA_SECTION_DEBUG, {})
        state = debug.get(DATA_DEBUG_WIFI_RSSI, 0)

        result = {ATTR_STATE: state}

        return result

    def _get_temperature_data(self, _entity_description) -> dict | None:
        dynamic = self.aws_data.get(DATA_SECTION_DYNAMIC, {})
        iot_response = dynamic.get(DYNAMIC_TYPE_IOT_RESPONSE, {})
        temperature_int = iot_response.get(DYNAMIC_DESCRIPTION_TEMPERATURE, 0)

        state_str = str(temperature_int)
        state_str_fixed = f"{state_str[:2]}.{state_str[2:].ljust(2, '0')}"
        state = float(state_str_fixed)

        result = {ATTR_STATE: state}

        return result

    def _get_network_name_data(self, _entity_description) -> dict | None:
        wifi = self.aws_data.get(DATA_SECTION_WIFI, {})
        net_name = wifi.get(DATA_WIFI_NETWORK_NAME)

        result = {ATTR_STATE: net_name}

        return result

    def _get_clean_mode_data(self, _entity_description) -> dict | None:
        # HARD-13 — read-only mirror of the firmware-reported mode. Returns
        # None until the first cleaningMode shadow lands; BUG-16 already
        # gates entity availability on `has_real_data`, so the None should
        # not surface in practice.
        cycle_info = self.aws_data.get(DATA_SECTION_CYCLE_INFO, {})
        cleaning_mode = cycle_info.get(DATA_CYCLE_INFO_CLEANING_MODE, {})
        mode = cleaning_mode.get(ATTR_MODE)

        return {ATTR_STATE: mode}

    def _get_desired_clean_mode_data(self, _entity_description) -> dict | None:
        # HARD-13 — writable select backed by the staged pick.
        # `_desired_clean_mode` is seeded from / reconciled with the reported
        # mode in `_reconcile_desired_clean_mode`, so this falls through to
        # the firmware's reported mode whenever nothing is staged.
        mode = self._desired_clean_mode

        if mode is None:
            cycle_info = self.aws_data.get(DATA_SECTION_CYCLE_INFO, {})
            cleaning_mode = cycle_info.get(DATA_CYCLE_INFO_CLEANING_MODE, {})
            mode = cleaning_mode.get(ATTR_MODE, CleanModes.REGULAR)

        result = {
            ATTR_STATE: mode,
            ATTR_ACTIONS: {SERVICE_SELECT_OPTION: self._set_cleaning_mode},
            # FEAT-03 — expose visible_modes so a preferences save
            # triggers `_handle_coordinator_update` to see
            # `_data != new_data` and re-emit state. Without this, the
            # base entity short-circuits on data equality and the
            # `options` @property is never re-read → the frontend keeps
            # the stale pick list.
            "_visible_modes": self._visible_modes,
        }

        return result

    def _get_power_supply_status_data(self, _entity_description) -> dict | None:
        state = self._system_details.power_unit_state

        result = {ATTR_STATE: None if state is None else state.lower()}

        return result

    def _get_robot_status_data(self, _entity_description) -> dict | None:
        state = self._system_details.robot_state

        result = {ATTR_STATE: None if state is None else state.lower()}

        return result

    def _get_robot_type_data(self, _entity_description) -> dict | None:
        state = self._system_details.robot_type

        result = {ATTR_STATE: state}

        return result

    def _get_busy_data(self, _entity_description) -> dict | None:
        is_on = self._system_details.is_busy

        result = {ATTR_IS_ON: is_on}

        return result

    def _get_cycle_count_data(self, _entity_description) -> dict | None:
        state = self._system_details.turn_on_count

        result = {ATTR_STATE: state}

        return result

    def _get_vacuum_data(self, _entity_description) -> dict | None:
        # BUG-13 (write-on-commit) — same source of truth as the clean-mode
        # select: the staged pick, falling back to reported.
        mode = self._desired_clean_mode

        if mode is None:
            cycle_info = self.aws_data.get(DATA_SECTION_CYCLE_INFO, {})
            cleaning_mode = cycle_info.get(DATA_CYCLE_INFO_CLEANING_MODE, {})
            mode = cleaning_mode.get(ATTR_MODE, CleanModes.REGULAR)

        # HARD-11 — overlay wins while armed (only set on Run / pickup —
        # Stop is honest-linger and does not touch this slot). Pure read.
        state = self._optimistic_vacuum_state or self._system_details.vacuum_state

        result = {
            ATTR_STATE: state,
            ATTR_ATTRIBUTES: {ATTR_MODE: mode},
            ATTR_ACTIONS: {
                SERVICE_START: self._vacuum_start,
                SERVICE_PAUSE: self._vacuum_pause,
                SERVICE_SET_FAN_SPEED: self._set_cleaning_mode,
                SERVICE_LOCATE: self._vacuum_locate,
                SERVICE_RETURN_TO_BASE: self._pickup,
            },
            # FEAT-03 — see comment in `_get_desired_clean_mode_data`;
            # without this key the vacuum's `fan_speed_list` @property
            # is never re-read after a preferences save.
            "_visible_modes": self._visible_modes,
        }

        return result

    def _get_remote_data(self, _entity_description) -> dict | None:
        state = self._system_details.is_manual_mode
        activity = self._system_details.activity

        result = {
            ATTR_STATE: state,
            ATTR_ATTRIBUTES: {ATTR_ACTIVITY: activity},
            ATTR_ACTIONS: {
                SERVICE_SEND_COMMAND: self._set_joystick_mode,
                SERVICE_TURN_OFF: self._exit_joystick_mode,
            },
        }

        return result

    def _get_led_mode_data(self, _entity_description) -> dict | None:
        led = self.aws_data.get(DATA_SECTION_LED, {})
        led_mode = str(led.get(DATA_LED_MODE, LED_MODE_BLINKING))

        result = {
            ATTR_STATE: led_mode,
            ATTR_ICON: ICON_LED_MODES.get(led_mode, LED_MODE_ICON_DEFAULT),
            ATTR_ACTIONS: {SERVICE_SELECT_OPTION: self._set_led_mode},
        }

        return result

    def _get_led_data(self, _entity_description) -> dict | None:
        led = self.aws_data.get(DATA_SECTION_LED, {})
        led_enable = led.get(DATA_LED_ENABLE, DEFAULT_ENABLE)

        result = {
            ATTR_IS_ON: led_enable,
            ATTR_ACTIONS: {
                SERVICE_TURN_ON: self._set_led_enabled,
                SERVICE_TURN_OFF: self._set_led_disabled,
            },
        }

        return result

    def _get_led_intensity_data(self, _entity_description) -> dict | None:
        led = self.aws_data.get(DATA_SECTION_LED, {})
        led_intensity = led.get(DATA_LED_INTENSITY, DEFAULT_LED_INTENSITY)

        result = {
            ATTR_STATE: led_intensity,
            ATTR_ACTIONS: {
                SERVICE_SET_VALUE: self._set_led_intensity,
            },
        }

        return result

    def _get_clean_mode_cycle_time_data(self, entity_description) -> dict | None:
        key = entity_description.key
        key_parts = key.split("_")
        clean_mode_str = key_parts[len(key_parts) - 1]
        clean_mode = CleanModes(clean_mode_str)
        state = self.config_manager.get_clean_cycle_time(clean_mode)

        result = {
            ATTR_STATE: state,
            ATTR_ACTIONS: {
                SERVICE_SET_VALUE: self._set_clean_mode_cycle_time_data,
            },
        }

        return result

    def _get_filter_status_data(self, _entity_description) -> dict | None:
        filter_bag_indication = self.aws_data.get(
            DATA_SECTION_FILTER_BAG_INDICATION, {}
        )
        filter_state = filter_bag_indication.get(CONF_STATE, -1)
        reset_fbi = filter_bag_indication.get(
            DATA_FILTER_BAG_INDICATION_RESET_FBI, False
        )
        state = None

        for state_name in FILTER_BAG_STATUS:
            state_range = FILTER_BAG_STATUS.get(state_name)
            state_range_min = int(state_range[0])
            state_range_max = int(state_range[1])

            is_in_range = state_range_max >= filter_state >= state_range_min

            if is_in_range:
                state = state_name
                break

        result = {
            ATTR_STATE: state,
            ATTR_ATTRIBUTES: {
                ATTR_RESET_FBI: reset_fbi,
                ATTR_STATUS: filter_state,
            },
            ATTR_ICON: FILTER_BAG_ICONS.get(filter_state),
        }

        return result

    def _get_cycle_time_data(self, _entity_description) -> dict | None:
        cycle_info = self.aws_data.get(DATA_SECTION_CYCLE_INFO, {})
        cleaning_mode = cycle_info.get(DATA_CYCLE_INFO_CLEANING_MODE, {})

        cycle_time_minutes = cleaning_mode.get(
            DATA_CYCLE_INFO_CLEANING_MODE_DURATION, 0
        )

        attributes = {}

        if cycle_time_minutes == 0:
            cycle_time_hours = None

        else:
            cycle_time = timedelta(minutes=cycle_time_minutes)
            cycle_time_hours = int(cycle_time / timedelta(hours=1))

            cycle_start_time_ts = cycle_info.get(
                DATA_CYCLE_INFO_CLEANING_MODE_START_TIME, 0
            )
            cycle_start_time = self._get_date_time_from_timestamp(cycle_start_time_ts)

            attributes[ATTR_START_TIME] = cycle_start_time

        icon = self._get_hour_icon(cycle_time_hours)

        result = {
            ATTR_STATE: cycle_time_minutes,
            ATTR_ATTRIBUTES: attributes,
            ATTR_ICON: icon,
        }

        return result

    def _get_cycle_time_left_data(self, _entity_description) -> dict | None:
        calculated_state = self._system_details.calculated_state

        cycle_info = self.aws_data.get(DATA_SECTION_CYCLE_INFO, {})
        cleaning_mode = cycle_info.get(DATA_CYCLE_INFO_CLEANING_MODE, {})

        cycle_time = cleaning_mode.get(DATA_CYCLE_INFO_CLEANING_MODE_DURATION, 0)
        cycle_time_in_seconds = cycle_time * 60

        cycle_start_time_ts = cycle_info.get(
            DATA_CYCLE_INFO_CLEANING_MODE_START_TIME, 0
        )
        cycle_start_time = self._get_date_time_from_timestamp(cycle_start_time_ts)

        now = datetime.now()
        now_ts = now.timestamp()

        expected_cycle_end_time_ts = cycle_time_in_seconds + cycle_start_time_ts
        expected_cycle_end_time = self._get_date_time_from_timestamp(
            expected_cycle_end_time_ts
        )

        state = 0
        seconds_left = 0
        state_hours = None

        if (
            calculated_state == CalculatedState.CLEANING
            and expected_cycle_end_time_ts > now_ts
        ):
            seconds_left = expected_cycle_end_time_ts - now_ts

        if seconds_left > 0:
            state = timedelta(seconds=seconds_left).total_seconds()
            state_hours = int((expected_cycle_end_time - now) / timedelta(hours=1))

        icon = self._get_hour_icon(state_hours)

        result = {
            ATTR_STATE: state,
            ATTR_ATTRIBUTES: {
                ATTR_START_TIME: cycle_start_time,
                ATTR_EXPECTED_END_TIME: expected_cycle_end_time,
            },
            ATTR_ICON: icon,
        }

        return result

    def _get_aws_broker_data(self, _entity_description) -> dict | None:
        is_on = self._aws_client.status == ConnectivityStatus.CONNECTED

        result = {
            ATTR_IS_ON: is_on,
            ATTR_ATTRIBUTES: {ATTR_STATUS: self._aws_client.status},
        }

        return result

    def _get_robot_error_data(self, entity_description) -> dict | None:
        result = self._get_error_code(entity_description, DATA_SECTION_ROBOT_ERROR)

        return result

    def _get_pws_error_data(self, entity_description) -> dict | None:
        result = self._get_error_code(entity_description, DATA_SECTION_PWS_ERROR)

        return result

    def _get_battery_data(self, _entity_description) -> dict | None:
        # Pool cleaning robots are always connected to power, so battery is always 100%
        state = 100

        result = {ATTR_STATE: state}

        return result

    def _refresh_next_scheduled_data(self) -> None:
        """Recompute the next-scheduled-run dict once per update tick.

        Stored in ``self._next_scheduled_data`` so the three next-scheduled
        sensors (run / mode / cycle time) read the same value and stay
        atomically consistent at slot boundaries (FEAT-04).
        """
        data = self.aws_data

        system_state = data.get(DATA_SECTION_SYSTEM_STATE, {})

        self._next_scheduled_data = compute_next_scheduled_run(
            data.get(DATA_SECTION_WEEKLY_SETTINGS),
            data.get(DATA_SECTION_DELAY),
            system_state.get(DATA_SYSTEM_STATE_TIME_ZONE_NAME),
            system_state.get(DATA_SYSTEM_STATE_TIME_ZONE),
            dt_util.utcnow(),
        )

    def _get_next_scheduled_run_data(self, _entity_description) -> dict | None:
        computed = self._next_scheduled_data

        if computed is None:
            return {ATTR_STATE: None, ATTR_ATTRIBUTES: {}}

        return {
            ATTR_STATE: computed[ATTR_NSR_STATE],
            ATTR_ATTRIBUTES: {
                ATTR_NSR_CLEANING_MODE: computed[ATTR_NSR_CLEANING_MODE],
                ATTR_NSR_SOURCE: computed[ATTR_NSR_SOURCE],
                ATTR_NSR_DAY_OF_WEEK: computed[ATTR_NSR_DAY_OF_WEEK],
            },
        }

    def _get_next_scheduled_mode_data(self, _entity_description) -> dict | None:
        computed = self._next_scheduled_data

        if computed is None:
            return {ATTR_STATE: None}

        return {ATTR_STATE: computed[ATTR_NSR_CLEANING_MODE]}

    def _get_next_scheduled_cycle_time_data(self, _entity_description) -> dict | None:
        # The next-scheduled cycle's duration is the carried-forward
        # ``reported.cycleInfo.cleaningMode.cycleTime``, persisted across
        # PWS reboots (BUG-18 / #88). The ``cleaningModes`` catalog is a
        # firmware-side follower that resets at every PWS reboot, so it
        # is the wrong source.
        if self._next_scheduled_data is None:
            return {ATTR_STATE: None}

        cycle_info = self.aws_data.get(DATA_SECTION_CYCLE_INFO, {})
        cleaning_mode = cycle_info.get(DATA_CYCLE_INFO_CLEANING_MODE, {})
        value = cleaning_mode.get(DATA_CYCLE_INFO_CLEANING_MODE_DURATION)

        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            return {ATTR_STATE: None}

        return {ATTR_STATE: value}

    def _get_error_code(self, entity_description, data_section_key) -> dict | None:
        data = self.aws_data

        system_state = data.get(DATA_SECTION_SYSTEM_STATE, {})
        turn_on_count = system_state.get(DATA_SYSTEM_STATE_TURN_ON_COUNT, 0)

        error_section = data.get(data_section_key, {})
        error_code = error_section.get(DATA_ERROR_CODE, 0)
        error_turn_on_count = error_section.get(DATA_ERROR_TURN_ON_COUNT, 0)

        state = 0

        if error_turn_on_count == turn_on_count:
            state = error_code

        icon = entity_description.icon

        if state not in ERROR_CLEAN_CODES:
            icon = f"{icon}-alert"

        result = {ATTR_STATE: state, ATTR_ICON: icon}

        return result

    async def _set_cleaning_mode(
        self, _entity_description: EntityDescription, fan_speed
    ):
        current = self._desired_clean_mode
        _LOGGER.debug(f"Change cleaning mode, State: {current}, New: {fan_speed}")

        if current == fan_speed:
            return

        # HARD-12 (#104) — picking a mode is a UI-level affordance, never
        # a robot command. We stage the choice in coordinator memory and
        # propagate it to entities; we do not write `desired.cleaningMode.mode`
        # to AWS regardless of the robot's state.
        #
        # The firmware only hears the mode at Run: `_vacuum_start` reads
        # `_desired_clean_mode` and writes via `aws_client.set_cleaning_mode`,
        # and the BUG-08 chain in `_on_update_accepted` then delivers the
        # per-mode `cycleTime`. To apply a new mode mid-cycle the operator
        # stops then starts.
        #
        # Together with the BUG-13 pivot (write-on-commit, #100) this also
        # closes the running-path side effects documented in PR #102: no
        # transient `init` re-entry, no silent cycleTime rewrite of an
        # in-flight cycle. The Maytronics app retains its mid-cycle swap
        # capability and `_reconcile_desired_clean_mode` adapts to it.
        self._desired_clean_mode = fan_speed
        self.async_update_listeners()

    async def _set_led_mode(self, _entity_description: EntityDescription, option: str):
        _LOGGER.debug(f"Change led mode, New: {option}")

        value = int(option)

        self._aws_client.set_led_mode(value)

    async def _set_led_enabled(self, _entity_description: EntityDescription):
        _LOGGER.debug("Enable LED light")

        self._aws_client.set_led_enabled(True)

    async def _set_led_disabled(self, _entity_description: EntityDescription):
        _LOGGER.debug("Disable LED light")

        self._aws_client.set_led_enabled(False)

    async def _set_led_intensity(
        self, _entity_description: EntityDescription, intensity: int
    ):
        self._aws_client.set_led_intensity(intensity)

    async def _set_clean_mode_cycle_time_data(
        self, entity_description: EntityDescription, cycle_time: int
    ):
        key_parts = entity_description.key.split("_")
        clean_mode_str = key_parts[len(key_parts) - 1]
        clean_mode = CleanModes(clean_mode_str)

        await self.config_manager.update_clean_cycle_time(clean_mode, cycle_time)

    async def _pickup(self, _entity_description: EntityDescription):
        _LOGGER.debug("Pickup vacuum")

        # HARD-11 — share the start-serialization guard with `_vacuum_start`:
        # `pickup` writes via the same `set_cleaning_mode` primitive and
        # therefore carries the same BUG-19 / BUG-20 race risk.
        if self._is_start_guard_active():
            _LOGGER.warning(
                "Pickup refused: previous pause not yet acknowledged by firmware"
            )
            return

        # HARD-11 v1.3 — a permitted start makes the guard moot. Drop the
        # bookkeeping immediately so no later tick of `_reconcile_pause_guard`
        # can wipe this start's fresh overlay via the TTL path (closes the
        # sub-tick race between block-lift and the next reconcile tick).
        self._pause_issued_at = None

        self._arm_optimistic_start(VacuumActivity.RETURNING)
        self._aws_client.pickup()
        self.async_update_listeners()

    async def _vacuum_start(self, _entity_description: EntityDescription, _state):
        _LOGGER.debug("Start vacuum")

        # HARD-11 — refuse a new start while the previous start→stop
        # mini-cycle is unacknowledged. Load-bearing guard derived from the
        # E5a reactive-stop session (F9/F11): a fresh `set_cleaning_mode`
        # firing before the firmware acknowledged the prior `pause()`
        # triggers the BUG-19 silent restart and the BUG-20 stuck-init
        # cascade.
        if self._is_start_guard_active():
            _LOGGER.warning(
                "Start refused: previous pause not yet acknowledged by firmware"
            )
            return

        # HARD-11 v1.3 — a permitted Run makes the guard moot. Drop the
        # bookkeeping immediately so no later tick of `_reconcile_pause_guard`
        # can wipe this Run's fresh overlay via the TTL path (closes the
        # sub-tick race between block-lift and the next reconcile tick).
        self._pause_issued_at = None

        # BUG-13 (write-on-commit) — Run commits the staged pick. Falls back
        # to the firmware's reported mode (and finally REGULAR) only when
        # nothing is staged, which should be rare in practice — the staged
        # field is seeded from reported on the first refresh.
        mode = self._desired_clean_mode

        if mode is None:
            cycle_info = self.aws_data.get(DATA_SECTION_CYCLE_INFO, {})
            cleaning_mode = cycle_info.get(DATA_CYCLE_INFO_CLEANING_MODE, {})
            mode = cleaning_mode.get(ATTR_MODE, CleanModes.REGULAR)

        # HARD-11 — overlay armed before the AWS write so the entities
        # observe the optimistic state on the same refresh cycle as the
        # listener push.
        target = (
            VacuumActivity.RETURNING
            if mode == CleanModes.PICKUP
            else VacuumActivity.CLEANING
        )
        self._arm_optimistic_start(target)

        self._aws_client.set_cleaning_mode(mode)
        self.async_update_listeners()

    async def _vacuum_pause(self, _entity_description: EntityDescription, state):
        is_idle_state = state == VacuumActivity.DOCKED
        _LOGGER.debug(f"Pause vacuum, State: {state}")

        if is_idle_state:
            return

        # HARD-11 — honest-linger on Stop: arm only the statut overlay
        # (`pausingPending`) so the chip acknowledges the click; the
        # vacuum activity follows the real state, transitioning to
        # `docked` only on the firmware's `pwsState=off` echo.
        self._arm_optimistic_pause()
        self._pause_issued_at = time.monotonic()
        self._aws_client.pause()
        self.async_update_listeners()

    async def _vacuum_locate(self, entity_description: EntityDescription):
        led_light_entity = self._get_led_data(None)

        led_light_state = led_light_entity.get(CONF_STATE)

        if led_light_state:
            _LOGGER.warning(
                "Locate will not run as the LED currently on, "
                "you should see the robot"
            )

        else:
            _LOGGER.debug("Locate robot")

            await self._config_manager.update_is_locating(True)
            await self._set_led_enabled(entity_description)

    async def _exit_joystick_mode(self, _entity_description: EntityDescription):
        _LOGGER.debug("Exit joystick mode")

        if self._system_details.is_manual_mode:
            self._aws_client.exit_joystick_mode()

        else:
            _LOGGER.error(
                "Robot cannot exit from joystick mode, "
                f"Manual Mode: {self._system_details.is_manual_mode}, "
                f"State: {self._system_details.vacuum_state}"
            )

    async def _set_joystick_mode(
        self, _entity_description: EntityDescription, activity: str
    ):
        _LOGGER.debug("Set joystick mode")

        if self._system_details.is_active or self._system_details.is_manual_mode:
            direction = JoystickDirection(activity)

            self._aws_client.set_joystick_mode(direction)

        else:
            _LOGGER.error(
                "Robot cannot be set to joystick mode, "
                f"State: {self._system_details.vacuum_state}"
            )

    def _set_system_status_details(self):
        aws_data = self.aws_data
        has_system_state = bool(aws_data.get(DATA_SECTION_SYSTEM_STATE))

        if has_system_state:
            self._has_real_data = True
            updated = self._system_details.update(aws_data)

            if updated:
                _LOGGER.debug(
                    f"System status recalculated, "
                    f"Calculated State: {self._system_details.calculated_state}, "
                    f"Main Unit State: {self._system_details.power_unit_state}, "
                    f"Robot State: {self._system_details.robot_state}"
                )

        # HARD-11 — overlay and pause-guard reconcile run every tick, after
        # the fresh shadow has been applied (so the firmware-leaves-origin
        # check sees this tick's data) and before the mode reconcile. Both
        # are pure mutations against the current `_system_details`
        # snapshot; the per-tick refresh propagates the result to entities.
        # They run even when no `systemState` is present so TTL fallback
        # and guard cap fire while the firmware is silent.
        self._reconcile_optimistic_overlay()
        self._reconcile_pause_guard()

        if has_system_state:
            self._reconcile_desired_clean_mode()

        # HARD-11 v1.1 — update the edge tracker at the end of the tick so
        # the *next* tick's `_reconcile_pause_guard` can detect the entering
        # transition into `holdWeekly`. Snapshot the calculated_state we
        # just decided was current — gated on `has_real_data` so a tick
        # that did not apply a fresh shadow does not poison the edge with
        # a stale `None`.
        if self._has_real_data:
            self._last_observed_calculated_state = self._system_details.calculated_state

    def _reconcile_desired_clean_mode(self):
        """BUG-13 (write-on-commit) — seed at startup and reconcile on foreign
        change.

        Tracks the firmware-reported mode in `_last_seen_reported_clean_mode`
        so a real change (since the previous refresh) can be told from a
        steady-state echo. Two cases:

        * First refresh after coordinator init (`_last_seen is None`): take
          the reported mode as the baseline; seed `_desired` from it only if
          it is itself unset, so a pick that landed before the first refresh
          is preserved.
        * Subsequent refresh with `reported != _last_seen`: the firmware
          mode just changed. The only HA-initiated path that moves `reported`
          is a write that itself wrote the same value to `_desired` first
          (Run from the staged value, or the running live-swap), so our own
          echoes converge on `_desired` and the overwrite is idempotent. A
          divergence therefore indicates a foreign initiator (Maytronics
          app, scheduler). Contract: `desired := reported` ("desired
          becomes the current one").

        The load-bearing invariant is "reconcile is gated on `reported`
        movement", not "every set of `_desired` writes to the firmware" —
        the docked-pick path sets `_desired` and writes nothing, but that
        path also does not move `reported`, so it never enters this method
        at all. Explicit `_event_is_ours` plumbing from `aws_client` would
        be equivalent on the cases that DO move `reported`; the value-based
        check is kept for locality.
        """
        cycle_info = self.aws_data.get(DATA_SECTION_CYCLE_INFO, {})
        cleaning_mode = cycle_info.get(DATA_CYCLE_INFO_CLEANING_MODE, {})
        reported = cleaning_mode.get(ATTR_MODE)

        if reported is None:
            return

        if self._last_seen_reported_clean_mode is None:
            self._last_seen_reported_clean_mode = reported
            if self._desired_clean_mode is None:
                self._desired_clean_mode = reported
            return

        if reported != self._last_seen_reported_clean_mode:
            self._last_seen_reported_clean_mode = reported
            self._desired_clean_mode = reported

    # ------------------------------------------------------------------ #
    # HARD-11 — optimistic overlay and start-serialization guard         #
    # ------------------------------------------------------------------ #

    def _arm_optimistic_start(self, target_vacuum: VacuumActivity) -> None:
        """Arm the overlay for a Run / pickup.

        Sets the click-time vacuum target (CLEANING or RETURNING) and the
        ``startingPending`` statut acknowledgement; records the click-time
        real vacuum state as origin so the reconcile can detect the
        firmware moving away from where it was at the click; refreshes the
        TTL.
        """
        self._optimistic_vacuum_state = target_vacuum
        self._optimistic_statut = CalculatedState.STARTING_PENDING
        self._optimistic_origin_vacuum_state = self._system_details.vacuum_state
        self._optimistic_deadline = time.monotonic() + _OPTIMISTIC_TTL_S

    def _arm_optimistic_pause(self) -> None:
        """Arm the overlay for a Stop.

        Honest-linger: leave the vacuum overlay untouched (real ``cleaning``
        keeps showing until the ``pwsState=off`` echo) and add only the
        ``pausingPending`` statut acknowledgement. Origin is the current
        real vacuum state at the click (typically ``cleaning``) — the
        reconcile clears once the firmware moves away from it. TTL is
        refreshed even when a Run overlay was already armed: the click is
        the new reference instant.
        """
        self._optimistic_statut = CalculatedState.PAUSING_PENDING
        if self._optimistic_origin_vacuum_state is None:
            self._optimistic_origin_vacuum_state = self._system_details.vacuum_state
        self._optimistic_deadline = time.monotonic() + _OPTIMISTIC_TTL_S

    def _clear_optimistic_overlay(self) -> None:
        self._optimistic_vacuum_state = None
        self._optimistic_statut = None
        self._optimistic_origin_vacuum_state = None
        self._optimistic_deadline = None

    def _reconcile_optimistic_overlay(self) -> None:
        """Clear the overlay when it is no longer load-bearing.

        Conditions (any one): TTL expired; firmware reports ERROR;
        firmware vacuum state moved away from the click-time origin (the
        Run or Stop reached the firmware, whatever the exact target). The
        getters stay pure reads — the reconcile is the *only* place that
        clears.
        """
        if self._optimistic_deadline is None:
            return

        now = time.monotonic()

        if now >= self._optimistic_deadline:
            _LOGGER.debug("HARD-11 — optimistic overlay TTL expired, clearing")
            self._clear_optimistic_overlay()
            return

        if not self._has_real_data:
            # Without a systemState snapshot we cannot reason about the
            # firmware's authority; only TTL applies.
            return

        real_vacuum = self._system_details.vacuum_state

        if real_vacuum == VacuumActivity.ERROR:
            _LOGGER.debug("HARD-11 — firmware reports ERROR, clearing overlay")
            self._clear_optimistic_overlay()
            return

        origin = self._optimistic_origin_vacuum_state
        if origin is not None and real_vacuum != origin:
            _LOGGER.debug(
                "HARD-11 — firmware moved %s → %s, clearing overlay",
                origin,
                real_vacuum,
            )
            self._clear_optimistic_overlay()

    def _reconcile_pause_guard(self) -> None:
        """Clear the start-serialization guard when the pause flow is settled.

        Two clear conditions: TTL timeout (the edge may never arrive if
        the firmware suppressed both the start *and* the pause, so the
        system never left its at-rest calculated_state); or the *entering*
        transition into any of the rest states the firmware emits when
        idle (``HOLD_WEEKLY`` / ``HOLD_DELAY`` / ``OFF`` —
        :data:`_PAUSE_ACK_REST_STATES`). Hardcoding ``HOLD_WEEKLY`` alone
        would tie the guard to robots running an active weekly schedule.

        When the guard clears, the optimistic overlay is also cleared in
        the same step — that is the only signal that bounds the
        ``Run → Stop-in-gap → firmware-stays-docked`` UX, because the
        vacuum overlay's origin-moved check would never fire in that case
        (``real == origin == DOCKED`` throughout). The TTL therefore
        doubles as the worst-case overlay-revert horizon in that scenario
        (~15 s instead of the overlay TTL's 120 s).
        """
        if self._pause_issued_at is None:
            return

        cleared_reason: str | None = None
        elapsed = time.monotonic() - self._pause_issued_at

        if elapsed >= _PAUSE_GUARD_TTL_S:
            cleared_reason = "ttl"
        elif self._has_real_data:
            current = self._system_details.calculated_state
            prev = self._last_observed_calculated_state
            entering_rest = (
                prev is not None
                and prev not in _PAUSE_ACK_REST_STATES
                and current in _PAUSE_ACK_REST_STATES
            )
            if entering_rest:
                cleared_reason = f"rest edge ({current})"

        if cleared_reason is not None:
            _LOGGER.debug(
                "HARD-11 — pause guard cleared (%s); dropping overlay too",
                cleared_reason,
            )
            self._pause_issued_at = None
            # Tie the optimistic overlay clear to the guard resolution. In
            # the Run → Stop-in-gap case the vacuum overlay's origin-moved
            # check cannot fire (real never left the click-time origin), so
            # the guard's edge / TTL is the only path that bounds the
            # `cleaning + Stopping…` lie when the firmware suppressed the
            # start. When no overlay is armed, this is a no-op.
            self._clear_optimistic_overlay()

    def _is_start_guard_active(self) -> bool:
        if self._pause_issued_at is None:
            return False
        return time.monotonic() - self._pause_issued_at < _PAUSE_GUARD_TTL_S

    @property
    def has_real_data(self) -> bool:
        """Return ``True`` once a payload carrying ``systemState`` has been
        applied. BUG-16: until then, every entity's ``available`` stays
        ``False`` so the initial ``async_add_entities`` state write cannot
        publish stale defaults (e.g. ``docked``)."""
        return self._has_real_data

    @staticmethod
    def _get_date_time_from_timestamp(timestamp):
        result = datetime.fromtimestamp(timestamp)

        return result

    @staticmethod
    def _get_hour_icon(current_hour: int | None) -> str:
        if current_hour is None:
            icon = CLOCK_HOURS_NONE

        else:
            if current_hour > 11:
                current_hour = current_hour - 12

            if current_hour >= len(CLOCK_HOURS_TEXT):
                current_hour = 0

            hour_text = CLOCK_HOURS_TEXT[current_hour]
            icon = "".join([CLOCK_HOURS_ICON, hour_text])

        return icon
