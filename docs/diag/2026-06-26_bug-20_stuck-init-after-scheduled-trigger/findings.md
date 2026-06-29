# BUG-20 — Scheduler trigger leaves firmware in stuck-init, `rTurnOnCount` reset to sentinel 255, shadow silent for the entire 2 h 47 cycle

## TL;DR

Following two consecutive BUG-19 (#96) reproductions earlier the same
morning (08:43–08:47 CEST mode-pick storm), the operator left the robot
idle at the dock with cycle catalog at `mode=short, cycleTime=60` and let
the regular Friday 11:00 CEST weekly schedule fire on its own.

The schedule trigger fired at 11:00:03 CEST. Within 1 s:

- `rTurnOnCount` jumped **67 → 255** in a single shadow update — a 188-step
  increment that is physically impossible (one cycle = one increment, cf.
  BUG-18 #88 body which documents `rTurnOnCount: 59 → 60` as the normal
  step). 255 = `0xFF` looks like an uninitialised-register / sentinel
  value the firmware writes when its internal state machine cannot
  produce a real count.
- `pwsState` transitioned `holdWeekly → on`, `robotState → init`.
- `cycleStartTimeUTC` stamped `1782464384` (= 10:59:44 CEST).

The robot then **physically ran a complete cycle** for 2 h 47 min (proven
by the upstream smart-plug power trace: 79 W → 14–140 W oscillations
between 11:00:26 and 13:47:41 CEST, returning to 5 W standby afterwards).

But **the AWS shadow did NOT publish a single update to
`systemState.pwsState`, `systemState.robotState`, `systemState.rTurnOnCount`,
or `cycleInfo.cycleStartTimeUTC` during the entire 2 h 47 cycle**. The
last shadow transition we observed before the cycle physically ended was
the `09:00:04` UTC entry — `pwsState=on / robotState=init / rTurnOnCount=255`
— and that tuple persisted unchanged through `09:00:04 → 13:00 UTC`.

Operator-facing consequence: the HA dashboard shows **0 % progression,
"Analyse" status, "Nono 2 Nombre de cycles = 255"** for the whole run
and beyond. The integration mirrors the broken shadow honestly — every
sensor downstream (`sensor.{robot}_statut`, `..._etat_du_robot`,
`..._nombre_de_cycles`, `..._temps_restant_du_cycle`, the template
`..._progression` in `pool_package.yaml`) is correct given its input,
the input is just wrong.

There is also a silent error-masking side-effect: `coordinator.py:902`
gates error code surfacing on `error_turn_on_count == rTurnOnCount`. With
`rTurnOnCount=255`, any real error stamped with a sane `turnOnCount`
(≤67) will fail the equality and be silently dropped.

## Context

- **Date**: 2026-06-26
- **Robot**: Maytronics Dolphin S2000 (`Nono 2`), `robotType=S4`
- **Firmware**: `pwsSwVersion="11.0004"`, `muSwVersion="9F88"`
- **Fork tag installed via HACS**:
  [`v1.0.26b3-raoul.12`](https://github.com/raouldekezel/dolphin-robot/releases/tag/v1.0.26b3-raoul.12)
  (same as BUG-19 #95)
- **HA version**: Home Assistant 2026.1.3 (Docker, container `hass` on
  intel-nuc, `network_mode: host`)
- **Pre-experiment state**: post-overnight PWS reboot (`Plug Nono coupé
la nuit` automation, ~04:00 UTC). At 04:45:50 UTC the shadow reports
  `pwsState=holdWeekly, robotState=notConnected, rTurnOnCount=66,
cycleInfo.cleaningMode={all, 60}` — a sane baseline.
- **Notable preceding event**: BUG-19 (#96) reproduction session at
  06:43:22 → 06:47:24 UTC (= 08:43–08:47 CEST). Two consecutive
  silent-E-B failures observed inline; one cycle committed
  (`rTurnOnCount 66 → 67` at 06:45:20 UTC) before the operator paused.
  After 06:47:24 the robot was left at dock in
  `pwsState=holdWeekly, robotState=notConnected, rTurnOnCount=67,
cycleInfo.cleaningMode={short, 60}` — i.e. **cycle catalog at 1 hour,
  not the schedule's default 2 h** — and the scheduler was left to fire
  on its own ~2 h 12 min later.
- **Test intent**: confirm whether the operator-left `cycleTime=60` would
  be honoured by the weekly scheduled cycle (this is the persistence
  story tested by BUG-18 #88's fix in `cycleInfo.cycleTime`).
- **Source files in this session**:
  - `shadow_transitions.txt` — every change in
    `(pwsState, robotState, rTurnOnCount, cycleStartTimeUTC, cycleTime, mode)`
    extracted from the HA container logs between 04:45 and 13:00 UTC,
    PII redacted.
  - `power_trace_08-00_to_13-00_utc.txt` — decimated power readings from
    the upstream smart-plug (Shelly SP1, `sensor.sps_04_nono_puissance`),
    proof that the robot physically executed the cycle.
  - `sensors_pre_restart.txt` — HA-side sensor snapshot taken just
    before the recovery power-cycle, showing all downstream sensors
    still stuck on the 11:00 values at 14:43 CEST.

## Timeline

All times UTC. Shadow rows pulled from `shadow_transitions.txt`. Power
rows pulled from `power_trace_08-00_to_13-00_utc.txt`. Source-of-truth
columns marked.

### Phase 1 — Post-overnight baseline (PWS reboot persistence)

| Time UTC | pwsState   | robotState   | rTOC | cycleTime | mode   | Note                                                                                            |
| -------- | ---------- | ------------ | ---- | --------- | ------ | ----------------------------------------------------------------------------------------------- |
| 04:45:50 | holdWeekly | notConnected | 66   | 60        | all    | Shadow republishes after PWS reboot. Baseline: `cycleInfo` persisted from yesterday's last run. |
| 06:00:22 | holdWeekly | notConnected | 66   | 150       | stairs | App-driven (?) mode/cycleTime echo. Operator not at HA yet.                                     |

### Phase 2 — BUG-19 (#96) reproduction session (operator at HA)

Operator drives `select.{robot}_clean_mode` and (in two cases) `vacuum.pause`
from the dashboard. Sequence summarized; raw transitions in
`shadow_transitions.txt`.

| Time UTC | pwsState   | robotState   | rTOC   | cycleTime | mode   | Action                                                                                            |
| -------- | ---------- | ------------ | ------ | --------- | ------ | ------------------------------------------------------------------------------------------------- |
| 06:43:22 | holdWeekly | notConnected | 66     | 60        | short  | Operator pick #1: `short`. Silent E-B holds (no `pwsState=on`).                                   |
| 06:43:52 | **on**     | **init**     | 66     | 120       | all    | Operator pick #2 (`all`) within ~30 s — silent E-B **fails** (BUG-19 repro #1). Cycle starts.     |
| 06:43:56 | holdWeekly | notConnected | 66     | 120       | all    | Operator `vacuum.pause`. Cycle aborted ~4 s in, `rTOC` does not bump (too short to commit).       |
| 06:44:26 | on         | init         | 66     | 120       | floor  | Operator pick #3 (`floor`) ~30 s later — silent E-B fails again. Cycle starts.                    |
| 06:45:20 | on         | init         | **67** | 120       | floor  | `rTurnOnCount` bumps 66 → 67 (firmware committed the cycle).                                      |
| 06:45:23 | holdWeekly | notConnected | 67     | 120       | floor  | Operator `vacuum.pause` again, ~57 s into the run.                                                |
| 06:45:43 | holdWeekly | notConnected | 67     | 120       | all    | Operator pick: `all`. Silent E-B holds.                                                           |
| 06:46:03 | holdWeekly | notConnected | 67     | 150       | stairs | Operator pick: `stairs`. Silent E-B holds.                                                        |
| 06:46:29 | on         | init         | 67     | 120       | floor  | Operator pick: `floor` ~26 s later — silent E-B fails (BUG-19 repro #2). Cycle starts.            |
| 06:46:48 | holdWeekly | notConnected | 67     | 120       | floor  | Operator `vacuum.pause` ~19 s in. Too short to commit; `rTOC` stays at 67.                        |
| 06:47:20 | on         | init         | 67     | 60        | short  | Operator pick #N: `short`. Silent E-B fails. Cycle "starts" briefly.                              |
| 06:47:24 | holdWeekly | notConnected | 67     | 60        | short  | Last operator pause. Final catalog state: **cycleInfo = {short, 60}, rTOC = 67**. Operator stops. |

The operator then walked away. No further HA-side writes until the
scheduler fires.

### Phase 3 — 2 h 13 min quiet (robot truly idle)

No shadow transitions between 06:47:24 UTC and 09:00:03 UTC. Power trace
holds at ~3 W standby (PSU keep-alive only).

### Phase 4 — Scheduled weekly cycle fires (the BUG)

| Time UTC            | pwsState   | robotState   | rTOC    | cycleStartTimeUTC | cycleTime | mode | Note                                                                                                |
| ------------------- | ---------- | ------------ | ------- | ----------------- | --------- | ---- | --------------------------------------------------------------------------------------------------- |
| 09:00:03.611        | holdWeekly | notConnected | **255** | **1782464384**    | 120       | all  | **Scheduler stamps**. `rTOC: 67 → 255` in **one** shadow update. `cycleStartTimeUTC` newly stamped. |
| 09:00:04.375        | **on**     | **init**     | 255     | 1782464384        | 120       | all  | PWS turned on, robot reports init. `cycleStartTimeUTC` decodes to 10:59:44 CEST.                    |
| ↳ 09:00 → 13:00 UTC | —          | —            | —       | —                 | —         | —    | **No further transitions in any of these six fields for 3 h 59 min.**                               |

Note Phase 4 facts:

- **The scheduler ignored the operator-left `cycleInfo={short, 60}`** and
  fired its own `mode=all, cycleTime=120`. (Same shape as BUG-18 #88's
  conclusion: the weekly schedule has its own per-day mode+duration in
  `weeklySettings.<day>` and that wins over the live `cycleInfo` at fire
  time.) This is a separate concern from this bug, but worth flagging.
- **`rTurnOnCount 67 → 255` in one step is impossible by design.** One
  shadow update == at most one cycle worth of bump. The 188-step jump is
  the firmware writing a sentinel, not counting.
- **`cycleStartTimeUTC` is set, so the firmware did decide to start a
  cycle** — it is not a no-op. The scheduler successfully drove the
  cycle on the motor side.

### Phase 5 — Physical cycle (power trace, source of truth: smart-plug)

Power readings from upstream smart-plug (Shelly SP1, polled by HA every
~10 s, observable to within 1 W resolution). The robot is the only load
behind this plug.

| Time UTC        | Power                                | Interpretation                                                                                              |
| --------------- | ------------------------------------ | ----------------------------------------------------------------------------------------------------------- |
| 08:00 → 08:55   | ~3 W                                 | PSU standby. Robot truly docked, no motor activity.                                                         |
| 09:00:26        | 79.76 W                              | **Motor starts**. 23 s after the scheduler stamp. PWS now feeding the cleaner.                              |
| 09:00:32        | 109 W                                | Motor ramped.                                                                                               |
| 09:00 → 11:47   | 14–140 W oscillations, ~87 W average | Classic robot-pool pattern: brush motor + impeller pump cycling. **Robot is physically cleaning the pool.** |
| 11:47:41        | **5 W**                              | **Cycle ends**. Power drops to standby in one step.                                                         |
| 11:47 → present | ~5 W                                 | Robot back at dock. PSU standby.                                                                            |

Run length: **2 h 47 min** (09:00:26 → 11:47:41 UTC). That is **47 min
longer** than the firmware's own `cycleTime=120` setting — possibly the
firmware extending the run because its state machine stayed in `init`
the whole time and never declared `cleaning`, possibly something else.
Not investigated here.

### Phase 6 — Shadow remains stuck, operator notices

Operator at HA dashboard checks the robot at 12:50 CEST (10:50 UTC) and
again at 14:35 CEST (12:35 UTC). Sensor states (snapshot in
`sensors_pre_restart.txt`):

```
sensor.nono_2_etat_du_robot          = init               (last_changed 2026-06-26T09:00:07)
sensor.nono_2_nombre_de_cycles       = 255                (last_changed 2026-06-26T09:00:07)
sensor.nono_2_etat_de_l_alimentation = on                 (last_changed 2026-06-26T09:00:07)
sensor.nono_2_cycle_time             = 120                (last_changed 2026-06-26T09:00:07)
sensor.nono_2_temps_restant_du_cycle = 0                  (last_changed 2026-06-25T11:41:07)
sensor.nono_2_progression            = 0                  (last_changed 2026-06-25T11:40:57)
sensor.nono_2_etat_synthetique       = "Analyse"          (couleur=grey)
vacuum.nono_2                        = cleaning           (last_changed 2026-06-26T09:00:07)
binary_sensor.nono_2_broker_aws      = on (Connected)
```

The integration is doing the right thing given the shadow:

- `vacuum.nono_2 = cleaning` because the vacuum-state mapping sees
  `pwsState=on + cycleStartTime + cycleTime > 0`.
- `sensor.{robot}_statut = init` and the template
  `sensor.{robot}_progression = 0` because both gate on
  `calculated_state == CLEANING` (which requires `robotState ∈
{scanning, cleaning}`, see `system_details.py`) — and `robotState`
  reports `init` for 3 h 59 min straight.

So the bug surface is **firmware-side / shadow-side**, not
integration-side. The integration only inherits and propagates it.

## Findings

- **F1 — `rTurnOnCount=255` is a firmware sentinel, not a count.** A
  step of `67 → 255` in one shadow update is impossible under normal
  semantics (BUG-18 body documents `+1` per scheduled trigger). 255 =
  `0xFF` looks like the firmware writing an uninitialised-register or
  reset marker. Open question: under what conditions does the firmware
  emit this? Hypothesised triggers include (a) entering a "soft" cycle
  branch that bypasses the counter, (b) detecting an inconsistency from
  the BUG-19 sequence and entering a degraded-reporting mode.

- **F2 — The shadow is silent for the full cycle execution.** From
  09:00:04 UTC (entry into `init`) until at least 12:43 UTC, no
  `systemState` or `cycleInfo` field changes in any incoming shadow
  payload. The keep-alive shadow document containing the same values is
  republished periodically (sensor `last_updated` advances on
  `sensor.{robot}_cycle_time` at 09:00:07 UTC and never after — until
  the operator-triggered restart), but the values do not change. This
  contrasts with the BUG-19 #95 capture and the BUG-18 #90 sync table,
  both of which observed normal `init → scanning` and
  `scanning → notConnected` transitions during cycles on the same
  hardware.

- **F3 — The cycle physically completed.** Smart-plug power trace is
  unambiguous: 09:00:26 → 11:47:41 UTC with a brush-motor signature
  (oscillating 14–140 W). The robot motor / pump / cleaning logic runs
  on what is evidently a **different firmware code path** than the
  shadow-update path. The reporting code path got stuck; the physical
  cleaning code path did not.

- **F4 — `rTurnOnCount=255` silently masks errors in HA.** Per
  `custom_components/mydolphin_plus/managers/coordinator.py:890-903`,
  the integration only surfaces an error code if
  `error_section.turnOnCount == systemState.rTurnOnCount`. With
  `rTOC=255`, any real error stamped with a sane `turnOnCount` (≤67)
  cannot match. Both `sensor.nono_2_erreur_du_robot` and
  `sensor.nono_2_erreur_d_alimentation` therefore remain at `0`
  regardless of any error the firmware may emit. This is a
  user-impacting side effect of the firmware bug — not a separate
  integration bug, but worth a workaround in the integration.

- **F5 — The scheduler ignored the operator-left `cycleTime=60`.**
  Independent of the stuck-init symptom: the weekly schedule for Friday
  is configured `{time: 11:00, mode: all}`, and at fire time the
  firmware applied `mode=all, cycleTime=120`. The 60-min override the
  operator had left in `cleaningModes.short` and in
  `cycleInfo.cleaningMode={short, 60}` did **not** influence the
  scheduled cycle's duration. This is the persistence asymmetry already
  described in BUG-18 #88 — the scheduler reads `weeklySettings.<day>`
  and its own per-day duration, not the live `cycleInfo`. Mentioned for
  completeness; not the subject of this bug.

- **F6 — The morning BUG-19 sequence is the only abnormality before
  the trigger.** The robot's state at 09:00 UTC is the direct
  consequence of the BUG-19 manipulations: multiple short-lived cycle
  starts, one committed cycle (`rTOC 66 → 67` at 06:45:20), one
  un-committed but visible cycle (06:46:29), several rapid mode picks.
  No other operator or external action happened in the 2 h 12 min idle
  window before the scheduled trigger. The corruption almost certainly
  has a causal link to the BUG-19 sequence — see _Open questions_.

## Operator-facing impact

For the entire ~3 h while the robot was actually cleaning the pool, and
for the ~1 h after it returned to the dock, the operator had **no
correct read of the robot's state** in HA:

- The dashboard chip showed "Analyse" (grey) — implies "not running
  yet". Actual state: running.
- "Nombre de cycles = 255" — implies "robot has done 255 cycles" or "the
  counter is bogus". Either way, useless.
- "Progression = 0 %" for the whole 2 h 47 — implies "no progress".
  Actual: progressing normally on the motor side.
- "Temps restant = 0 s, expected end = 12:59 CEST" — the displayed
  expected-end time was wrong by 48 min (real end: 13:47 CEST), because
  the firmware extended the cycle by 47 min beyond `cycleTime=120` and
  never announced it.
- Errors silently masked by F4.

There is no way for the operator to know from HA alone that the cycle
ran to completion. The only ground truth is the upstream smart-plug
power trace.

## Open questions

- **Q1 — Why does `rTurnOnCount` become 255?** A clean PWS power-cycle
  (overnight reboot, `Plug Nono coupé la nuit`) should reset firmware
  state. If `rTOC` returns to a sane value after tonight's reboot, the
  255 was triggered by the BUG-19 → scheduled-cycle sequence and is
  recoverable. If it persists, the corruption is more deeply lodged
  (NVRAM, possibly).

- **Q2 — Is the stuck-init / silent-shadow a deterministic consequence
  of the BUG-19 sequence, or does it require additional conditions?**
  Reproduction strategy:

  1. Replay the BUG-19 mode-pick storm (3–4 picks with ≥1 commit) and
     then issue `vacuum.pause`.
  2. Leave the robot idle for at least 1 h to let the firmware quiesce.
  3. Trigger a fresh scheduled cycle (or simulate one via the weekly
     schedule).
  4. Observe whether the cycle runs with normal shadow updates
     (`init → scanning → notConnected`, `rTurnOnCount` bumps to 68) or
     repeats the stuck-init pattern.

  A control run without the BUG-19 sequence should produce a normal
  cycle (this is the everyday case on this robot per BUG-18 #90's
  table, where scheduled cycles show normal `init → scanning`
  transitions).

- **Q3 — Is there a code path in the firmware that handles "scheduled
  cycle after operator stops" differently from "scheduled cycle after
  clean idle"?** F3 implies that the cleaning execution path and the
  shadow-reporting path are independently triggered — one survives the
  BUG-19 fallout, the other does not. Understanding the split would
  identify the right firmware-level fix; it cannot be done from outside
  the firmware, so this is for documentation / Maytronics report only.

- **Q4 — Should the HA integration mask the `rTurnOnCount=255`
  sentinel?** Argument for: when `rTOC == 255`, the equality gate at
  `coordinator.py:902` silently drops legitimate errors stamped with
  any other `turnOnCount`. A small workaround would surface the most
  recent non-zero error code from any section when `rTOC == 255`, with
  a flag attribute indicating the gate was bypassed. Argument against:
  the integration's role is to mirror, not interpret; this is the
  firmware's bug to fix.

## Recovery action taken

After this diag was assembled, the operator power-cycled the upstream
smart-plug (`switch.sps_04_nono` off → on). Outcome of the recovery is
captured in the issue thread, not in this immutable diag.

## Relation to other issues

- **Direct precondition**: [BUG-19 #96](https://github.com/raouldekezel/dolphin-robot/issues/96)
  — the two silent-E-B failures of the morning are what left the
  firmware in the state that this cycle triggered. BUG-19 covers the
  operator-visible-at-the-time symptom (unwanted cycle starts);
  **BUG-20 covers the delayed firmware-state corruption surfaced by
  the next scheduled cycle**.
- **Persistence model context**: [BUG-18 #88](https://github.com/raouldekezel/dolphin-robot/issues/88)
  — F5's observation that the scheduler ignored the operator's
  `cycleTime=60` is the expected behaviour per BUG-18's analysis of
  `weeklySettings.<day>` precedence. Not a regression.
- **Catalog reset diagnosis context**: [docs/diag/2026-06-22_bug-18_catalog-reset-across-reboot/findings.md](../2026-06-22_bug-18_catalog-reset-across-reboot/findings.md)
  — establishes the overnight PWS reboot model that produced the sane
  baseline at 04:45:50 UTC.
- **Schedule observation context**: [docs/diag/2026-06-23_bug-18_cycletime-vs-nextcycleduration-sync/findings.md](../2026-06-23_bug-18_cycletime-vs-nextcycleduration-sync/findings.md)
  — anchor for the "normal" scheduled-cycle shadow pattern (`init →
scanning → notConnected`, `rTOC` bumps by 1). Today's stuck-init
  pattern is a **diverging trace** from that baseline.

## See also

- BUG-19 issue: #96
- BUG-19 diag PR: #95
- BUG-18 issue: #88
- BUG-18 sync diag: PR #90
- This diag is the source of evidence for BUG-20: see the corresponding
  issue for the user-facing summary and the recovery action.
