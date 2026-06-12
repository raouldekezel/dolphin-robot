# MAP-01 — app-initiated `all` and `stairs` cycles, integration's per-mode cycleTime policy fires regardless of trigger

## TL;DR

When the Maytronics app — not Home Assistant — initiates the mode change (start « Complet » then start « Couverture complète »), the integration silently enforces its locally-configured per-mode `cycle_time_<mode>` value on the firmware ~1.0 s after every mode delta. The `Set cleaning mode` log line is absent in this case (the integration didn't emit the mode write), but `Set cycle time` fires consistently with the BUG-08 `sleep(1)` cadence already observed in session [#43](https://github.com/raouldekezel/dolphin-robot/pull/43). Combined with #43, this isolates the BUG-08 race window to _the integration vs. the app_ writing `cycleTime` competitively — last-write-wins on AWS IoT Shadow, integration always loses if the app initiated.

## Context

- **Date**: 2026-06-12, local time `+02:00`.
- **Robot**: Dolphin S2000, firmware `pwsSwVersion=11.0004`, `muSwVersion=9F88`. Model identifier reported as `S4`.
- **Integration**: fork `raouldekezel/dolphin-robot`, branch tip `deploy` at SHA `3036f42` + the same in-place patch documented in session [#43](https://github.com/raouldekezel/dolphin-robot/pull/43) (`STAIRS = "stairs"` in `CleanModes`, `CleanModes.STAIRS: 150` in `CLEAN_MODES_CYCLE_TIME`).
- **Home Assistant**: 2026.1.3, container deployment on `intel-nuc.local`.
- **HA-side per-mode cycle time configuration at the start of the experiment**:
  - `cycle_time_all = 60`
  - `cycle_time_stairs = 180` (changed from 150 in session #43 and not reverted)
- **Robot's pre-experiment state**: stopped from the HA UI after the cycle started in #43, currently `docked` / `pwsState=holdWeekly`.
- **Logging**: `custom_components.mydolphin_plus.managers.{aws_client,config_manager,rest_api} = debug` enabled in `configuration.yaml`.

## Actions taken

1. **`05_app-driven-mode-transitions`** — All four transitions driven from the **Maytronics mobile app** (no HA service call): start `« Complet »` (mode `all`) → stop → start `« Couverture complète »` (mode `stairs`) → stop. The trace captures the integration's reactive writes.

   The user reports an extra repeat-start of `« Complet »` between 19:45:44 and 19:46:17 — visible in the trace as a second `Set cycle time, cycleTime: 60` line. The cause (operator double-tap vs app behaviour vs delta re-issue) is not investigated here; what matters is that the integration consistently re-applies its policy on every mode delta.

## Timeline

Wall-clock timestamps are local (`+02:00`).

| Timestamp    | Origin                 | Event                                                         | Effect                                                                                      |
| ------------ | ---------------------- | ------------------------------------------------------------- | ------------------------------------------------------------------------------------------- |
| 19:45:43.832 | Firmware → Integration | `shadow/update/delta` carrying `cleaningMode.mode = "all"`    | (start « Complet » initiated by app at ~19:45)                                              |
| 19:45:44.834 | **Integration**        | `Set cycle time, Desired: {'cycleInfo': {'cycleTime': 60}}`   | publish #67, **+1.002 s** after the delta (BUG-08 `sleep(1)`)                               |
| 19:45:44.842 | **Integration**        | published to `shadow/update`                                  | firmware accepts                                                                            |
| 19:45:46.460 | Firmware → Integration | `shadow/update/delta`                                         | echo of integration's cycleTime write being applied                                         |
| 19:46:17.567 | Firmware → Integration | `shadow/update/delta` carrying `cleaningMode.mode = "all"`    | second mode delta (operator repeat-start, see Actions)                                      |
| 19:46:17.566 | **Integration**        | `Set cycle time, Desired: {'cycleInfo': {'cycleTime': 60}}`   | publish #70, near-simultaneous with the delta — fires from the previous BUG-08 sleep window |
| 19:48:34.362 | Firmware → Integration | `shadow/update/delta` carrying `cleaningMode.mode = "stairs"` | (start « Couverture complète » initiated by app at ~19:48)                                  |
| 19:48:35.398 | **Integration**        | `Set cycle time, Desired: {'cycleInfo': {'cycleTime': 180}}`  | publish #79, **+1.036 s** after the delta (BUG-08 `sleep(1)`)                               |
| ~19:50       | Operator               | stop from app                                                 | end of experiment                                                                           |

Notable **absence**: no `Set cleaning mode, Desired: {'cleaningMode': {'mode': ...}}` log line anywhere. The integration only logs that string when _it_ publishes the mode; here the publishes come from the app, so the integration only observes them via delta and only re-emits its own `cycleTime` policy.

## Findings

1. **The integration's per-mode `cycle_time_<mode>` policy is applied on every observed mode delta**, regardless of the delta's origin. Session #43 already showed the path when HA is the initiator (`set_fan_speed → mode publish → cycle_time publish`); session 05 closes the contrapositive: when the _app_ publishes the mode, the integration still publishes the configured cycle_time ~1 s later. The user-facing implication is non-trivial — the firmware catalog's `cleaningModes.<mode>` value (150 for `stairs`) is effectively ignored as long as the integration is running with its own value (`cycle_time_stairs = 180` in this experiment).

2. **BUG-08 `sleep(1)` cadence confirmed twice more.** The two observable delta → publish gaps are **+1.002 s** (`all` start) and **+1.036 s** (`stairs` start) — consistent with sessions #41 / #43.

3. **`stairs` (= « Couverture complète ») behaves identically to `all` (= « Complet ») on the firmware side.** Both are accepted as mode writes via `desired.cleaningMode.mode`, both trigger the integration's `cycle_time` policy on the same path, both reflect in `reported.cleaningMode.mode`. This is the symmetry MAP-01 predicted — `stairs` is not a special-case phase, it is a peer of `all`.

4. **Repeated mode-`all` delta within ~33 s produces two integration writes.** The integration does not debounce: every delta gets its own `Set cycle time` publish (publishes #67 and #70). If the underlying delta is just a re-affirmation of the same state, the integration still re-applies the policy — harmless here, but worth noting for any future race analysis. The mechanism (operator double-tap vs Maytronics app retransmission) is out of scope; only the integration's response is documented.

## Open questions

- The second mode-`all` delta at 19:46:17 — what produced it? Could be operator (double-tap on app), could be app retransmission (some Maytronics versions re-publish on connection blip), could be firmware echo round-tripping. A follow-up session with a parallel app-side packet capture would disambiguate.
- The trace contains a single `shadow/update/delta` at 19:45:00, ~43 s _before_ the first mode delta at 19:45:43. Its payload was not investigated here. May be the user's HA-side stop from session #43 still propagating.
- The integration's behaviour of always re-applying `cycle_time_<mode>` may conflict with users who deliberately want to vary the duration via the app for a one-off cycle. Not a bug per se — but a user-visible side effect of how the integration models per-mode duration as policy. Worth flagging if anyone files "the app's cycleTime doesn't stick".

## Refs

- Session [#43 — MAP-01 stairs validation](../2026-06-12_map-01_stairs-validation/findings.md) — companion session, HA-initiated path.
- Issue [#17 — BUG-08: time.sleep(1) blocks the awscrt event-loop thread](../../../issues/17) — two more data points added.
- Issue [#31 — MAP-01: CleanModes enum has no 'stairs' value](../../../issues/31) — hypothesis source.
- PR [#35 — MAP-01: CleanModes adds STAIRS + tolerant parse](../../../pull/35) — must be redesigned per session #43's _Implications_ section before merge.
