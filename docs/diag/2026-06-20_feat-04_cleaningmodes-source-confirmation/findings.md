# FEAT-04 — `cleaningModes[mode]` is the firmware's duration source at scheduled trigger

## TL;DR

[FEAT-04 (#79)](https://github.com/raouldekezel/dolphin-robot/issues/79)
asks for a `sensor.{robot}_next_scheduled_cycle_time` exposing the
minute count the firmware will use for the next scheduled run. The
open question was _which shadow field the firmware reads at trigger
time_, since the schedule object itself
(`reported.weeklySettings.<day>.cleaningMode`,
`reported.delay.cleaningMode`) carries only `{mode}` — no `cycleTime`.

This session confirms empirically on `v1.0.26b3-raoul.4` against the
live device that the firmware reads
**`reported.cleaningModes[mode]`** at trigger time. At
`2026-06-20 11:49:04 CEST`, a weekly schedule armed ~30 s earlier
fired and the firmware reported
`cycleInfo.cleaningMode = {mode: "all", cycleTime: 180}`. The value
`180` matched `reported.cleaningModes.all` as last reported in the
shadow before the trigger.

## Context

- **Date:** 2026-06-20 11:43–11:50 CEST.
- **Robot:** Maytronics Dolphin S2000 (Nono 2), `robotType:"S4"`.
- **Integration:** `v1.0.26b3-raoul.4` ([release](https://github.com/raouldekezel/dolphin-robot/releases/tag/v1.0.26b3-raoul.4)).
- **HA:** 2026.1.3 (container `hass` on intel-nuc, `network_mode: privileged`).
- **Debug logging:** persistent `…managers.aws_client: debug` from
  `configuration.yaml`.

No probe or patch was used. The session captures unmodified
production traffic during an ad-hoc operator test, all driven from
the Maytronics phone app (start an ad-hoc cycle, abort it, then arm a
weekly schedule for ~5 min in the future).

Mechanical pre-observation (no test needed, established before the
session): `reported.weeklySettings.<day>.cleaningMode` and
`reported.delay.cleaningMode` both contain a single `mode` key. No
`cycleTime`. So at trigger time the firmware must look up the
duration elsewhere; the only candidate inside the shadow is
`reported.cleaningModes[mode]`. The session below confirms that
candidate is in fact used.

## Actions taken

`01_schedule-arming-and-trigger.mqtt.log` — 52 lines of raw AWS shadow
traffic captured during the session.

**Important attribution note.** All `desired.*` writes in the window
originate from the **Maytronics app**, not from HA. The integration
emits an `INFO` log line every time it calls `set_cleaning_mode()` or
`_set_cycle_time()` (`Set cleaning mode, Desired: …` and
`Set cycle time, Desired: …` in `aws_client.py`), and **neither
appears** anywhere between `11:43:14` and `11:50:09` in the captured
window. HA was passively reading the shadow throughout. The mode
change, the chained `cycleInfo.cycleTime` write, the abort, and the
schedule arming/re-arming are all the operator driving the
Maytronics app on a phone.

In chronological order (all times CEST):

1. **11:44:11** — `desired.cleaningMode.mode = all` pushed via the
   Maytronics app (operator starts an ad-hoc cycle in `Complete`
   mode).
2. **11:44:14** — `desired.cycleInfo.cycleTime = 180` pushed via the
   Maytronics app. The operator confirms this came from picking
   `180 min` in the app's start-cycle duration picker when launching
   the ad-hoc `Complete` cycle in step 1 — the picker value rides
   along on the next `desired.cycleInfo.cycleTime` write the app
   issues. (Later, outside the captured window, the operator started
   another `Complete` cycle at `120 min`. The shadow's long-mode
   `cleaningModes` entries — `all`, `floor`, `water`, `ultra` — all
   moved together from `180` to `120`; **`stairs` stayed at `150`**,
   unchanged. Same shape as the propagation captured in this
   session's 11:44:17 reported event.)
3. **11:44:21** — `desired.systemState.pwsState = off` pushed via the
   Maytronics app (operator aborts the ad-hoc cycle). Robot returns
   to `holdWeekly` at 11:44:24.
4. **11:44:41** — `desired.weeklySettings.saturday = {mode: all,
11:52, enabled}` pushed via the Maytronics app (operator arms a
   weekly schedule for Saturday 11:52, originally meant as "~10 min
   from now").
5. **11:48:11** — `desired.weeklySettings.saturday.time = 11:49`
   pushed via the Maytronics app (operator tightens the wait).
6. **11:49:04** — weekly scheduler fires inside the robot's firmware.
   `reported.cycleInfo.cleaningMode = {mode: "all", cycleTime: 180}`,
   `pwsState=on, robotState=init`.
7. **11:49:50** — robot confirms cycle is in progress
   (`rTurnOnCount` incremented 50 → 51 between snapshots).

## Timeline

All times CEST (UTC+02:00). Columns `cleaningModes.all` and
`cycleInfo.cleaningMode` are the values reported by the firmware at
each snapshot. `desired` rows show inbound writes from the
**Maytronics app** (HA emitted no `Set cleaning mode` / `Set cycle
time` logs in this window — see Actions taken).

| t (CEST)     | Direction                      | What                                                                                                        | `cleaningModes.all` | `cycleInfo.cleaningMode`                       |
| ------------ | ------------------------------ | ----------------------------------------------------------------------------------------------------------- | ------------------- | ---------------------------------------------- |
| 11:43:14     | reported (refresh)             | baseline snapshot, `isInRepeatMode=true`                                                                    | **150**             | `{all, 150}`                                   |
| 11:44:11     | desired (Maytronics app)       | `cleaningMode.mode = "all"` (operator starts ad-hoc cycle)                                                  | 150                 | —                                              |
| 11:44:13     | reported                       | mode-change ack, `cycleStartTime` set                                                                       | 150                 | `{all, 150}`                                   |
| 11:44:14     | reported                       | `pwsState=on, robotState=init` (cycle started)                                                              | 150                 | `{all, 150}`                                   |
| **11:44:14** | **desired (Maytronics app)**   | **`cycleInfo.cycleTime = 180` (app's own cycle-duration sync)**                                             | 150                 | —                                              |
| 11:44:17     | reported (firmware)            | `cycleInfo.cycleTime → 180`; **`cleaningModes.all → 180`**                                                  | **180**             | `{all, 180}`                                   |
| 11:44:21     | desired (Maytronics app)       | `pwsState = off` (operator aborts cycle)                                                                    | 180                 | `{all, 180}`                                   |
| 11:44:24     | reported                       | back to `holdWeekly`                                                                                        | 180                 | `{all, 180}`                                   |
| 11:44:41     | desired (Maytronics app)       | arm `weeklySettings.saturday = {mode: all, 11:52, enabled}` — **note: only `mode` is sent, no `cycleTime`** | 180                 | `{all, 180}`                                   |
| 11:44:41     | reported                       | `weeklySettings.saturday = {all, 11:52}` confirmed                                                          | 180                 | `{all, 180}`                                   |
| 11:48:11     | desired (Maytronics app)       | re-arm `weeklySettings.saturday.time = 11:49` (still only `{mode, time}`)                                   | 180                 | `{all, 180}`                                   |
| 11:48:14     | reported                       | `weeklySettings.saturday = {all, 11:49}` confirmed                                                          | 180                 | `{all, 180}`                                   |
| **11:49:04** | **reported (scheduler fired)** | `pwsState=on, robotState=init`, **`cycleInfo.cleaningMode = {mode: "all", cycleTime: 180}`**                | **180**             | **`{all, 180}` ← matches `cleaningModes.all`** |
| 11:49:50     | reported                       | cycle ongoing, `rTurnOnCount: 50 → 51`                                                                      | 180                 | `{all, 180}`                                   |

## Findings

### F1 — Schedule object carries `mode` only, no `cycleTime`

Both `reported.weeklySettings.<day>.cleaningMode` and
`reported.delay.cleaningMode` contain a single `{mode}` field. The
schedule armed at 11:44:41 (`{cleaningMode:{mode:"all"}, isEnabled,
time}`) and re-armed at 11:48:11 carries no `cycleTime`. So at
trigger time the firmware cannot retrieve the duration from the
schedule itself — it must read it from another field of the shadow.

### F2 — Firmware reads `cleaningModes[mode]` at trigger time

At 11:49:04, the scheduler fired and immediately reported
`cycleInfo.cleaningMode = {mode: "all", cycleTime: 180}`. The value
`180` matches `reported.cleaningModes.all = 180` as of the last
update at 11:44:17. `cleaningModes[mode]` is the only candidate
inside the shadow that holds a per-mode duration, and it is the value
the firmware adopted for the cycle.

Consequence for FEAT-04: the new
`sensor.{robot}_next_scheduled_cycle_time` must surface
`reported.cleaningModes[mode]` of the next-scheduled-run's `mode`.

## Implications for FEAT-04 design

1. The compute layer for `next_scheduled_cycle_time` reads
   `reported.cleaningModes[mode]` from the AWS shadow snapshot the
   coordinator already has in `self.data`. The field is not currently
   referenced anywhere in `custom_components/`; FEAT-04 introduces
   the first reader.
2. If `cleaningModes` is absent (cold-start before first AWS message)
   or `cleaningModes[mode]` is missing/non-int, the sensor returns
   `None` and goes `unavailable`.

## Limitations

- Only the **weekly** trigger path is exercised here. The **delay**
  path was visited as `delay.isEnabled=false` throughout (the existing
  `delay.startTime=13:13` from a prior session was left untouched).
  Mechanically, `reported.delay.cleaningMode` also contains only
  `{mode}` (no `cycleTime`) — see the 11:43:14 snapshot in the raw
  log — so the same conclusion holds by construction. An empirical
  verification of the delay trigger is left as a follow-up if any
  doubt remains, but is not necessary for FEAT-04 to ship.
- The cycle started at 11:49:04 was not allowed to run to completion;
  no `cycleInfo.cycleEndTime` event is captured. This does not
  affect the finding — what matters is the `cycleTime` value the
  firmware _adopted_ at trigger, which is recorded by the very first
  reported event at 11:49:04.
- No diagnostic probe or patch was used; the session is plain
  production traffic, so no rollback was needed.

## Identifiers redacted

- `REDACTED-MUSN` — the device's AWS thing name (8-char prefix).
- `REDACTED-ROBOT-SERIAL` — the device's full serial (10 chars including the
  reported-only `3Q` suffix).
- `REDACTED-WIFI-SSID` — the operator's Wi-Fi network name.
