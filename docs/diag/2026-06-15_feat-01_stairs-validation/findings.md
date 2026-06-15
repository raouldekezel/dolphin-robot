# FEAT-01 — `stairs` (Full Coverage) mode validation in vivo after PR #50 re-merge

## TL;DR

PR #50 (FEAT-01 re-applied) is functionally correct end-to-end on the operator's
Dolphin S2000: the firmware accepts the mode, the integration localizes the
sensor and the vacuum `fan_speed` picker to "Couverture complète", and the
default cycle time of 150 min comes through after the storage residue from the
previous session's in-place patch is purged. Two pre-existing bugs (BUG-13 and
BUG-14) are reconfirmed in vivo during the same run, and one new label-related
bug (BUG-15) is uncovered as a direct consequence of PR #50 missing the
`entity.number.cycle_time_stairs` translation.

## Context

- **Date:** 2026-06-15 (CEST, UTC+02:00)
- **Robot:** Maytronics Dolphin S2000, motor unit `REDACTED-MUSN`, robot
  family `S4`, firmware `pwsSwVersion=11.0004` / `muSwVersion=9F88`.
- **Fork tag during experiment:** `v1.0.26b3-raoul.2` (re-merged FEAT-01 from
  PR #50, commit `be780e0`). HACS active selector confirmed via the
  presence of `"Full Coverage"` in `translations/en.json`.
- **HA Core:** `2026.1.3` (Docker container `hass` on `intel-nuc`,
  `192.168.0.27`).
- **Pre-experiment state:**
  - `number.<robot>_cycle_time_stairs` storage residue **180** (legacy from
    [session 2026-06-12](../2026-06-12_map-01_stairs-validation/findings.md))
    was reset to 150 by editing `.storage/core.entity_registry` (removed the
    entity row, id `97f68c8d…`) and `.storage/mydolphin_plus.config.json`
    (popped `cycle_time_stairs`), then `docker restart hass`. Backups kept as
    `*.bak-20260615-150050`.
  - `binary_sensor.<robot>_broker_aws = on`, `vacuum.<robot> = docked`,
    `sensor.<robot>_battery = 100`, `binary_sensor.<robot>_weekly_schedule`
    armed (Sunday–Saturday 11:00 → `all`).
  - Loggers `aws_client`, `config_manager`, `rest_api` in DEBUG (persisted in
    `configuration.yaml` since session 2026-06-12).
  - In-place patch of `clean_modes.py` from session 2026-06-12 already
    overwritten by HACS at the raoul.2 upgrade — verified by absence of any
    `clean_modes.py.bak-*` next to the file.

## Actions taken

1. **`01_app-start-stairs`** — operator opens MyDolphin Plus app, taps
   "Couverture complète" preset 3 h. Firmware reports the resulting shadow
   round-trip; HA sensors update.
2. **`02_app-stop-stairs`** — operator taps Stop in the app.
3. **`03_ha-fan-speed-stairs-same-mode`** — operator opens HA vacuum card and
   selects `stairs` ("Couverture complète") again from the `fan_speed` picker,
   even though `vacuum.<robot>.fan_speed` was already showing `stairs` from
   the previous app cycle. The intent reported was "press Run"; the actual
   path observed in logs is a `set_fan_speed` write (see Findings #4).
4. **`04_ha-fan-speed-complete-bug13`** — operator selects `all` ("Complet")
   from the `fan_speed` picker without pressing Run. Cycle starts immediately
   (BUG-13). Operator then presses Stop a few minutes in.

## Timeline

UTC offset throughout: `+02:00` (capture started at 15:17:26 CEST,
ended 15:43:09 CEST).

| Local time   | Event                                                                                                                                                                                                                                                                                                                                       | Evidence                                                                        |
| ------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------- |
| 15:18:10.901 | Integration `Connected. Refresh details` after `docker restart`                                                                                                                                                                                                                                                                             | `01_app-start-stairs.mqtt.log:1`                                                |
| 15:18:43.868 | **Spurious `Set cycle time 150` write from the integration** before any user action — the integration's `config_manager` syncs each `number.<robot>_cycle_time_<mode>` it owns to the firmware shadow on reconnect                                                                                                                          | `01_app-start-stairs.mqtt.log` (search `Set cycle time`)                        |
| 15:18:45.057 | Firmware reports `cleaningMode.mode = stairs`, `cycleTime = 180` (app-side mode-write fully echoed)                                                                                                                                                                                                                                         | `01_app-start-stairs.mqtt.log` (`Updated key cleaning_mode … (None -> stairs)`) |
| 15:18:55.067 | `sensor.<robot>_cycle_time` updates to **180** (app sent 180, not the HA-configured 150 — separate channel)                                                                                                                                                                                                                                 | `01_app-start-stairs.mqtt.log`                                                  |
| 15:19:34.961 | `sensor.<robot>_nombre_de_cycles` increments to 25 (turn-on counter incremented by firmware)                                                                                                                                                                                                                                                | `01_app-start-stairs.mqtt.log`                                                  |
| 15:34:10.918 | App stop — firmware reports `pwsState = holdWeekly`, `cleaningMode.mode = stairs` retained (mode memory survives stop)                                                                                                                                                                                                                      | `02_app-stop-stairs.mqtt.log`                                                   |
| 15:37:48.116 | **`Set cleaning mode {mode: stairs}` from HA** — operator re-selects the (already-current) `stairs` from the picker. Despite being a same-mode write, the integration emits both the mode write and…                                                                                                                                        | `03_ha-fan-speed-stairs-same-mode.mqtt.log:1`                                   |
| 15:37:49.184 | **…`Set cycle time {cycleTime: 150}`** chained ~1.07 s later (BUG-08 chain)                                                                                                                                                                                                                                                                 | `03_ha-fan-speed-stairs-same-mode.mqtt.log` (second `Set` near top)             |
| 15:38:08.123 | Firmware reported `pwsState = on` arrives; HA `vacuum.<robot>` flips to `cleaning`. The reported `cycleTime` **stays at 180** — the firmware silently drops the integration's `cycleTime=150` write because the mode did not actually change firmware-side. This is the substance of **BUG-14**, more precisely than previously documented. | `03_ha-fan-speed-stairs-same-mode.mqtt.log`                                     |
| 15:39:09.108 | Firmware reports `robotState = scanning` → `sensor.<robot>_statut = cleaning`, `sensor.<robot>_etat_du_robot = scanning`, `sensor.<robot>_etat_synthetique = Analyse`. **Delay between HA emit and reported scanning = 61 s** (15:38:08 + 61 s) — this is the operator's "noticeable delay" comment.                                        | `03_ha-fan-speed-stairs-same-mode.mqtt.log`                                     |
| 15:39:29.114 | Operator taps Stop in HA — firmware reports `pwsState = holdWeekly`, `vacuum.<robot> = docked` (single-shot transition, no delay)                                                                                                                                                                                                           | `03_ha-fan-speed-stairs-same-mode.mqtt.log` (end)                               |
| 15:40:11.811 | **`Set cleaning mode {mode: all}` from HA picker** — operator selects "Complet" without pressing Run. Cycle starts immediately (**BUG-13**).                                                                                                                                                                                                | `04_ha-fan-speed-complete-bug13.mqtt.log:1`                                     |
| 15:40:12.900 | **`Set cycle time {cycleTime: 60}`** chained ~1.09 s later (BUG-08). This time the firmware **does** accept the cycleTime, because the mode write is a real delta (`stairs → all`). Reported `cleaningMode.mode = all`, `cycleTime = 60`.                                                                                                   | `04_ha-fan-speed-complete-bug13.mqtt.log`                                       |
| 15:41:06.107 | `nombre_de_cycles` → 27 (Complete cycle counted on start)                                                                                                                                                                                                                                                                                   | `04_ha-fan-speed-complete-bug13.mqtt.log`                                       |
| 15:42:28.943 | Operator taps Stop — firmware → `holdWeekly`, `vacuum.<robot> = docked`. End of session.                                                                                                                                                                                                                                                    | `04_ha-fan-speed-complete-bug13.mqtt.log` (end)                                 |

## Findings

1. **FEAT-01 (PR #50) is correct in vivo on S2000.** `vacuum.<robot>.fan_speed_list`
   exposes `stairs` as the 7th entry, immediately after `pickup` (Python
   `StrEnum` member-insertion order). The sensor `sensor.<robot>_clean_mode` is
   localized to "Couverture complète" in French (and to "Full Coverage" in
   English) via the `entity.sensor.clean_mode.state.stairs` translation. The
   `vacuum.<robot>.fan_speed` attribute's translated label is also "Couverture
   complète" via `entity.vacuum.vacuum.state_attributes.fan_speed.state.stairs`.
   Operator confirmation: both the home dashboard tile and the vacuum card's
   drop-down displayed the French label correctly during the test.

2. **Default cycle time = 150 confirmed empirically.** After purging the residue
   storage entry (`cycle_time_stairs = 180` from the prior in-place-patch
   session) and restarting hass, `number.<robot>_cycle_time_stairs` came back
   at 150 — matching `CLEAN_MODES_CYCLE_TIME[CleanModes.STAIRS] = 150` in
   `common/clean_modes.py`. Closes the open question "is the FEAT-01 default
   actually applied on a clean state?" raised in the design discussion of PR #50.

3. **BUG-15 (label / slug oversight in PR #50).** PR #50 localized the
   `clean_mode` and `fan_speed.state` translations for `stairs` but **missed**
   `entity.number.cycle_time_stairs.name` in `strings.json` and in all three
   `translations/{en,fr,it}.json`. HA falls back to `original_name` from the
   `EntityDescription` (constructed as
   `f"Cycle Time {clean_mode}" = "Cycle Time stairs"`), and the registry
   slug is derived from it as `number.<robot>_cycle_time_stairs` (English),
   inconsistent with the six other modes which produce localized slugs
   (`number.<robot>_duree_du_cycle_complet`, etc., in French). Reproducing
   by deleting the entry from `core.entity_registry` and restarting confirms
   the bug is in the package itself, not in the operator's stale registry.
   Filed as [issue #53](../../../issues/53).

4. **BUG-13 reconfirmed.** Selecting a different mode in the
   `vacuum.<robot>.fan_speed` picker (`stairs → all`) without pressing Run
   triggered a full mode-write + cycle-time write + immediate cycle start. The
   operator did not press Run between the picker selection and the robot
   moving. Already documented in [issue #47](../../../issues/47) but worth
   keeping a fresh in-vivo confirmation in the diag corpus.

5. **BUG-14, more precisely characterised.** The integration **does** emit
   `Set cycle time 150` on a same-mode `set_fan_speed` call (the BUG-08
   chain runs unconditionally). The shadow update is published successfully
   (`Publish results: {'packet_id': N}`). What happens then is that the
   **firmware silently drops the cycleTime write** because `reported.mode ==
desired.mode` so it treats the request as a no-op delta. The reported
   `cycleTime` therefore stays at 180 (the value the app pushed earlier),
   not 150. This is a meaningfully different model from the wording in
   [issue #48](../../../issues/48) which currently says "the chain is not
   triggered" — the chain _is_ triggered, the firmware ignores it. Suggests
   the fix should be on the firmware side (impossible), or alternatively that
   the integration should force a sentinel delta when the user explicitly
   intends to refresh the cycleTime. A follow-up edit to issue #48 wording
   is in order.

6. **App-pushed `cycleTime` propagates verbatim.** When the operator started
   the cycle from the Maytronics app at 15:18:45 with the 3 h preset, the
   firmware reported `cycleTime = 180` and the integration's
   `sensor.<robot>_cycle_time` followed. The integration's
   `number.<robot>_cycle_time_stairs = 150` is **not** pushed when the cycle
   is initiated app-side; it only acts as the chained-write payload from a
   HA-initiated `set_fan_speed`. Already known as Gotcha #2 / BUG-14 — this
   session is just another data point.

7. **"Noticeable delay" measured.** Between the HA-side mode-write
   (15:37:48) and the firmware-reported `robotState = scanning`
   (15:39:09) there are **~81 s**. Most of that is firmware-internal: the
   `pwsState = on` reported transition arrives at 15:38:08 (~20 s after HA
   emit, consistent with AWS IoT round-trip + integration polling), but
   `robotState = scanning` only arrives ~61 s later, which is the firmware's
   own initialization & nav-system warmup. Helpful to set user expectations
   in the dashboard: "press Run, wait ~1 minute".

8. **`sensor.<robot>_etat_synthetique` rendering on `cleaning + scanning` is
   "Analyse".** This is a HA template sensor in the operator's config (not
   shipped by the integration). The label is already accurate French for the
   `scanning` substate. No action item.

## Open questions

- **Should BUG-14 issue wording be updated to reflect the firmware-side
  reality?** ("The chain is emitted but the firmware drops same-mode
  cycleTime writes" vs. the current text "the chain is not triggered".)
  Not blocking FEAT-01 validation.
- **Should the integration force a mode-toggle (`mode → all → mode`) to
  work around firmware same-mode filtering when the user explicitly
  changes `number.<robot>_cycle_time_<mode>`?** Probably no — too magical,
  and the BUG-14 path is rare.
- **Should the integration suppress its spurious `Set cycle time 150`
  emission at reconnect (line `13:18:43.868` in `01_app-start-stairs`)?**
  It pushes the operator's number value before any user request. Same write
  pattern was observed in session 2026-06-12. Worth a separate issue if it
  causes user confusion.

## Refs

- Closes [FEAT-01 #31](../../../issues/31) (mode `stairs` is fully exposed,
  localized, and behaves as expected).
- Filed [BUG-15 #53](../../../issues/53) — translation oversight in PR #50.
- Reconfirms [BUG-13 #47](../../../issues/47) and
  [BUG-14 #48](../../../issues/48) in vivo (refined BUG-14 wording suggested).
- Related: [PR #50](../../../pull/50) — re-merged FEAT-01 base.
- Previous: [session 2026-06-12 MAP-01 stairs validation](../2026-06-12_map-01_stairs-validation/findings.md) — pre-FEAT-01 state.
