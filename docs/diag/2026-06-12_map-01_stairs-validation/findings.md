# MAP-01 — `stairs` is a first-class user-selectable mode (`Couverture complète`)

## TL;DR

Hypothesis from issue [#31](../../../issues/31)'s analysis confirmed experimentally: `stairs` is the firmware identifier of the « Couverture complète » (Full Coverage) program, **not** a transient phase of `REGULAR (= all)`. From a freshly docked, `holdWeekly` robot, a single integration write of `desired.cleaningMode.mode = "stairs"` is interpreted by the firmware as both a _mode selection_ and a _start command_: the robot transitions `holdWeekly → on / init → scanning`, the integration's follow-up `desired.cycleInfo.cycleTime = 180` is honored, the operator-facing Maytronics app displays « Couverture complète » with an end time at T0 + 3 h 00. No app-side counter-write occurs because the app did not initiate.

PR #35's original design (keep `STAIRS` out of `fan_speed_list` / services / `CLEAN_MODES_CYCLE_TIME` because of a presumed "phase" status) is therefore falsified and must be reversed before merge.

## Context

- **Date**: 2026-06-12, local time `+02:00`.
- **Robot**: Dolphin S2000, firmware `pwsSwVersion=11.0004`, `muSwVersion=9F88`. Model identifier reported as `S4`.
- **Integration**: fork `raouldekezel/dolphin-robot`, branch tip `deploy` at SHA `3036f42` + the live in-place patch documented below — equivalent to the planned `v1.0.26b3-raoul.2`.
- **Home Assistant**: 2026.1.3, container deployment on `intel-nuc.local`.
- **Pre-experiment configuration**:
  - The `CleanModes` enum on the deployed install was extended in-place with `STAIRS = "stairs"`, and `CLEAN_MODES_CYCLE_TIME` was extended in-place with `CleanModes.STAIRS: 150` (firmware catalog default). All other code paths unchanged. This is the minimum surgery required to let the integration's existing `vol.In(list(CleanModes))` and `fan_speed_list=list(CleanModes)` accept `stairs`; without it the experiment cannot be driven from HA at all. Backup retained at `clean_modes.py.bak-20260612-192755`.
  - The number entity `number.<robot>_cycle_time_stairs` was set to 180 via `number.set_value` before the start command. This intentionally exceeds the firmware catalog default (150) so the cycleTime write is unambiguously observable.
- **Robot's pre-experiment state**: `docked`, `pwsState=holdWeekly`, weekly schedule programming `cleaningMode.mode = "stairs"` on all 7 days at 11:00 (set from the Maytronics app).
- **Logging**: `custom_components.mydolphin_plus.managers.{aws_client,config_manager,rest_api} = debug` enabled persistently in `configuration.yaml`.

## Actions taken

1. **`04_set-fan-speed-stairs-on-ha`** — From the HA UI: `vacuum.set_fan_speed(entity_id=vacuum.<robot>, fan_speed=stairs)`. No explicit `vacuum.start` call followed; the firmware took the mode write as a start signal (see Findings #2).

The earlier per-mode sensor poll (cf. the BUG-08 session) was not run here — the question is binary (does `stairs` behave like a mode), the operator's manual observation of the Maytronics app is sufficient to answer it, and the MQTT slice is fully self-describing.

## Timeline

Wall-clock timestamps are local (`+02:00`).

| Timestamp    | Actor           | Payload                                                                                                        | Effect                                                                                |
| ------------ | --------------- | -------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------- |
| 19:40:32.260 | **Integration** | `Set cleaning mode, Desired: {'cleaningMode': {'mode': 'stairs'}}`                                             | publish #47, triggered by `vacuum.set_fan_speed`                                      |
| 19:40:33.320 | **Integration** | `Set cycle time, Desired: {'cycleInfo': {'cycleTime': 180}}`                                                   | publish #48, **+1.058 s** after #47 (BUG-08 `sleep(1)` still present)                 |
| 19:40:33.389 | Firmware        | `desired` echo of `cycleTime: 180` accepted into `accepted`/`documents`                                        | shadow version 199                                                                    |
| 19:40:33.708 | Firmware        | `reported.cycleInfo.cleaningMode.cycleTime = 150`                                                              | initial transient: firmware still carrying weekly default before applying new desired |
| 19:40:34.542 | Firmware        | `reported.systemState.pwsState = "on", robotState = "init"`, `reported.cycleInfo.cleaningMode.cycleTime = 180` | mode write interpreted as start; new cycleTime now in `reported`                      |
| 19:40:35.308 | Firmware        | `reported.cycleInfo.cleaningMode.cycleTime = 180` (stable)                                                     | shadow version 204                                                                    |
| 19:40:36.073 | Firmware        | `reported.cleaningMode.{mode: stairs, cycleTime: 180}`                                                         | shadow version 205                                                                    |
| ≈22:40       | Operator        | Maytronics app displays « Couverture complète » running until 22:40                                            | T0 + 3 h 00 confirms 180 min applied end-to-end                                       |

Notable **absence**: no app-side `desired.cycleInfo.cycleTime` write in the window (contrast session [#41](https://github.com/raouldekezel/dolphin-robot/pull/41) action 2, where the app was the trigger and wrote its own 150 ~50 ms after the integration's 60, overwriting it). The app's counter-write race only occurs when the app itself initiated the mode change; an HA-initiated start runs clean.

## Findings

1. **`stairs` is a first-class user-selectable mode.** It is published by the integration on `desired.cleaningMode.mode`, accepted by the firmware, mirrored back on `reported.cleaningMode.mode`, and rendered as the operator-facing « Couverture complète » program in the Maytronics app. This matches the three independent pre-experiment indicators already analysed in [#31](../../../issues/31) (catalog entry with a duration, weekly schedule programmability, cycle starting _in_ stairs, not _passing through_ it).
2. **Mode write while docked + holdWeekly is an implicit start.** No `desired.systemState.pwsState = "on"` write is issued by the integration before the firmware transitions to `pwsState=on`. The firmware's state machine treats a `desired.cleaningMode.mode` write as "start running this mode now" when the robot is dockable. Independently observed twice in this session: once by the operator earlier (selecting « Couverture complète » in the Maytronics app at ~19:29 also started the cycle without an explicit Run press; same MQTT shape, app-side); once at 19:40:32 from HA.
3. **HA-initiated `cycleTime` writes win when the app is uninvolved.** The firmware adopted the integration's `180` within ~2 s of publish #48, and the operator's app showed an end time of T0 + 3 h 00. No counter-write race — this isolates the [BUG-08](../../../issues/17) race window to _app-initiated_ mode changes only.
4. **BUG-08's `sleep(1)` is still visibly present** — publish #48 lands **+1.058 s** after publish #47. Adds a fourth data point to the BUG-08 collection (sessions #41 actions 2/3, action 1 had no integration publish).

## Implications for PR #35

The PR's original design enforced the falsified _phase_ premise. Required changes before merge (each with a regression test):

- Keep `STAIRS` in `fan_speed_list` and `vol.In(list(CleanModes))` in `service_schema` (these are the surfaces the original review #35 blocked).
- Add `CleanModes.STAIRS: 150` to `CLEAN_MODES_CYCLE_TIME` so the per-mode number entity is generated (this is the experiment's actual configuration default).
- Relabel `stairs` to follow the app's operator-facing program name:
  - EN: `"Full Coverage"` (not `"Stairs"`)
  - FR: `« Couverture complète »` (not `« Escaliers »`)
  - IT: `"Copertura completa"` (not `"Scale"`)
- Resolve the dead `parse()` review point: either wire it at `coordinator.py:637` / `:855` with an explicit fallback decision, or remove it. Shipping unwired safety-net code is misleading.
- Add a test asserting `STAIRS in fan_speed_list`, `STAIRS in CLEAN_MODES_CYCLE_TIME`, and the relabel in each translation file.

## Open questions

- The firmware catalog at the moment of this experiment shows `cleaningModes.all = 60` (not the 180 the catalog stored a few hours earlier in session [#41](https://github.com/raouldekezel/dolphin-robot/pull/41)). The catalog values appear to be the _last cycleTime applied per mode_ (carrier of last-write-wins state), not immutable defaults. Worth documenting separately — does not affect MAP-01's conclusion.
- The firmware catalog exposes 5 modes unknown to the integration's enum (`cove`, `spot`, `wall`, `ticTac`, `custom`). Out of MAP-01's scope — to be parked in a separate issue.
- `featureEn.{floor, short, pickup}: "disable"` while the Maytronics app exposes both `floor` and `short` programs. The semantic of `featureEn` is not understood; it did not gate this experiment but should not be assumed safe to ignore.

## Refs

- Issue [#31 — MAP-01: CleanModes enum has no 'stairs' value](../../../issues/31), comments dated 2026-06-12 carrying the hypothesis analysis.
- PR [#35 — MAP-01: CleanModes adds STAIRS + tolerant parse](../../../pull/35), to be redesigned per "Implications" above.
- Prior session [`docs/diag/2026-06-12_bug-08_cycle-time/`](../2026-06-12_bug-08_cycle-time/findings.md) — provides the shadow snapshots that originally triggered the hypothesis.
- Issue [#17 — BUG-08: time.sleep(1) blocks the awscrt event-loop thread](../../../issues/17) — fourth data point added by this session.
