# BUG-18 — `cycleInfo.cycleTime` vs `nextCycleInfo.nextCycleDuration` synchronization across cycle lifecycle events

## TL;DR

Operator-driven sequence on 2026-06-23 between 08:50 and 09:24 UTC: baseline observation post-PWS-reboot, then start/stop of a 2 h cycle, schedule edit (Tuesday 11:15), start/stop of a 3 h cycle, then the 11:15 CEST weekly schedule fire. Two shadow slots tracked at each event: `reported.cycleInfo.cleaningMode.cycleTime` and `reported.nextCycleInfo.nextCycleDuration`. Purely factual — no interpretation pass.

## Context

- **Date:** 2026-06-23 (live operator session).
- **Tag installed via HACS:** `v1.0.26b3-raoul.10`.
- **Robot:** Maytronics Dolphin S2000 ("Nono 2"). Firmware `pwsSwVersion="11.0004"`, `muSwVersion="9F88"`, `robotType="S4"`.
- **HA:** 2026.1.3 (container `hass` on intel-nuc, `network_mode: host`).
- **Notable preceding event:** PWS rebooted ~06:00 UTC this morning (`Plug Nono coupé la nuit` automation; cf. `docs/diag/2026-06-22_bug-18_catalog-reset-across-reboot/findings.md`).
- **Source files in this session:**
  - `slice_08-50_to_09-30_utc.mqtt.log` — raw `Payload:` / `Set cleaning|cycle|power` lines from the HA container, ANSI-stripped and PII-redacted.
  - `transitions.txt` — decoded value changes for `cycleInfo`/`nextCycleInfo` + every `DESIRED` echo + every HA-side `Set …` write.

## Factual table

Each row is an event observed directly in the trace. **Time** = UTC. **Δ from previous row** included for readability. **cycleInfo** = `reported.cycleInfo.cleaningMode.cycleTime`. **nextCycle** = `reported.nextCycleInfo.nextCycleDuration`. **Mode** = the mode tag of whichever slot non-`None` (both slots track the same mode value at any given moment in this session).

> **Reading the `None` rows — they are not firmware events.** `None` means the field was **absent from that particular delta payload**, not that the firmware cleared it. AWS streams partial shadow deltas, and the integration's cumulative merge (`managers/aws_client.py` `_message_callback`) never clears a category, so the merged shadow the sensors actually read **persists** the last value. The "cleared" / "re-populated" wording in the table therefore describes delta *presence*, not firmware state changes — these are non-events at the sensor level, and the relative `None` frequency of the two slots is a streaming artifact, not a property of either slot. Likewise, the "App opened (?)" guesses infer nothing firmware-side: a bare `desired={}` echo is PWS-firmware-authored (cf. SPIKE-02 E3b), not evidence of app activity. **The one firmware-level finding this session supports:** whenever both slots appear in the same full payload, they hold the same numeric value — they were never observed to differ.

| Time (UTC)   | cycleInfo | nextCycle  | Mode   | pws / robot / rTOC               | Triggering event                                                                                    |
| ------------ | --------- | ---------- | ------ | -------------------------------- | --------------------------------------------------------------------------------------------------- |
| 08:50:11     | 150       | 150        | stairs | `holdWeekly / notConnected / 60` | Baseline post-PWS-reboot. Slots persisted from yesterday.                                           |
| 08:58:39     | 150       | None       | stairs | (idle)                           | App opened (?): `desired={}` echo; nextCycle briefly cleared, then restored to 150 ~64 ms later.    |
| 08:58:58     | 150       | None       | —      | (idle)                           | Operator selected mode `all` (DESIRED `{cleaningMode: {mode: all}}`). nextCycle cleared.            |
| 08:59:00     | 150       | 150        | all    | `on / init / 60`                 | Firmware accepted mode write; cycle starting. nextCycle re-populated at 150.                        |
| 08:59:01     | 150       | None       | all    | `on / init / 60`                 | Operator-chosen 2 h preset pushed (DESIRED `{cycleInfo: {cycleTime: 120}}`). nextCycle cleared.     |
| 08:59:03     | **120**   | **120**    | all    | `on / init / 60`                 | Firmware adopted the 120-min cycleTime. Both slots now agree at 120.                                |
| 08:59:47     | 120       | None       | all    | `on / init / 61`                 | `rTurnOnCount` 60 → 61 (cycle officially launched). nextCycle cleared on cycle launch.              |
| 09:00:23     | 120       | 120        | all    | `on / scanning / 61`             | Robot enters `scanning` phase; nextCycle re-populated at 120.                                       |
| 09:00:32     | 120       | None       | all    | `on / scanning / 61`             | Operator stop: DESIRED `{systemState: {pwsState: off}}`. nextCycle cleared on stop.                 |
| 09:01:13     | 120       | 120        | all    | `holdWeekly / notConnected / 61` | Firmware adopted the stop; back to `holdWeekly`. nextCycle re-populated at 120.                     |
| 09:01:59     | 120       | None       | all    | (idle)                           | App opened (?): `desired={}` echo; nextCycle cleared.                                               |
| 09:02:12     | 120       | None       | all    | (idle)                           | **Schedule edit:** DESIRED `{weeklySettings: {tuesday: {time: 11:15, mode: all}, triggeredBy: 0}}`. |
| 09:02:22     | 120       | 120        | all    | `holdWeekly / notConnected / 61` | Shadow refresh after the schedule edit; nextCycle re-populated at 120.                              |
| 09:06:24     | 120       | None       | all    | (idle)                           | App opened (?): `desired={}` echo; nextCycle cleared.                                               |
| 09:06:33     | 120       | None       | —      | (idle)                           | Operator selected mode `all` again (DESIRED `{cleaningMode: {mode: all}}`).                         |
| 09:06:34     | 120       | 120        | all    | `holdWeekly / notConnected / 61` | Firmware ack. nextCycle re-populated at 120.                                                        |
| 09:06:36     | 120       | None       | all    | `on / init / 61`                 | Cycle starting; operator-chosen 3 h preset pushed (DESIRED `{cycleInfo: {cycleTime: 180}}`).        |
| 09:06:39     | **180**   | **180**    | all    | `on / init / 61`                 | Firmware adopted the 180-min cycleTime. Both slots now agree at 180.                                |
| 09:07:22     | 180       | None       | all    | `on / init / 62`                 | `rTurnOnCount` 61 → 62 (cycle officially launched).                                                 |
| 09:07:43     | 180       | 180        | all    | `on / init / 62`                 | nextCycle re-populated at 180 during `init`.                                                        |
| 09:08:01     | 180       | None       | all    | `on / scanning / 62`             | Robot enters `scanning`; nextCycle cleared.                                                         |
| 09:08:33     | 180       | 180        | all    | `on / scanning / 62`             | nextCycle re-populated at 180.                                                                      |
| 09:09:17     | 180       | None       | all    | `on / scanning / 62`             | App opened (?): `desired={}` echo; nextCycle cleared.                                               |
| 09:09:18     | 180       | None       | all    | `on / scanning / 62`             | Operator stop: DESIRED `{systemState: {pwsState: off}}`.                                            |
| 09:09:56     | 180       | 180        | all    | `holdWeekly / notConnected / 62` | Firmware adopted the stop; back to `holdWeekly`. nextCycle re-populated at 180.                     |
| 09:10:03     | 180       | None       | all    | (idle)                           | App opened (?): `desired={}` echo; nextCycle cleared.                                               |
| 09:10:36     | 180       | 180        | all    | `holdWeekly / notConnected / 62` | Shadow refresh; nextCycle re-populated at 180.                                                      |
| 09:15:03     | 180       | None       | all    | (idle)                           | (transient nextCycle clear, no observable DESIRED)                                                  |
| **09:15:13** | 180       | 180        | all    | `on / init / 62`                 | **Weekly schedule fire** (Tuesday 11:15 CEST = 09:15 UTC, mode `all`). Cycle starts in 180 min.     |
| 09:15:49     | 180       | None       | all    | `on / init / 63`                 | `rTurnOnCount` 62 → 63 (scheduled cycle officially launched).                                       |
| 09:16:19     | 180       | 180        | all    | `on / scanning / 63`             | Robot enters `scanning`; nextCycle re-populated.                                                    |
| 09:21:42     | 180       | None       | all    | (running)                        | App opened (?): `desired={}` echo; nextCycle cleared, then restored.                                |
| 09:21:45     | 180       | 180        | all    | `on / scanning / 63`             | nextCycle re-populated.                                                                             |
| 09:23:19     | 180       | None / 180 | all    | (running)                        | App opened (?): `desired={}` echo; nextCycle toggles.                                               |
| 09:23:21     | 180       | None       | all    | `on / scanning / 63`             | Operator stop: DESIRED `{systemState: {pwsState: off}}`.                                            |
| 09:23:59     | 180       | 180        | all    | `holdWeekly / notConnected / 63` | Firmware adopted the stop; back to `holdWeekly`. nextCycle re-populated at 180.                     |

## See also

- [BUG-18 issue #88](https://github.com/raouldekezel/dolphin-robot/issues/88) — sensor sources the wrong shadow slot.
- [QUIRK-01 issue #81](https://github.com/raouldekezel/dolphin-robot/issues/81) Q3 — firmware-side persistence asymmetry (cleaningModes resets at PWS reboot, cycleInfo / nextCycleInfo persist).
- `docs/diag/2026-06-22_bug-18_catalog-reset-across-reboot/findings.md` — the 30 h capture spanning a PWS reboot that anchors BUG-18.
- `docs/diag/2026-06-20_feat-04_cleaningmodes-source-confirmation/findings.md` — the original FEAT-04 source-selection session, superseded for the multi-slot reality.
