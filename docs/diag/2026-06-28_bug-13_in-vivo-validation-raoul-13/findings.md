# BUG-13 — in-vivo validation of the write-on-commit pivot on raoul.13

## TL;DR

End-to-end validation of [PR #100](https://github.com/raouldekezel/dolphin-robot/pull/100) (write-on-commit cleaning-mode pick) on the live robot, exercising the four code paths the pivot reshapes:

- **T-a — N rapid picks while docked → PASS.** Multiple consecutive mode picks issued from HA while docked. `rTurnOnCount` stayed at `75` throughout the picking window, `vacuum.nono_2` stayed `docked`, no `pwsState=on` reached HA. The pivot's docked-write-nothing branch holds, including across the BUG-19 / BUG-20 trigger pattern (rapid picks after a scheduled morning cycle).
- **T-b — Run after staging a mode → PASS.** Pick `floor` while docked stages without start; subsequent `vacuum.start` commits the staged mode through `set_cleaning_mode`, the BUG-08 chain fires, `cycle_time` lands at the per-mode HA `number` value, `rTurnOnCount` ticks exactly once.
- **T-c — App override of a staged HA pick → PASS.** Stage `all` from HA while docked, then start `short` from the Maytronics app. `_reconcile_desired_clean_mode` detects the foreign initiator (mode-echo from the app differs from `_last_seen_reported_clean_mode`), reconciles `_desired := short`, picker reflects `short` immediately. The staged `all` is overwritten without being pushed to the firmware.
- **T-d — Live-swap mode mid-cycle → PASS.** Running in `short` (Rapide), pick `all` (Complet) from the HA picker. The pivot's running-path live-write reaches the firmware, the BUG-08 chain fires, `cycle_time` updates from `60` to `120`, **`rTurnOnCount` stays unchanged**, `vacuum.state` stays `cleaning` (no docked transition).

The BUG-19 #96 second-pick race and BUG-20 #98 stuck-init are removed by construction (no implicit `pwsState=on` ever triggered on the docked path) — repro pattern executed in T-a, neither reproduced.

One UX gap surfaced during T-b and T-d and is filed separately as HARD-11 (see § Follow-ups).

## Context

- **Date:** 2026-06-28
- **Tag installed via HACS:** `v1.0.26b3-raoul.13` (BUG-13 pivot from PR #100, no E5a hot-patch residue on host)
- **Robot:** Maytronics Dolphin S2000 ("Nono 2"). Firmware reports `robotType="S4"`.
- **HA:** 2026.1.3 (container `hass` on intel-nuc, `network_mode: host`)
- **Capture:** HA state reads via REST API (`/api/states`) at observation points. No MQTT trace this session — pivot effects observed at the HA state layer, which is the user-facing contract being validated.

The morning of the test had a scheduled cycle (09:00 UTC, mode `all`, 2h30) that completed normally at ~11:30 UTC. The test sequence below ran on the same `vacuum.nono_2` instance with no integration reload between the scheduled cycle and the picking session — exactly the trigger pattern documented in [BUG-19 #96](https://github.com/raouldekezel/dolphin-robot/issues/96) and [BUG-20 #98](https://github.com/raouldekezel/dolphin-robot/issues/98). Both bugs were observed as removed-by-construction on the pivot.

## Branch coverage

The pivot reshapes four call sites of `_set_cleaning_mode` + `_vacuum_start` + `_reconcile_desired_clean_mode`. The four T-tests exercise them as follows:

| #   | Action                       | Pre-state                       | Pick from | Code path exercised                                                       | Expected effect                                                                    | Result |
| --- | ---------------------------- | ------------------------------- | --------- | ------------------------------------------------------------------------- | ---------------------------------------------------------------------------------- | ------ |
| T-a | N rapid picks while docked   | `docked`, `mode=all` (post-AM)  | HA picker | `_set_cleaning_mode` docked → stage only, **no AWS write**                 | No firmware start. `rTurnOnCount` unchanged. UI reflects staged mode immediately.  | PASS   |
| T-b | Run after staging `floor`    | `docked`, staged `_desired=floor` | HA `vacuum.start` | `_vacuum_start` reads `_desired_clean_mode`, writes via `set_cleaning_mode` → BUG-08 chain fires | Firmware adopts `floor` + per-mode cycleTime. `rTurnOnCount` +1.            | PASS   |
| T-c | Stage `all` then start app `short` | `docked`, staged `_desired=all` | App | App writes directly. `_reconcile_desired_clean_mode` detects foreign initiator (mode-echo differs from `_last_seen_reported_clean_mode`) | `_desired := short`. Staged `all` overwritten. No re-write from integration. | PASS   |
| T-d | Live-swap `short → all` mid-cycle | `cleaning`, `mode=short`        | HA picker | `_set_cleaning_mode` running → stage `_desired` AND live-write to firmware | Mode swap accepted, no restart. BUG-08 chain fires (mode-delta detected). `rTurnOnCount` unchanged. | PASS   |

## Timeline (UTC)

| Time (UTC)   | Event                                                              | Observable                                                                                                                                                                                |
| ------------ | ------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 09:00:54     | Scheduler started morning cycle, mode `all`                        | `nombre_de_cycles 74 → 75`                                                                                                                                                                |
| ~11:30       | Morning cycle ended                                                | `vacuum.state = docked` last_changed `11:32:42`; `statut = holdweekly`                                                                                                                    |
| 11:32 → 12:58 | **T-a — operator-initiated rapid picks while docked**             | `nombre_de_cycles` stayed at `75` throughout (no tick). `vacuum.state` stayed `docked` (no `cleaning` transition). Final pick `floor` at `12:58:59` left `select.mode_de_nettoyage=floor` and vacuum still `docked`. |
| 13:01:28     | T-b — `vacuum.start` commits the staged `floor`                    | `vacuum.state = cleaning`, `sensor.statut = init`, `sensor.cycle_time = 120` (= `number.duree_du_cycle_sol_uniquement`)                                                                   |
| 13:02:08     | Firmware enters `scanning` phase                                   | `nombre_de_cycles 75 → 76` (tick exactly once for the Run)                                                                                                                                |
| ~13:05:00    | T-c step 1 — operator stopped cycle via `vacuum.pause`             | `vacuum.state = docked`, `statut = holdweekly`, `etat_du_robot = notconnected` @ `13:05:30`                                                                                                |
| 13:05:39     | T-c step 2 — operator picked `all` (Complet) in HA                 | `select.mode_de_nettoyage = all`, `vacuum.state` still `docked` (staged, no firmware write)                                                                                                |
| ~13:06:00    | T-c step 3 — operator started `short` (Rapide) from Maytronics app | _no HA write_                                                                                                                                                                             |
| 13:06:20     | Reconcile: foreign initiator detected                              | `select.mode_de_nettoyage = short` (reconciled from app), `cycle_time = 60`, `vacuum.state = cleaning`                                                                                     |
| 13:07:11     | Firmware tick                                                      | `nombre_de_cycles 76 → 77`                                                                                                                                                                |
| 13:08:29     | T-d — operator picked `all` in HA while cycle running              | `select.mode_de_nettoyage = all`. Running path: stage + live-write                                                                                                                        |
| 13:08:39     | BUG-08 chain delivers cycleTime                                    | `cycle_time = 60 → 120`. `vacuum.state` stays `cleaning`. `nombre_de_cycles` stays at `77`. `etat_du_robot` stays `scanning`.                                                              |

## Per-test detail

### T-a — N rapid picks while docked

After the morning scheduled cycle docked the robot at `~11:32 UTC`, the operator issued a series of rapid mode picks from the HA `select.nono_2_mode_de_nettoyage` dropdown (cycling through `all`, `floor`, `short`, etc., over ~90 minutes). Observation point at `13:01:25 UTC` showed `vacuum.nono_2.state = docked`, `vacuum.nono_2.fan_speed = floor` with `last_changed = 11:32:42` (= the post-cycle docked timestamp, not refreshed by intervening picks).

**Critical metric:** `sensor.nono_2_nombre_de_cycles = 75` with `last_changed = 09:00:54` — meaning the cycle counter has not been touched since the morning cycle started. Zero implicit starts during the entire picking window.

This is the exact pattern that triggered BUG-19 #96 on the silent E-B fix (#86), where a second consecutive docked pick ~17 s later started a cycle. On the write-on-commit pivot, no `desired.cleaningMode.mode` is ever written while docked — the firmware has no opportunity to interpret the write as an implicit start, regardless of timing between picks.

The pivot's `async_update_listeners()` call after each stage propagated the staged value to entities immediately — `vacuum.fan_speed` and `select.mode_de_nettoyage` followed each pick without waiting for a firmware echo. The operator confirmed the picker UI updates were visible.

### T-b — Run after staging a mode

| Time (UTC)   | Event                                                          |
| ------------ | -------------------------------------------------------------- |
| 12:58:59     | Pick `floor` (Sol uniquement) — staged                         |
| 13:01:28     | `vacuum.start` → `vacuum.state = cleaning`, `cycle_time = 120` |
| 13:02:08     | `nombre_de_cycles 75 → 76`                                     |

`_vacuum_start` read the staged `_desired_clean_mode = floor`, wrote it via `set_cleaning_mode`, the firmware took it as the start command. The BUG-08 chain emitted `cycle_time = 120` (`number.nono_2_duree_du_cycle_sol_uniquement = 120`) ~1 s after the mode write (the chain delay is preserved on purpose, per SPIKE-02 E7).

The interval mode-write → first-`scanning`-tick was ~40 s. This is consistent with the firmware echo latency band documented across prior sessions (`60-77 s` for HA-initiated starts in the FEAT-01 session 2026-06-15). The `init → scanning` transition triggers the `rTurnOnCount` increment.

### T-c — App override stage HA

Robot back to docked at `13:05:30` after the operator paused the floor cycle. Picker stage at `13:05:39` → `select.mode_de_nettoyage = all`, vacuum still `docked`. No firmware write at this step (write-nothing branch of the pivot).

The operator then started `short` (Rapide) from the Maytronics app. The app push reached the firmware directly (not via HA), and the firmware echoed `reported.cycleInfo.cleaningMode.mode = short` plus `pwsState = on` in a single shadow update. Observation at `13:06:20`:

- `select.mode_de_nettoyage` flipped from `all` to `short`
- `vacuum.state` flipped from `docked` to `cleaning`
- `cycle_time = 60`

This is `_reconcile_desired_clean_mode` doing the "foreign change overwrites" branch: `_last_seen_reported_clean_mode` was `floor` (from T-b), the new echo is `short`, so the gate triggers and `_desired := short`. The staged `all` is dropped without ever being pushed.

`cycle_time = 60` here matches `number.nono_2_duree_du_cycle_rapide = 60` by coincidence — the firmware value comes from the app's own catalog push, not from the HA `number`. This test does not distinguish BUG-14 (HA `number` ignored on app-initiated start) from the cycleTime happening to land on the same value; a dedicated BUG-14 follow-up would need to detune the HA `number` before the app start to isolate.

`nombre_de_cycles 76 → 77` at `13:07:11` confirms a single tick for the app-initiated start.

### T-d — Live-swap mid-cycle

Robot running in `short` since `13:06:20`. Operator picked `all` (Complet) in the HA `select` at `13:08:29`. Two updates landed:

| Time (UTC)   | Field                                  | Before | After |
| ------------ | -------------------------------------- | ------ | ----- |
| 13:08:29     | `select.mode_de_nettoyage`             | short  | all   |
| 13:08:39     | `sensor.nono_2_cycle_time`             | 60     | 120   |

Critically:

- `nombre_de_cycles` stays at `77` — **no `rTurnOnCount` tick**, confirming no restart
- `vacuum.state` stays `cleaning` — no docked transition
- `etat_du_robot` stays `scanning` — no `init` re-entry
- `statut` stays `cleaning`

This is the running-path branch of `_set_cleaning_mode`: stage `_desired := all`, AND live-write to AWS. The firmware accepts the mode swap on a running robot without restarting the cycle (Maytronics-app parity, validated in [PR #87](https://github.com/raouldekezel/dolphin-robot/pull/87) — same primitive, same effect). The BUG-08 chain fires (mode-delta detected), delivering the per-mode `cycle_time = 120` from `number.nono_2_duree_du_cycle_complet`.

The ~10 s gap between the mode write and the visible `cycle_time` update reflects coordinator refresh cadence, not the firmware-side `sleep(1)` of the BUG-08 chain. The chain itself emitted at `~13:08:30 UTC`; HA observation lagged.

#### Operator-flagged behaviour to track separately

`cycleStartTime` is **not restamped** on live-swap (confirmed by PR #87). The new `cycleTime = 120` is interpreted by the firmware as the new total duration starting from the original `cycleStartTime` at `13:06:20`. Time-left at swap moment: `(13:06:20 + 120 min) - 13:08:29 ≈ 117 min ≈ 2h`. The Maytronics app reflected this as "2 hours of work remaining".

The operator flagged this as surprising — picking a longer mode mid-cycle extends total cycle duration to that mode's catalog value. The alternative — not writing `cycleTime` on live-swap — would leave the robot running the new mode for whatever time remained (~58 min in this case), too short for the new mode to be effective. SPIKE-02 E7 also documented that a combined `{mode, cycleTime}` atomic write is lossy at firmware level, so the chain itself cannot be removed.

This is current-behaviour-by-design (no firmware path exists for "change mode, keep planned end-time without restart") but the user-visible effect is non-obvious. To be tracked in a separate behaviour-change request from the operator (out of scope for this PR).

## BUG-19 / BUG-20 status check

Both bugs are removed by construction on the pivot because no `desired.cleaningMode.mode` is written while docked, ever. T-a executed the exact trigger pattern (scheduled morning cycle, then operator rapid picks within hours), with multiple closely-spaced picks. Neither bug reproduced:

- **BUG-19 #96** (second consecutive docked pick ~17 s apart triggers cycle): T-a did multiple consecutive picks; `vacuum.state` stayed `docked` and `nombre_de_cycles` stayed at `75`.
- **BUG-20 #98** (scheduled cycle after BUG-19 sequence leaves firmware stuck in `init`): the morning scheduled cycle preceded the picking session and completed normally; subsequent T-b Run reached `scanning` (`nombre_de_cycles 75 → 76`) on schedule with no stuck-init.

The E5a hot-patch on the previous `raoul.12` host was not ported into `raoul.13` and was not re-applied as a workaround for this session. Host inspection (`managers/aws_client.py`) at session start showed 0 references to the silent-stop apparatus identifiers (`set_cleaning_mode_silent`, `_silent_stop_due`, `_silent_stop_deadline`, `_SILENT_STOP_TTL`), confirming the pivot code is actually running.

## Follow-ups

- **HARD-11 (filed, separate PR)** — no visual feedback during the firmware echo window. On T-b, the operator observed ~60-90 s between `vacuum.start` and `vacuum.state = cleaning`, during which the picker UI showed no transient state. On T-d, the live-swap was applied at the integration level but the operator initially did not perceive the picker update (resolved a few seconds later when `cycle_time` and the picker UI caught up). Proposed approach: optimistic `vacuum.state` transition on `vacuum.start` with TTL fallback, and a transient indicator on writes (mode pick, cycle time pick, pause). The pivot already sets the right precedent for staged values reaching entities via `async_update_listeners()`; extending the same pattern to `vacuum.state` and other write paths is the natural next step.
- **Behaviour change request (operator-flagged, separate ticket)** — live-swap cycleTime extension semantics (cf. T-d note above). Operator to file a dedicated request.

## Verdict

PR #100 passes all four planned validation tests in vivo on the live S2000 against tag `v1.0.26b3-raoul.13`. Closes the validation phase of [#47](https://github.com/raouldekezel/dolphin-robot/issues/47) as far as the pivot's four code paths are concerned. The two follow-ups above are scoped out and tracked separately.
