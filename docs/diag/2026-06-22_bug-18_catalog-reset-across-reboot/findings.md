# BUG-18 — `cleaningModes` catalog reset across PWS reboot, and the firmware uses the persisted `cycleInfo` slot at schedule trigger

## TL;DR

`sensor.{robot}_next_scheduled_cycle_time` (FEAT-04, `v1.0.26b3-raoul.9+`) reads `reported.cleaningModes[next_scheduled_mode]` from the AWS shadow. On 2026-06-22 the sensor predicted **180 min** for the next scheduled cycle; the cycle that actually fired at 11:00 CEST ran in **150 min**. Root cause: the firmware uses `reported.cycleInfo.cleaningMode.cycleTime` (a slot that persists across PWS reboot) at schedule trigger, while `cleaningModes.<mode>` (read by FEAT-04) is **reset to firmware defaults at every PWS reboot**. The operator's `Plug Nono coupé la nuit` automation cuts and restores the PWS every night, so the two slots diverge by morning, every morning.

Captured here:

- **`transitions_24h.txt`** — every `reported.cleaningModes.all` transition over 30 h, decoded with surrounding context and any HA-side writes in the same window. Five transitions, two of them at PWS reboots (06:00 UTC on both days), both setting `all → 180` regardless of the pre-cut value.
- **`slice_01_pws_reconnect.mqtt.log`** — raw shadow stream around the 2026-06-22 06:00 UTC reconnect: the catalog jump from 60 → 180 happens in the **first reported payload after reconnect** (06:00:14.201), while `cycleInfo.cleaningMode = {stairs, 150}` is preserved intact.
- **`slice_02_schedule_fire.mqtt.log`** — raw shadow stream around the 2026-06-22 09:00 UTC weekly trigger for mode `all`: pre-fire state has `cleaningModes.all = 180` _and_ `cycleInfo.cleaningMode.cycleTime = 150`; post-fire `cycleInfo.cleaningMode = {all, 150}` — the **150 wins, not the 180**.

## Context

- **Date:** 2026-06-22 (capture spans 2026-06-21 04:52 UTC → 2026-06-22 10:51 UTC, ~30 h).
- **Tag installed via HACS:** `v1.0.26b3-raoul.10` (BUG-13 fix + diag from #85; FEAT-04 code unchanged since `raoul.9`).
- **Robot:** Maytronics Dolphin S2000 ("Nono 2"). Firmware `pwsSwVersion="11.0004"`, `muSwVersion="9F88"`, `robotType="S4"`.
- **HA:** 2026.1.3 (container `hass` on intel-nuc, `network_mode: host`).
- **Operator automation in scope:** `Plug Nono coupé la nuit` — the PWS plug is cut every night and restored in the morning (~06:00 UTC = 08:00 CEST). Each cut/restore is a full PWS reboot. Two such reboots are captured in this window (06:00:13 on 06-21 and 06:00:14 on 06-22).

## 24 h trace — `cleaningModes.all` transitions

Full source in `transitions_24h.txt`. Summary:

| UTC                                     | catalog.all               | `cycleInfo.cleaningMode`                                | systemState                             | Cause                                                                                                                    |
| --------------------------------------- | ------------------------- | ------------------------------------------------------- | --------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| 2026-06-21 04:52:29                     | None → 120                | `{floor, 120}`                                          | `holdWeekly`, `notConnected`, `rTOC=57` | First payload of the capture; catalog reported for the first time                                                        |
| **2026-06-21 06:00:13**                 | 120 → **180**             | `{stairs, 150}`                                         | `holdWeekly`, `notConnected`, `rTOC=57` | **PWS reboot (`Plug Nono coupé la nuit`)** — catalog reset to default; cycleInfo preserved                               |
| 2026-06-21 11:32:17                     | 180 → 150                 | (no context — null payload)                             | (null)                                  | Firmware-side update (no HA write between previous and this transition)                                                  |
| 2026-06-21 12:33:19                     | 150 → 120                 | `{floor, 120}`                                          | `on`, `scanning`, `rTOC=59`             | BUG-13 in-vivo Action 3 — our `Set cycle time=120` (for `floor`) bumped `cleaningModes.all` too (Q2 quartet propagation) |
| 2026-06-21 12:38:18                     | 120 → **60**              | `{all, 60}`                                             | (null)                                  | BUG-13 in-vivo Action 4 — our `Set cycle time=60` (for `all`) ✅                                                         |
| (2026-06-21 evening → 2026-06-22 night) | 60 stable                 | settles to `{stairs, 150}` after the in-vivo cycle ends | —                                       | No HA writes in this window                                                                                              |
| **2026-06-22 06:00:14**                 | 60 → **180**              | `{stairs, 150}`                                         | `holdWeekly`, `notConnected`, `rTOC=59` | **PWS reboot — catalog reset to default again; cycleInfo preserved.** No HA write in the window.                         |
| (later 2026-06-22)                      | 180 stable until schedule | refer to slice_02 below                                 |                                         |                                                                                                                          |

**Key observation:** the two reboot transitions (06-21 and 06-22) both reset `cleaningModes.all` to **180**, regardless of the value held before the cut (120 on 06-21, 60 on 06-22). Same applies to `floor`, `water`, `ultra` — these four modes form the propagation group of QUIRK-01 Q2, and they share the firmware's hard-coded factory default of 180.

## Slice 1 — PWS reconnect (2026-06-22 ~06:00 UTC)

`slice_01_pws_reconnect.mqtt.log`. Window 05:55:00 → 06:05:00 UTC. The relevant transitions, decoded:

| UTC                 | `isConnected.connected` | `cleaningModes.all` | `cycleInfo.cleaningMode`        | `desired` echoed                                                 |
| ------------------- | ----------------------- | ------------------- | ------------------------------- | ---------------------------------------------------------------- |
| 05:55:02 → 06:00:12 | **false**               | 60                  | `{all, 60}`                     | None                                                             |
| **06:00:12.824**    | (null)                  | (null)              | (null)                          | `{dynamicTopics: [...], robotSerial: ...}` — reconnect handshake |
| **06:00:14.201**    | **true**                | **180** (reset)     | **`{stairs, 150}`** (preserved) | None — firmware-published `reported`                             |
| 06:00:16+ steady    | true                    | 180                 | `{stairs, 150}`                 | None                                                             |

The two slots' divergence is created in this single ~2 s reconnect window:

- `cleaningModes.all` was last reported at **60** (our HA-pushed value from BUG-13 in-vivo Action 4 yesterday) before disconnection at 05:55. In the first post-reconnect `reported`, it has jumped to **180** — the firmware's hard-coded default, NOT what the shadow held when the PWS went offline. The custom value is lost.
- `cycleInfo.cleaningMode` was last reported at **`{stairs, 150}`** (the cycle that started yesterday at 14:30 settled there) before disconnection. After reconnect, **same value, byte-identical**. This slot is persistent across the reboot.

Identical pattern at the 2026-06-21 06:00:13 reboot (captured in `transitions_24h.txt`): catalog jumped from 120 → 180, `cycleInfo` preserved as `{stairs, 150}`. Two reboots, same shape — reproducible.

**Implication for QUIRK-01 Q3** (firmware-side gotcha): `cleaningModes.<mode>` is volatile state; `cycleInfo.cleaningMode.{mode, cycleTime}` (and by extension `nextCycleInfo`, `weeklySettings`) are durable. Any contract that relies on the catalog holding a custom value after a reboot is broken by construction.

## Slice 2 — weekly schedule trigger (2026-06-22 ~09:00 UTC)

`slice_02_schedule_fire.mqtt.log`. Window 08:55:00 → 09:05:00 UTC. The robot was idle (`holdWeekly`/`notConnected`); the Monday 11:00 CEST = 09:00 UTC weekly slot for mode `all` was about to fire.

| UTC              | `cleaningModes.all`           | `cycleInfo.cleaningMode`               | `nextCycleInfo.{mode, duration}` | `systemState`                            |
| ---------------- | ----------------------------- | -------------------------------------- | -------------------------------- | ---------------------------------------- |
| 08:55:22         | **180** (post-reboot default) | `{stairs, 150}` (persisted from 06-21) | `{stairs, 150}` (persisted)      | `holdWeekly, notConnected, rTOC=59`      |
| 08:56–08:59      | 180 (steady)                  | `{stairs, 150}` (steady)               | `{stairs, 150}` (steady)         | `holdWeekly, notConnected, rTOC=59`      |
| **09:00:03.093** | (null)                        | **`{all, 150}`** ← cycle started       | `{all, 150}`                     | **`on, init, rTOC=59`**                  |
| 09:00:03.950     | (null)                        | (null)                                 | `{None, None}` (cleared)         | `on, init, rTOC=59`                      |
| 09:00:42.807     | 180 (steady)                  | `{all, 150}`                           | `{all, 150}` (republished)       | `on, init, rTOC=59`                      |
| **09:00:50.015** | (null)                        | `{all, 150}`                           | `{None, None}` (cleared)         | `on, init, **rTOC=60**` ← counter ticked |
| 09:01:07.097     | (null)                        | `{all, 150}`                           | (null)                           | `on, **scanning**, rTOC=60`              |
| 09:01:17.158     | 180 (steady)                  | `{all, 150}`                           | `{all, 150}` (steady)            | `on, scanning, rTOC=60`                  |

The cycle that the firmware actually started at 09:00:03 used **mode = `all` (from the schedule) + cycleTime = `150`** — **NOT** `cleaningModes.all = 180` and NOT a hard-coded default. The firmware took the duration from the persisted slot (`cycleInfo.cleaningMode.cycleTime` or `nextCycleInfo.nextCycleDuration`, both held 150 from yesterday's BUG-13 Action 1 / Action 2 BUG-08 chain writes).

`rTurnOnCount` ticked 59 → 60 between 09:00:42 and 09:00:50 — the firmware's official "cycle launched" event. `robotState` then transitioned `init → scanning` at 09:01:07, ~64 s after the schedule fired.

**FEAT-04 sensor state at the relevant moments:**

| When                                                                              | What the sensor would have shown (reads `cleaningModes[mode]`) | What the firmware would actually use (reads persisted `cycleInfo`/`nextCycleInfo`) | Discrepancy    |
| --------------------------------------------------------------------------------- | -------------------------------------------------------------- | ---------------------------------------------------------------------------------- | -------------- |
| 2026-06-22 08:00 CEST (between reboot and schedule)                               | 180 — wrong                                                    | 150                                                                                | **30 min off** |
| 2026-06-22 12:12 CEST (current, cycle running, next is tomorrow 11:00 CEST `all`) | 180 — still wrong                                              | 150 (until something rewrites `cycleInfo`)                                         | **30 min off** |

## `cleaningModes` is a follower, sometimes desynchronized

Re-observation post-cycle (2026-06-22 ~12:30 UTC, after the 09:00 UTC cycle completed):

```
cycleInfo.cleaningMode.cycleTime   = 150   (mode=all)
nextCycleInfo.nextCycleDuration    = 150   (mode=all)
cleaningModes.{all,floor,water,ultra} = 150   ← all four bumped from 180 to 150
cleaningModes.stairs               = 150   (unchanged)
cleaningModes.short                = 60    (unchanged)
cleaningModes.pickup               = 12    (unchanged)
cleaningModes.{cove,spot,wall,ticTac,custom} = 120 (unchanged)
```

So `cleaningModes` **is not authoritative — it is a _follower_** of the running/recent cycle:

- After PWS reboot, the catalog holds firmware factory defaults (`all/floor/water/ultra = 180`).
- The next cycle that runs (whether scheduled, started by HA, or started by the app) drives the firmware to overwrite the `all/floor/water/ultra` quartet (QUIRK-01 Q2 propagation group) to the running cycle's `cycleTime`.
- The quartet then tracks the running cycle's `cycleTime` until a new mode change reassigns them again.
- `stairs`, `short`, `pickup`, and the non-enum modes (`cove/spot/wall/ticTac/custom`) are NOT in the propagation group — they hold their own values independently.

The "wrong-prediction" window for FEAT-04 is therefore not "every morning until somebody picks a mode" but **specifically the gap between the PWS reconnect and the next cycle actually running**. Today's timeline made the asymmetry visible:

| Phase (2026-06-22, UTC)         | catalog.all                                | `cycleInfo.cleaningMode.cycleTime` | FEAT-04 sensor (today) | Firmware would use |
| ------------------------------- | ------------------------------------------ | ---------------------------------- | ---------------------- | ------------------ |
| 06:00:14 (PWS reconnect)        | **180** (reset to default)                 | 150 (persisted)                    | 180 ❌                 | 150 ✅             |
| 06:00 → 09:00 (idle morning)    | 180 (no follower trigger)                  | 150                                | 180 ❌                 | 150 ✅             |
| 09:00:03 (schedule fires)       | 180 momentarily, then propagation kicks in | 150 (running cycle)                | 180 → 150 transition   | 150 ✅             |
| ~09:01 → ~11:30 (cycle running) | 150 (Q2 propagation synced)                | 150                                | 150 ✅                 | 150 ✅             |
| ~11:30 → next reboot            | 150 (steady, latched by last cycle)        | 150 (steady)                       | 150 ✅                 | 150 ✅             |

**Practical impact for this operator**: ~3 h/day of FEAT-04 wrong prediction (06:00 UTC reboot → 09:00 UTC schedule). Outside that window, the follower has caught up and the sensor reads the same value the firmware will use.

This nuances the BUG-18 framing but does not change the fix direction. The catalog being a follower (not an authoritative source) is exactly why FEAT-04 sourcing from it is structurally wrong — the prediction is correct only by coincidence whenever the follower has caught up.

## Why this matters (and why it will recur every day)

The operator's `Plug Nono coupé la nuit` automation guarantees a daily reset window. Every morning between ~08:00 CEST (PWS reconnect) and ~11:00 CEST (weekly schedule for `all` fires), the catalog holds firmware defaults and the FEAT-04 sensor predicts the wrong value. As soon as the morning cycle actually runs, the Q2 follower propagation re-aligns the catalog and the sensor agrees with the firmware again — until tomorrow morning's reboot.

The bug is **not** "the catalog is sometimes wrong" — the catalog is _structurally_ a non-authoritative follower of the running cycle. The firmware's source of truth for the next cycle's duration is the persisted slot. FEAT-04 needs to source from there.

## Proposed fix direction (for #88 / BUG-18)

In `common/next_scheduled_run.py::_resolve_cycle_time_minutes`, replace the current `cleaning_modes.get(mode)` lookup with a three-source resolver:

1. **Primary** — `reported.cycleInfo.cleaningMode.cycleTime`, gated on `pwsState != "on"` (slot reflects the upcoming cycle only when no cycle is running).
2. **Secondary** — `reported.nextCycleInfo.nextCycleDuration` (preferred when a cycle is running, since `cycleInfo` reflects the running cycle).
3. **Fallback** — `cleaningModes[mode]`, used only when neither of the above is available (e.g. cold start before any cycle ever ran).

Sentinel rejection unchanged from current FEAT-04 (positive int gate).

Pure-logic unit test: synthesize `aws_data` with `cycleInfo.cleaningMode.cycleTime ≠ cleaningModes[mode]`, assert the resolver returns the `cycleInfo` value when the robot is idle, the `nextCycleInfo` value when the robot is running, and the catalog value only when both are absent.

## See also

- **#88** — BUG-18 issue body (this session is the empirical foundation).
- **#81** QUIRK-01 Q3 — the persistence asymmetry as a firmware-side gotcha (added in this same series).
- `docs/diag/2026-06-20_feat-04_cleaningmodes-source-confirmation/findings.md` — the FEAT-04 anchoring session. Single observation, catalog and `cycleInfo` happened to be aligned. **Superseded** by the present session for the multi-slot reality on a robot with daily reboots.
- `docs/diag/2026-06-21_bug-13_in-vivo-validation/findings.md` — the in-vivo validation that produced the HA-written values (catalog=60 on 06-21 evening) seen at the start of the 06-22 reboot transition.
- [Home Assistant - Dolphin S2000 - AWS Shadow Structure](https://github.com/raouldekezel/it-documentation) §A — wording on `reported.cycleInfo.cleaningMode` to be amended (currently calls it "État du cycle en cours", which is misleading both for running-robot mode picks per BUG-13 in-vivo and for reboot persistence per this session).
