# BUG-08 — cycle time round-trip with the BUG-08 `sleep(1)` still in place

## TL;DR

The integration's per-mode cycle time write reaches the firmware and is applied — but only when the cycle is started from Home Assistant. When started from the official Maytronics app, the app runs its own two-step sequence and writes its **user-selected** duration (the app's "Complet" picker offers 2 h / 2 h 30 / 3 h, default 2 h 30 = 150) to the same Shadow key **~1.4 s after the integration's write** (~50 ms after the firmware reported the integration's value), and **last-write-wins on AWS IoT Shadow** means the app's value is what the robot keeps.

## Context

- **Date**: 2026-06-12, local time `+02:00`.
- **Robot**: Dolphin S2000, firmware `pwsSwVersion=11.0004`, `muSwVersion=9F88`. Model identifier reported as `S4`.
- **Integration**: fork `raouldekezel/dolphin-robot`, tag `v1.0.26b3-raoul.1` (deploy commit `0d5d63e` at run time).
- **Home Assistant**: 2026.1.3, container deployment on `intel-nuc.local`.
- **HA-side per-mode cycle time configuration**: `cycle_time_all = 60` (Regular = "Complet"). All other modes left at integration defaults.
- **Robot's pre-experiment state**: cycling under the weekly schedule programmed in the Maytronics app, mode `stairs`, firmware-default duration `150` min for that mode.
- **Logging**: `custom_components.mydolphin_plus.managers.aws_client = debug` enabled persistently in `configuration.yaml` for the duration of the session.

## Actions taken

1. **`01_resume-mode-stairs-on-dolphin-app`** — Stop + Start triggered from the Maytronics mobile app, without changing the mode in the app. The intent was a pure pause/resume of the currently running `stairs` cycle.
2. **`02_start-complete-on-dolphin-app`** — Stop + Start from the Maytronics app, this time selecting "Complet" (mode `all`). The integration's HA-configured `cycle_time_all = 60` should, on paper, be applied.
3. **`03_start-complete-on-ha`** — Stop from the Maytronics app, then `vacuum.nono_2 → Start` from Home Assistant. Same target mode (`all`), same 60-minute configuration, but the trigger source is the integration this time, not the app.

For each action a parallel poll of `sensor.<robot>_cycle_time`, `…_temps_restant_du_cycle`, `…_mode_de_nettoyage`, and `…_etat_du_robot` ran in the background. The MQTT-side `.mqtt.log` files were sliced from the HA debug log using the action window only.

## Timeline

Wall-clock timestamps are local (`+02:00`); UTC second-precision conversion: `local - 02:00 = UTC`.

### Action 1 (11:41–11:44)

The Maytronics app published `desired.systemState.pwsState = "off"` and later `"on"`. No publish on `desired.cleaningMode.mode` or `desired.cycleInfo.*` (`02_start-complete-…` will show what such publishes look like for contrast). The integration's `Set cleaning mode` / `Set cycle time` lines do not appear in this slice at all: the callback that drives them keys on `desired.cleaningMode.mode`, which the app didn't touch. `sensor.cycle_time` stayed at 150 throughout.

### Action 2 (11:55–11:56)

| Timestamp | Actor | Payload | Effect |
|---|---|---|---|
| 11:55:43.845 | App | `desired.cleaningMode.mode = "all"` | triggers integration callback (mode change observed) |
| 11:55:44.846 | **Integration** | `Set cycle time, Desired: {'cycleInfo': {'cycleTime': 60}}` | publish #2, +1.001 s after the trigger (the BUG-08 `sleep(1)`) |
| 11:55:46.203 | Robot | reported `cleaningMode.cycleTime = 60` | integration's 60 was accepted by the firmware |
| 11:55:46.251 | App | `desired.cycleInfo.cycleTime = 150` | echo of an app-side write (the app emits its UI-selected duration — picker default 2 h 30 — after a mode change) |
| 11:55:47.021 | Robot | reported `cleaningMode.cycleTime = 150` | app's 150 overwrote the integration's 60 |

The integration's value held in `reported.cycleInfo.cleaningMode.cycleTime` for ~820 ms before being overwritten.

### Action 3 (12:02–12:06)

| Timestamp | Actor | Payload | Effect |
|---|---|---|---|
| 12:04:54.952 | Integration | `Set cleaning mode, Desired: {'cleaningMode': {'mode': 'all'}}` | publish #1, triggered by `vacuum.start` |
| 12:04:56.067 | Integration | `Set cycle time, Desired: {'cycleInfo': {'cycleTime': 60}}` | publish #2, +1.115 s after #1 (BUG-08 `sleep(1)` + overhead) |
| 12:05:02 | HA sensor | `sensor.cycle_time = 60` | firmware accepted 60 |
| (no app write follows) | | | |

`sensor.cycle_time` stayed at 60 for the rest of the polling window — there was no second writer to compete.

## Findings

- **The cycle-time feature does work end-to-end.** Action 3 proves that the integration's published `desired.cycleInfo.cycleTime` is honoured by the firmware and surfaces on `reported.cycleInfo.cleaningMode.cycleTime`. The earlier suspicion that the path mismatch (top-level `cycleInfo.cycleTime` vs nested `cleaningMode.cycleTime`) broke the write was ruled out — the firmware bridges the two paths transparently.
- **The app-vs-integration race during a Maytronics-app trigger is real, and the app wins.** Action 2 captured the exact sequence: the integration writes 60 (+1.0 s after the mode echo), the firmware honours it for ~820 ms, then the app writes 150 **~1.4 s after the integration's write** (~50 ms after the firmware's reported-60), and last-write-wins delivers 150 as the final state. The ~2.4 s mode→duration spacing on the app side suggests the app runs its own scheduled two-step sequence rather than reacting to our write — but one observation cannot separate the two (open question 2).
- **The BUG-08 `sleep(1)` did not distort the ordering in this trace.** The blocking window was 11:55:43.845→44.846; the app's 150 surfaced at 46.251, ~1.4 s after the awscrt thread unblocked, so its arrival timestamp is trustworthy and the write order (integration 60 → app 150) is established — corroborated by the firmware's reported sequence (60 at 46.203, 150 at 47.021). The queue-distortion caveat remains valid in general, but only for messages surfacing inside the sleep window or immediately at its end; none of this trace's decisive events fall there. What the trace cannot tell is *why* the app wrote 1.4 s later — its own scheduled second phase or a reaction to observing our 60.
- **The app's 150 is a UI choice, not a firmware default.** The current Maytronics app exposes a duration picker for "Complet": 2 h / 2 h 30 / 3 h (= 120 / 150 / 180 min), with **2 h 30 preselected**. The 150 written in action 2 is the app's default selection — which also resolves the apparent mismatch with the firmware table's `cleaningModes.all = 180` (that value is the 3 h picker option, not what the app sends by default). *Confirmed by the operator post-session from the app UI.*
- **Operator decision: the emergent semantics is the desired behaviour.** "The launcher picks the duration": HA-started cycles run the HA-configured 60, app-started cycles run the app's selection (150). Any future change to the `_set_cycle_time` trigger (e.g. restricting it to HA-initiated mode changes, which would remove the lost race and the ~820 ms transient) must preserve this semantics, and is gated on open question 2.

## Open questions

- If the BUG-08 sleep is replaced with `self._loop.call_later(1.0, …)` or removed entirely, does the app's 150 still overwrite the integration's 60 in action-2-style scenarios? The fix has to land first; the same experiment then becomes the deciding evidence.
- Does the app emit its `desired.cycleInfo.cycleTime` on **every** mode change, or only when its UI selection differs from the current Shadow value? The *value* it writes is now explained (the picker selection); the *trigger condition* rests on a single observation. This question gates any redesign of the integration's `_set_cycle_time` trigger.
- Does the app emit any other writes during a mode change that the integration's `_message_callback` doesn't currently react to (e.g. `desired.featureEn.*`, `desired.weeklySettings.*`)?

## Refs

- Tracker: [#17](https://github.com/raouldekezel/dolphin-robot/issues/17) — BUG-08 root-cause analysis.
- Convention: [`docs/diag/README.md`](../README.md).
- Conventions PR: [#40](https://github.com/raouldekezel/dolphin-robot/pull/40).
- Predecessor commit on the same investigation (analytical, no data): `0d5d63e` (= the `v1.0.26b3-raoul.1` tag).
