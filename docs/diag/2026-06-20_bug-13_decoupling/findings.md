# BUG-13 — decoupling experiments

## TL;DR

Issue [#47](https://github.com/raouldekezel/dolphin-robot/issues/47) proposed two
avenues to decouple "set cleaning mode" from "start cycle".

- **E-A (nested-path write): FAIL.** Writing `desired.cycleInfo.cleaningMode.mode = X`
  (the read-back slot) is silently ignored by the firmware — the desired slot is
  ACK'd (`desired:null`), but `reported.cycleInfo.cleaningMode.mode` does not
  change and `pwsState` stays `holdWeekly`. Long-shot branch confirmed.
- **E-B (set + immediate stop): PASS.** Running the normal start sequence
  (`cleaningMode.mode = X`, then BUG-08 chain `cycleInfo.cycleTime = N`) and
  publishing `systemState.pwsState = off` ~1.6 s after the mode write yields:
  mode + cycleTime adopted by the firmware, robot returned to `holdWeekly`,
  **no `pwsState=on` reported, no cycle-counter increment, no HA `cleaning`
  state transition**.

E-A2 (direct `pwsState=on` start) was unreachable as a follow-up of E-A1 — see
issue body. The fix direction is therefore E-B: chain mode → cycleTime → stop
inside `set_cleaning_mode` when the robot was docked at the time of the call.

## Context

- **Date:** 2026-06-20
- **Robot:** Maytronics Dolphin S2000 (Nono 2). Firmware reports
  `robotType:"S4"`, `pwsSwVersion:"11.0004"`, `muSwVersion:"9F88"`.
  Robot starts each experiment in `pwsState=holdWeekly`,
  `robotState=notConnected`.
- **Integration fork commit:** `613507f` on branch
  `patches/bug-13-decoupling` (`raouldekezel/dolphin-robot`), cut from
  `deploy@7061eda`. Adds one throwaway helper: `AWSClient.spike_publish(payload,
client_token=None)` proxied by HA service `mydolphin_plus.spike_publish`.
  Bypasses every existing helper; emits a literal `{"state":{"desired":
<payload>}, "clientToken": <token>}` on the shadow update topic. Reuses the
  per-process `self._our_token` so `_event_is_ours` provenance stays valid.
- **HA:** 2026.1.3 (container `hass` on intel-nuc, `network_mode: host`).
  Tag installed via HACS: `v1.0.26b3-bug13-probe.613507f`.
- **Debug logging:** persistent `custom_components.mydolphin_plus.managers.aws_client: debug`
  from `configuration.yaml`.

## E-A — nested-path mode write

### Action

```yaml
service: mydolphin_plus.spike_publish
data:
  payload: { cycleInfo: { cleaningMode: { mode: stairs } } }
```

Initial state: `holdWeekly`, `reported.cycleInfo.cleaningMode = {mode:"all",
cycleTime:180}`. Target mode different from current.

### Timeline

| Δ T₀         | Source   | Event                                                                                                                                             |
| ------------ | -------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| **+0.000 s** | HA       | publish `desired.cycleInfo.cleaningMode.mode = stairs`, `clientToken=325aa6f9…`                                                                   |
| +0.054 s     | AWS      | `/update/delta` arrives (delta non-empty, value differs from reported)                                                                            |
| +0.056 s     | AWS      | `/update/accepted` v1205 echoes our payload + clientToken                                                                                         |
| +1.347 s     | firmware | `/update/accepted` v1206 with `desired:null` (firmware ACK)                                                                                       |
| +5.886 s     | AWS      | `shadow/get/accepted` snapshot: `reported.systemState.pwsState = "holdWeekly"`, `reported.cycleInfo.cleaningMode.mode = "all"`, `cycleTime = 180` |

### Finding

The firmware acknowledges the write at the AWS-protocol level (clears the
`desired` slot) but does **not** propagate the value to its internal cleaning
state. `pwsState` is unchanged, `reported.cycleInfo.cleaningMode.mode` is
unchanged. The slot is firmware-output-only.

Matches scenario B of [Home Assistant - Dolphin S2000 - AWS Shadow
Structure.md §H](https://github.com/raouldekezel/it-documentation) ("firmware
ignore le path imbriqué"), with the ACK quirk noted (AWS Shadow's
device-side TTL clearance is visible even when the value is not actually
adopted). E-A is dead as a decoupling primitive.

E-A2 was conditional on E-A1 PASS — see #47. Not run.

## E-B — set + immediate stop

### Actions

1. `vacuum.set_fan_speed(fan_speed: "all")` — normal integration path.
   Robot pre-state mode was `stairs`, so this is a real mode-change and the
   BUG-08 chain fires.
2. After ~1.6 s, `mydolphin_plus.spike_publish` with
   `payload: {systemState: {pwsState: "off"}}` — direct symmetric of
   `AWSClient.pause()`. We use the probe rather than `vacuum.pause` because
   `_vacuum_pause` skips when the entity state is still `DOCKED` (the
   firmware hasn't reported `pwsState=on` yet at +1.6 s).

### Timeline

| Δ T₀           | Source   | Event                                                                                                   |
| -------------- | -------- | ------------------------------------------------------------------------------------------------------- |
| **+0.000 s**   | HA       | `Set cleaning mode, Desired: {cleaningMode: {mode: all}}` publish                                       |
| +0.060 s       | AWS      | `/update/accepted` echoes the mode write                                                                |
| **+1.068 s**   | HA       | BUG-08 chain — `Set cycle time, Desired: {cycleInfo: {cycleTime: 60}}` publish                          |
| +0.101 s after | AWS      | `/update/accepted` echoes the cycleTime write                                                           |
| **+1.624 s**   | HA       | `[BUG-13 spike_publish] payload={systemState: {pwsState: off}}`                                         |
| +1.711 s       | AWS      | `/update/accepted` echoes `pwsState=off` write                                                          |
| +2.815 s       | firmware | reported `cycleInfo.cleaningMode = {mode:"all", cycleTime:60}` adopted                                  |
| +3.736 s       | firmware | reported `systemState.pwsState = "holdWeekly"`, `robotState = "notConnected"`, `rTurnOnCount` unchanged |

### Finding

The firmware **never publishes `pwsState=on`** in this run. The stop write
lands in the firmware's queue before it finishes processing the mode-write
start, and the net effect on `reported` is: mode and cycleTime adopted,
`pwsState` stays `holdWeekly`.

Side effects measured:

- `sensor.nono_2_nombre_de_cycles`: unchanged (firmware reports
  `rTurnOnCount` at the same value before and after).
- `vacuum.nono_2` HA state: stayed `docked` throughout — no `cleaning`
  transition, no recorder blip.
- Maytronics app cross-check: displays the new mode and cycleTime as the
  current selection.

Note on variance: the +1.624 s spike timing is tuned to land the stop
between the BUG-08 chain completion (~+1.07 s) and the firmware's
`pwsState=on` reaction (~+2.5 s observed in BUG-13 reconfirmation runs).
Faster WAN latency widens the window; slower latency narrows it. A
production-grade fix should be event-driven (await the AWS `desired:null`
ACK of the cycleTime write before publishing the stop) rather than
sleep-based.

## Outcome matrix for #47

| Test                                  | Issue #47 prediction                     | Observed                                                                  |
| ------------------------------------- | ---------------------------------------- | ------------------------------------------------------------------------- |
| E-A1 (nested-path silent set)         | "long shot" — silent-ignore branch noted | **FAIL** (silent ignore, firmware ACK without adoption)                   |
| E-A2 (`pwsState=on` standalone start) | conditional on E-A1 PASS                 | **n/a** (E-A1 failed, E-A2 unreachable as designed)                       |
| E-B (set + immediate stop)            | partial workaround                       | **PASS** — full mode + cycleTime adoption with no observable side effects |

**Fix direction for the integration:** in `set_cleaning_mode`, when the
robot was `holdWeekly` at the time of the call, append a `pause()` call
gated on the firmware's `desired:null` ACK of the BUG-08 cycleTime write
(not a fixed sleep). The select then writes to the firmware cleanly:
mode and cycleTime adopted, no auto-start.
