# MAP-03 — Unknown firmware cleaning modes: investigation and decision

## TL;DR

The S4-family firmware advertises 12 entries in its `cleaningModes` catalog. Six are wired to the integration's `CleanModes` enum (`all`, `short`, `floor`, `water`, `ultra`, `pickup`); a seventh (`stairs`) is added by [PR #35](../../../pull/35). The remaining five (`cove`, `spot`, `wall`, `ticTac`, `custom`) had no documentation, no public-facing label, and an unknown effect on the robot.

This session experimentally verified that **`cove`, `spot` and `wall` are first-class firmware-driven cleaning modes** — each accepted by the shadow, each transitioning the robot from `holdWeekly` to `on / init` within ~2.5 s of an HA-initiated mode write, each surviving without error. The Maytronics app reflects them with **unresolved i18n placeholders** (`cleaning_mode_<mode>_title`), meaning the operator-facing UX deliberately does not expose them.

A follow-up **negative control** (see [Appendix](#appendix-negative-control--invalid-mode-write) and [`04_zzzz_invalid-mode-negative-control.mqtt.log`](./04_zzzz_invalid-mode-negative-control.mqtt.log)) writing the deliberately-invalid mode name `zzzz` confirmed that the firmware **does not simply mirror whatever it receives**: the catalog-unknown name was silently remapped to `all` (Regular). This strengthens the cove/spot/wall result — those names survived as themselves through the firmware's catalog lookup, whereas `zzzz` did not. The mirror is a real acceptance signal, not a passive sync.

`ticTac` was independently identified upstream of this session as a **service/diagnostic mode** documented only in DolphinTech Plus, Maytronics' technician-facing app — not a user-facing cleaning mode. Out of an abundance of caution it was excluded from the experiment.

`custom` was not exercised; its semantics (almost certainly a parameterized mode wired to the Maytronics app's « Custom » dialog) cannot be characterized by a bare mode write.

**Decision: do not extend `CleanModes` with `cove`, `spot`, `wall`, `custom`, or `ticTac`.** The integration ships only the modes Maytronics exposes through MyDolphin Plus (the operator-facing app) plus `stairs` (= « Couverture complète », which IS user-facing in the app). The five unknowns remain reachable only by hand-crafted shadow writes outside the integration's enum, which is the appropriate blast-radius for undocumented behavior.

## Context

- **Date**: 2026-06-13, local time `+02:00`.
- **Robot**: Dolphin S2000, firmware `pwsSwVersion=11.0004`, `muSwVersion=9F88`, reported `robotType=S4`.
- **Integration**: fork `raouldekezel/dolphin-robot`, branch tip `deploy` at SHA `8f5a3d2` + an extended in-place patch on the deployed install (see below).
- **Home Assistant**: 2026.1.3, container deployment on `intel-nuc.local`.
- **Pre-experiment configuration on the deployed install**:
  - `CleanModes` enum extended in-place with `COVE = "cove"`, `SPOT = "spot"`, `WALL = "wall"`, `CUSTOM = "custom"` (in addition to the `STAIRS` patch left in place from [PR #43](../../../pull/43)). `CLEAN_MODES_CYCLE_TIME` extended with each mode at `120` (matching the firmware catalog default the robot reported at boot). Backup of the pre-stairs original at `clean_modes.py.bak-20260612-192755` (one revert restores the unpatched file).
  - Persistent debug logging on `custom_components.mydolphin_plus.managers.{aws_client,config_manager,rest_api}` (kept from previous diag sessions).
- **Robot's pre-experiment state**: `docked`, `pwsState=holdWeekly`, weekly schedule programming `cleaningMode.mode = "stairs"` daily at 11:00 (carried over from earlier sessions).
- **Post-experiment cleanup**: the in-place patch was reverted back to the post-PR-43 form (`STAIRS` only); the 4 transient `cycle_time_{cove,spot,wall,custom}` entries that `config_manager` had auto-created in `.storage/mydolphin_plus.config.json` remain in storage but are ignored by the reverted enum.

## Actions taken

| Slug                                  | Trigger                                                                                                    | Outcome                                    |
| ------------------------------------- | ---------------------------------------------------------------------------------------------------------- | ------------------------------------------ |
| `01_cove` ([log](./01_cove.mqtt.log)) | HA UI: `vacuum.set_fan_speed(entity_id=vacuum.<robot>, fan_speed=cove)` from `holdWeekly`                  | Cycle started                              |
| `02_spot` ([log](./02_spot.mqtt.log)) | HA UI: `vacuum.set_fan_speed(entity_id=vacuum.<robot>, fan_speed=spot)` while previous cycle running       | Mode swap honored                          |
| `03_wall` ([log](./03_wall.mqtt.log)) | HA UI: `vacuum.set_fan_speed(entity_id=vacuum.<robot>, fan_speed=wall)`, twice (10 s probe then 2 min run) | Cycle started both times, both stops clean |
| _(skipped)_ `ticTac`                  | not run — known service mode (DolphinTech Plus), out of scope for an end-user enum                         | n/a                                        |
| _(skipped)_ `custom`                  | not run — semantically a parameterized mode, a bare write would not characterize it                        | n/a                                        |

The operator observed the Maytronics app for each run and reported back the displayed label and that the robot was visibly working.

## Timeline

Wall-clock timestamps are local (`+02:00`). All `Set cleaning mode` / `Set cycle time` rows are HA-initiated via the integration. All `reported.*` rows are firmware echoes from the device shadow.

### `01_cove`

| Timestamp      | Actor           | Payload                                                                                                                     | Effect                                                               |
| -------------- | --------------- | --------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------- |
| 11:05:42.943   | **Integration** | `Set cleaning mode, Desired: {'cleaningMode': {'mode': 'cove'}}`                                                            | publish #25 from `vacuum.set_fan_speed`                              |
| 11:05:44.029   | **Integration** | `Set cycle time, Desired: {'cycleInfo': {'cycleTime': 120}}`                                                                | **+1.086 s** after #25 — BUG-08 `sleep(1)` data point                |
| 11:05:45.503   | Firmware        | `reported.systemState.pwsState = "on", robotState = "init"`; `cycleInfo.cleaningMode = {"cove", 120}`; `cycleStartTime` set | **+2.560 s** after #25 — mode write interpreted as start             |
| 11:08:11.012   | Integration     | `desired.systemState.pwsState = "off"` (operator stop)                                                                      | shadow version 287                                                   |
| 11:08:15–17    | Firmware        | back to `pwsState=holdWeekly`, `robotState=notConnected`                                                                    | clean stop                                                           |
| Maytronics app | Operator        | « cleaning_mode_cove_title » (unresolved i18n placeholder)                                                                  | mode known to app, no human-facing label, not selectable from app UI |

### `02_spot`

| Timestamp      | Actor           | Payload                                                                                               | Effect                                         |
| -------------- | --------------- | ----------------------------------------------------------------------------------------------------- | ---------------------------------------------- |
| 11:08:31.566   | **Integration** | `Set cleaning mode, Desired: {'cleaningMode': {'mode': 'spot'}}`                                      | publish from `vacuum.set_fan_speed`            |
| 11:08:32.664   | **Integration** | `Set cycle time, Desired: {'cycleInfo': {'cycleTime': 120}}`                                          | **+1.098 s** — BUG-08 data point               |
| 11:08:33.208   | Firmware        | `reported.nextCycleInfo.cleaningMode.mode = "spot"`                                                   | scheduler side-effect                          |
| 11:08:33.988   | Firmware        | `reported.systemState.pwsState = "on", robotState = "init"`; `cycleInfo.cleaningMode = {"spot", 120}` | **+2.422 s** — mode write interpreted as start |
| Maytronics app | Operator        | « cleaning_mode_spot_title » (unresolved i18n placeholder)                                            | same shape as `cove`                           |

### `03_wall` (two runs)

| Timestamp      | Actor           | Payload                                                                                               | Effect                                         |
| -------------- | --------------- | ----------------------------------------------------------------------------------------------------- | ---------------------------------------------- |
| 11:11:44.778   | **Integration** | `Set cleaning mode, Desired: {'cleaningMode': {'mode': 'wall'}}`                                      | publish from `vacuum.set_fan_speed`            |
| 11:11:46.466   | Firmware        | `reported.nextCycleInfo.cleaningMode.mode = "wall"`                                                   | scheduler side-effect                          |
| 11:11:47.205   | Firmware        | `reported.systemState.pwsState = "on", robotState = "init"`; `cycleInfo.cleaningMode = {"wall", 120}` | **+2.427 s** — mode write interpreted as start |
| 11:11:54.108   | Integration     | `desired.systemState.pwsState = "off"` (operator probe stop)                                          | shadow version 308                             |
| 11:11:58.399   | Firmware        | back to `pwsState=holdWeekly`                                                                         | clean stop                                     |
| 11:12:03.834   | **Integration** | `Set cleaning mode, Desired: {'cleaningMode': {'mode': 'wall'}}` (2nd run)                            | publish from `vacuum.set_fan_speed`            |
| 11:12:05.002   | **Integration** | `Set cycle time, Desired: {'cycleInfo': {'cycleTime': 120}}`                                          | **+1.168 s** — BUG-08 data point               |
| 11:14:49.919   | Integration     | `desired.systemState.pwsState = "off"` (operator final stop)                                          | shadow version 322                             |
| 11:14:53.331   | Firmware        | back to `pwsState=holdWeekly`                                                                         | clean stop after ~2 min 46 s of cycle          |
| Maytronics app | Operator        | « cleaning_mode_wall_title » (unresolved i18n placeholder)                                            | same shape as `cove`/`spot`                    |

## Findings

1. **`cove`, `spot`, `wall` are firmware-supported standalone cleaning modes.** Each is published on `desired.cleaningMode.mode`, accepted by the firmware (no rejection, no fault, no error code in shadow), mirrored back into `reported.cleaningMode.mode`, drives the robot through `holdWeekly → on / init` (and presumably the rest of the cycle FSM — not pushed to completion in this session). Behavioral shape is identical to `all`/`stairs`/`ultra` per the MAP-01 baseline (see [diag/2026-06-12_map-01_stairs-validation](../2026-06-12_map-01_stairs-validation/findings.md)).
2. **Maytronics chooses not to expose these modes in the operator app.** The MyDolphin Plus app renders an unresolved i18n key (`cleaning_mode_<mode>_title`) rather than a translated label — the strings exist in the resource bundle but were never localized, which is the canonical Maytronics tell for "shipped firmware capability, intentionally not in the consumer UX". `stairs` by contrast is fully localized in the app as « Couverture complète » (FR) / "Full Coverage" (EN). The semantic difference is explicit.
3. **Mode-swap mid-cycle is honored without an off transition.** During `02_spot`, the integration wrote `desired.cleaningMode.mode = "spot"` while the robot was still cycling `cove` from `01_cove`. The firmware adopted `spot` immediately — no `off` interlude — confirming that on a non-docked, already-cleaning robot a mode write is treated as an in-flight mode swap, not a "reject because already running" condition.
4. **The BUG-08 sleep(1) is still present.** Three more data points: cove +1.086 s, spot +1.098 s, wall +1.168 s between `Set cleaning mode` publish and `Set cycle time` publish. Consistent with the BUG-08 collection in [#41](../../../pull/41) and [PR #43](../../../pull/43).
5. **`nextCycleInfo.cleaningMode.mode` follows `cycleInfo.cleaningMode.mode` on any HA-initiated mode write.** Same scheduler side-effect noted in [diag/2026-06-12_map-01_app-driven-mode-transitions](../2026-06-12_map-01_app-driven-mode-transitions/findings.md) on the app-initiated path is also observed on the HA-initiated path. Not in MAP-03's scope — flagged for future characterization (does this also overwrite `weeklySettings.<day>.cleaningMode.mode`? Not checked here; weeklySettings entries did not change in the captured payloads, but the window was short).

## `ticTac` — external evidence (no in-pool test required)

`ticTac` is documented on the App Store description of **DolphinTech Plus** (id1406110365), the Maytronics-for-Technicians app, in the list of diagnostic actions: « Activate the robot in tic-tac mode ». DolphinTech Plus is distinct from MyDolphin Plus (the consumer app); its audience is field service technicians performing diagnostics and repair on physical units. The behavior is not described in detail, but the contextual placement (diagnostic / repair) and the etymology of "tic-tac" (pendulum, back-and-forth metronome) are consistent with a **motor-driver test cycle** (alternating left/right traction) intended to be run by a technician with the robot out of water or on the bench. No consumer-facing manual, app, or community thread mentions a `ticTac` cleaning mode. The camelCase casing in the firmware catalog (versus lowercase for all other entries) is consistent with an internal/service identifier.

Conclusion: extending `CleanModes` with `ticTac` would put a technician-only operating mode behind the `vacuum.set_fan_speed` service. **Do not add.**

## `custom` — not exercised, but unlikely to belong in the enum

The Maytronics consumer app exposes a « Custom mode » dialog where the operator picks a duration and toggles floor/walls/waterline on or off; the resulting cycle is then started. A reasonable hypothesis is that `desired.cleaningMode.mode = "custom"` requires a companion payload describing the selected components — a bare mode write would either be ignored or run with stale parameters. Either way, the integration's `vacuum.set_fan_speed` UX is a single string, which cannot transport the companion payload. **Do not add** unless and until the integration grows a way to publish the full `custom` payload — out of MAP-03's scope.

## Decision

The integration's enum surface is the user contract; everything in `fan_speed_list` and accepted by `vol.In(list(CleanModes))` is implicitly endorsed as something a Home Assistant user can select safely. The five modes investigated here do not meet that bar:

| Mode     | Firmware-pilotable | Public-app exposure | Decision | Reason                                                                                |
| -------- | ------------------ | ------------------- | -------- | ------------------------------------------------------------------------------------- |
| `cove`   | ✅                 | i18n unresolved     | **skip** | Maytronics explicitly does not surface it to operators; unknown semantic ("alcove"?)  |
| `spot`   | ✅                 | i18n unresolved     | **skip** | Same; "spot cleaning" via manual drive is the app's documented spot-mode story        |
| `wall`   | ✅                 | i18n unresolved     | **skip** | Same; the regular `water` (waterline) mode is the operator-facing wall-side program   |
| `ticTac` | not tested         | DolphinTech only    | **skip** | Documented elsewhere as a technician/diagnostic mode — must not be selectable from HA |
| `custom` | not tested         | App dialog          | **skip** | Requires a parameter payload `vacuum.set_fan_speed` cannot transport                  |

The `CleanModes` enum stays at 7 entries (`all`, `short`, `floor`, `water`, `ultra`, `pickup`, `stairs`). Users who need to drive a non-enum mode for testing can publish directly on the AWS shadow; that remains an out-of-warranty path, deliberately.

## Open questions

- Does writing `desired.cleaningMode.mode = X` on a docked robot overwrite anything in `weeklySettings`? In this session it did not appear to — but the window was short. Worth a focused session if MAP-03 ever needs re-opening.
- What is `featureEn.{floor, short, pickup}: "disable"` actually gating? Not blocking this decision (the experiment ran cleanly), but the semantic remains undefined.
- The firmware catalog also exposes `cleaningModes.all = 180` after this session whereas it was `60` at the start of session [#43](../../../pull/43). The catalog appears to carry **last-applied** per-mode cycle times (not immutable defaults). Cross-references: see MAP-01 findings.

## Refs

- Issue [#31 — MAP-01: CleanModes enum has no 'stairs' value](../../../issues/31) — origin of the firmware catalog inventory.
- PR [#35 — MAP-01: CleanModes adds STAIRS + tolerant parse](../../../pull/35) — to be redesigned independently.
- PR [#43 — docs(diag): MAP-01 stairs validation session](../../../pull/43) — baseline for the `stairs` shape.
- PR [#44 — docs(diag): MAP-01 app-driven mode transitions](../../../pull/44) — origin of the `nextCycleInfo.cleaningMode` scheduler side-effect note.
- Issue [#17 — BUG-08: time.sleep(1) blocks the awscrt event-loop thread](../../../issues/17) — three new data points added here.
- DolphinTech Plus (Maytronics-for-Technicians), App Store id1406110365 — source for `ticTac` being a service/diagnostic mode.

## Appendix: Negative control — invalid mode write

Initial worry after the cove/spot/wall runs: the `reported.cleaningMode.mode` mirror could be passive — the firmware echoing whatever it received from `desired` regardless of whether the name was recognized. If so, the "acceptance" of cove/spot/wall would be meaningless and we would not actually have evidence they are catalog entries. The control test directly falsifies this concern.

### Setup

A one-off addition `ZZZZ = "zzzz"` to `CleanModes` in the deployed install, restart, single HA-initiated mode write to a deliberately-invalid name `"zzzz"`, then revert. Slice in [`04_zzzz_invalid-mode-negative-control.mqtt.log`](./04_zzzz_invalid-mode-negative-control.mqtt.log).

### Timeline

Wall-clock timestamps are local (`+02:00`). Robot in `docked` / `pwsState=holdWeekly`. The `weeklySettings` daily schedule had drifted from `"stairs"` (sessions earlier) to `"all"` between this control and the cove/spot/wall runs — this is the firmware-side scheduler echoing the most recent app-driven schedule reconfiguration, unrelated to the test.

| Timestamp    | Actor           | Payload                                                                                                           | Effect                                                                            |
| ------------ | --------------- | ----------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------- |
| 11:50:46.445 | **Integration** | `Set cleaning mode, Desired: {'cleaningMode': {'mode': 'zzzz'}}`                                                  | publish #7 — payload sent as-is to AWS shadow                                     |
| 11:50:47.524 | **Integration** | `Set cycle time, Desired: {'cycleInfo': {'cycleTime': 120}}`                                                      | +1.079 s — BUG-08 sleep, irrelevant to the control                                |
| 11:50:47.676 | Firmware        | `reported.cycleInfo.cleaningMode = {"mode": "all", "cycleTime": 120}`; `cycleStartTime` reset                     | **+1.231 s — the firmware did NOT echo `zzzz`. It silently substituted `all`.**   |
| 11:50:48.523 | Firmware        | `reported.systemState.pwsState = "on"`, `robotState = "init"`; `cleaningMode = {"mode": "all", "cycleTime": 120}` | +2.078 s — cycle started in `all` (not in `zzzz`), interpreted as "default start" |
| 11:55:07.035 | Integration     | Operator stop via `vacuum.pause`: `Set power state, Desired: {'systemState': {'pwsState': 'off'}}`                | publish #9                                                                        |
| 11:55:11.442 | Firmware        | `reported.pwsState = "holdWeekly"`, `robotState = "notConnected"`                                                 | clean stop                                                                        |

Throughout the window: `cleaningModes` catalog (the firmware's reported mode list) **never grew** a `zzzz` entry — the firmware does not learn arbitrary mode names from `desired` writes.

### Findings

1. **The firmware performs a catalog lookup on `desired.cleaningMode.mode` and substitutes a fallback for unknown names.** The fallback observed here is `all` (Regular, 120 min). This refutes the "passive mirror" hypothesis and demonstrates that the mirror seen in cove/spot/wall payloads is the result of a successful catalog hit, not passive sync.
2. **An unknown-mode write still starts a cycle.** `pwsState: holdWeekly → on` and `robotState: notConnected → init` transitions still fire — the firmware treats "mode write while docked" as an implicit start command, even when the mode name is unrecognized. Practical consequence: typo-protection is non-trivial; the worst-case effect of a malformed mode write from HA is "robot starts a Regular cycle", not "nothing happens".
3. **No error / no rejection is surfaced.** No fault code, no `rejected` shadow topic, no `robotError`. The operator-facing visibility of a remapping is zero — only the discrepancy between `desired.mode` (what we wrote) and `reported.mode` (what the firmware actually adopted) is observable, and only if you're inspecting the shadow.
4. **The remap fallback is deterministic to `all`.** Not random, not last-applied (would have been `stairs` carried over from earlier in the session if it were); the fallback target is explicitly the Regular mode. This is what we should expect from a firmware that picks a "safe default" for invalid input.

### Implication for the MAP-03 decision

Reinforces the decision unchanged. The reading of cove/spot/wall as "firmware-pilotable, catalog-recognized" is empirically stronger now: they passed the catalog lookup whereas `zzzz` did not. The skip-them decision continues to rest on the i18n unresolved label (= Maytronics intentionally not in operator UX), not on any doubt about firmware acceptance.

The control also surfaces one operational note worth carrying forward: **an HA-initiated mode write on a docked robot can start an unintended cycle even when the mode name is malformed**, because the firmware falls back to `all`. The integration's `vol.In(list(CleanModes))` validation is therefore a real safety surface, not a paperwork formality.

### Integration bug noticed in passing

`vacuum.stop` returns HTTP 500 for this entity (likely the long-running HA vacuum `STATE_*` deprecation issue [#240](../../../issues/240) showing up on `stop` as well as `start`). The working stop path is `vacuum.pause`, which the integration maps to `Set power state, Desired: {'systemState': {'pwsState': 'off'}}`. Out of MAP-03's scope; flag for a separate issue if not already covered.

## Appendix B: app-side use of the `cleaningModes` catalog — refined hypothesis

The TL;DR initially carried a naive hypothesis: the Maytronics app would display the per-mode shadow value as the default duration in its cycle picker. A short operator test on 2026-06-13 (timestamps in PR [#46](../../../pull/46) comment thread, ~12:48 local) refines this and surfaces a separate same-mode-start gotcha.

### Setup of the operator test

- Shadow's `cleaningModes.all` at the start of the test = `60` (a leftover from an earlier HA-side experiment, persisted across sessions).
- HA-side `number.<robot>_cycle_time_all = 60`.
- Robot docked, `pwsState=holdWeekly`.

### Observations

1. Operator opens the Maytronics app and picks Complete mode. The app's duration picker exposes **a fixed preset list `[2h, 2h30, 3h]` (i.e. 120, 150, 180 min)**. All three buttons render greyed-out — none is highlighted as the current selection.
2. Operator picks `2h30`; the cycle runs 2h30; firmware updates `cycleInfo.cleaningMode.cycleTime` to `150` (and, by the catalog-mutation mechanism documented above, `cleaningModes.all` to `150`).
3. Operator stops the robot.
4. Operator starts a Complete cycle from HA. Despite `number.<robot>_cycle_time_all = 60`, the Maytronics app displays the cycle running **2h30**.

### Refined hypothesis 1

The Maytronics app exposes only the fixed preset list per mode, and uses the shadow's catalog value to highlight whichever preset matches. An off-grid catalog value (e.g. an HA-written `60`, which doesn't match any of `120 / 150 / 180`) leaves no preset selected → all greyed. This is consistent with everything we have observed, supersedes the naive "the app shows the catalog value as-is" reading from the TL;DR, and matches the asymmetry between firmware capability (any cycleTime accepted) and operator UX surface (3 presets per mode).

### Gotchas surfaced — tracked as separate issues

- **BUG-13** ([#47](../../../issues/47)): `vacuum.set_fan_speed` on a docked robot is interpreted by the firmware as a combined "set mode + start" command — operator scripts that toggle `fan_speed` with no intent to clean will start a full cycle.
- **BUG-14** ([#48](../../../issues/48)): `vacuum.start` in an already-current mode does not emit `Set cleaning mode → Set cycle time`; the firmware resumes on its persisted `cycleInfo.cleaningMode.cycleTime`, which is often the value the Maytronics app wrote last. The HA-side `number.<robot>_cycle_time_<mode>` is therefore silently bypassed on same-mode restarts.

Both are out of MAP-03's scope (no `CleanModes` enum surgery would fix them); their resolution is tracked under their own issues.
