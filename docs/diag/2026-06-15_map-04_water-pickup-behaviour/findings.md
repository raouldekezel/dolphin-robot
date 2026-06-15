# MAP-04 — `water` and `pickup` firmware behaviour on S2000

## TL;DR

Both `water` and `pickup` are real firmware-pilotable modes on the operator's
Dolphin S2000, but with very different semantics: `water` is a fully-fledged
cleaning cycle with a non-wall-follow "spot-waterline" pattern (intentionally
**not** exposed in the Maytronics S2000 picker, which lists only `all`, `stairs`,
`short`, `floor`), while `pickup` is a short retrieval cycle that the app surfaces
as a first-class "Récupérez-moi" screen and whose duration is firmware-fixed at
12 min regardless of the HA-side `number.<robot>_duree_du_cycle_ramassage`.

## Context

- **Date:** 2026-06-15 (CEST, UTC+02:00)
- **Robot:** Maytronics Dolphin S2000, motor unit `REDACTED-MUSN`, family `S4`,
  firmware `pwsSwVersion=11.0004` / `muSwVersion=9F88`.
- **Fork tag during experiment:** `v1.0.26b3-raoul.2` (commit `be780e0`).
- **HA Core:** `2026.1.3` (`hass` container on `intel-nuc`, `192.168.0.27`).
- **Pre-experiment state:** Direct continuation of
  [session 2026-06-15 FEAT-01 stairs validation](../2026-06-15_feat-01_stairs-validation/findings.md)
  which ended at 15:43:09 CEST with the robot back in `docked + holdWeekly`,
  `cleaningMode.mode = all`, `cycleTime = 60`, turn-on counter 27.
- **HA-side configured cycle times** (sources for the BUG-08 chain payloads):
  `number.<robot>_duree_du_cycle_ligne_d_eau = 120`,
  `number.<robot>_duree_du_cycle_ramassage = 5`.
- **Maytronics app S2000 picker observed:** 4 cards only — Complet (`all`,
  presets 2 h / 2 h 30 / 3 h), Couverture complète (`stairs`, presets
  2 h / 2 h 30 / 3 h), Rapide (`short`, 1 h), Fond (`floor`,
  presets 2 h / 2 h 30 / 3 h). No card for `water`, `pickup`, `ultra`, etc.

## Actions taken

1. **`01_ha-fan-speed-water`** — operator opens HA `vacuum.<robot>` card and
   selects `water` ("Ligne d'eau") from the `fan_speed` picker. The cycle
   auto-starts (BUG-13 behaviour). Operator observes physical behaviour for
   ~13 min.
2. **`02_water-stop-transition`** — operator stops the cycle. No corresponding
   `Set power state` write is captured on the HA side, suggesting the stop
   was issued from the Maytronics app rather than from HA (see Findings #3).
3. **`03_ha-fan-speed-pickup`** — operator selects `pickup` ("Ramassage")
   from the same picker. The cycle auto-starts, runs for ~72 s, and the
   firmware itself transitions back to `holdWeekly` without external input.

## Timeline

| Local time   | Event                                                                                                                                                                                                                                                                       | Evidence                                 |
| ------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------- |
| 15:45:30     | Capture window opens; robot in `docked + holdWeekly`, `cleaningMode.mode = all`, `cycleTime = 60` (residue from prior session)                                                                                                                                              | `01_ha-fan-speed-water.mqtt.log` (head)  |
| 15:45:49.253 | **`Set cleaning mode {mode: water}` from HA picker (BUG-13)** — operator's selection auto-starts the cycle                                                                                                                                                                  | `01_ha-fan-speed-water.mqtt.log`         |
| 15:45:50.374 | **`Set cycle time {cycleTime: 120}`** chained ~1.12 s later (BUG-08); 120 = `number.<robot>_duree_du_cycle_ligne_d_eau`                                                                                                                                                     | `01_ha-fan-speed-water.mqtt.log`         |
| 15:45:51.448 | Firmware reports `pwsState = on`, `cleaningMode.mode = water`, **`cycleTime = 120`** — the firmware accepted both writes (real mode delta `all → water`, unlike the same-mode case examined in the FEAT-01 session)                                                         | `01_ha-fan-speed-water.mqtt.log`         |
| 15:46:47.638 | `nombre_de_cycles` → 28 (water cycle counted on start, with the firmware's usual delay)                                                                                                                                                                                     | `01_ha-fan-speed-water.mqtt.log`         |
| 15:47:16.157 | Firmware reports `robotState = scanning` (delay ~87 s after HA emit, longer than the 61 s seen for the same-mode stairs case — firmware nav warmup)                                                                                                                         | `01_ha-fan-speed-water.mqtt.log` (tail)  |
| ~15:47–15:58 | Robot operates: heads to a pool wall, surfaces briefly at the waterline, dives back, traverses underwater, surfaces again elsewhere. **Not** a continuous wall-follow. Operator confirms the pattern in real time.                                                          | physical observation                     |
| 15:58:55.025 | Firmware reports `pwsState = off`. **No corresponding HA-side `Set power state` write in the capture** — the stop came from elsewhere (Findings #3).                                                                                                                        | `02_water-stop-transition.mqtt.log`      |
| 15:58:57.467 | Firmware → `pwsState = holdWeekly`, `vacuum.<robot> = docked`                                                                                                                                                                                                               | `02_water-stop-transition.mqtt.log`      |
| 15:59:31.589 | **`Set cleaning mode {mode: pickup}` from HA picker (BUG-13)**                                                                                                                                                                                                              | `03_ha-fan-speed-pickup.mqtt.log`        |
| 15:59:32.659 | **`Set cycle time {cycleTime: 5}`** chained ~1.07 s later (BUG-08); 5 = `number.<robot>_duree_du_cycle_ramassage`                                                                                                                                                           | `03_ha-fan-speed-pickup.mqtt.log`        |
| 15:59:33.209 | Firmware reports `pwsState = on`, `cleaningMode.mode = pickup`. **Reported `cycleTime = 12`, not 5** — the firmware overrides the HA-supplied value with its own fixed pickup duration of 12 min. The fast propagation (~2 s vs. ~6 s for water) is also notable.           | `03_ha-fan-speed-pickup.mqtt.log`        |
| 15:59:33.986 | `cleaningModes.pickup` catalog stays at 12 — the firmware never let HA's 5 enter the catalog either                                                                                                                                                                         | `03_ha-fan-speed-pickup.mqtt.log`        |
| 16:00:13.761 | `nombre_de_cycles` → 29 (pickup counted on start)                                                                                                                                                                                                                           | `03_ha-fan-speed-pickup.mqtt.log`        |
| ~16:00       | Robot heads directly to a pool edge, surfaces, becomes motionless on the waterline. App switches to a dedicated "Récupérez-moi" screen (operator confirms).                                                                                                                 | physical observation                     |
| 16:00:45.168 | Firmware **auto-ends** the cycle: reports `pwsState = off` ~72 s after the start, _much_ shorter than the catalog 12 min cycleTime. **`cycleTime = 12` is therefore a maximum / wait-time, not an active-duration** for pickup. No HA-side stop was emitted in this window. | `03_ha-fan-speed-pickup.mqtt.log`        |
| 16:00:47.783 | Firmware → `pwsState = holdWeekly`, `vacuum.<robot> = docked`                                                                                                                                                                                                               | `03_ha-fan-speed-pickup.mqtt.log` (tail) |

## Findings

1. **`water` is a real firmware-pilotable cleaning mode on S2000**, distinct
   from `all`. HA-initiated `set_fan_speed = water` is honored end-to-end
   (mode delta accepted, cycleTime delta accepted, robot performs a
   self-contained cycle). The Maytronics S2000 app **does not** expose
   `water` in its picker (4 cards only: `all`, `stairs`, `short`, `floor`),
   but the app **does** correctly localize the running-mode label as "Ligne
   d'eau" when the firmware reports `water`. This matches the
   "intentionally not surfaced, but firmware-known" pattern previously
   characterised in [MAP-03 #45](../../../issues/45) / PR #46 for `cove` /
   `spot` / `wall`, but with a critical difference: `water`'s app-side i18n
   key resolves correctly (vs. the raw `cleaning_mode_<x>_title` placeholders
   shown for `cove`/`spot`/`wall`), suggesting `water` had a real product
   role on a previous S-series generation. Decision: keep `water` in the
   `CleanModes` enum as it currently is.

2. **`water` cleaning pattern observed = "spot-waterline + dives + traverse".**
   Operator reported: the robot heads to a wall, surfaces briefly at the
   waterline (does not follow the wall along the surface), dives back to the
   floor, travels underwater away from the edge, then surfaces again at a
   different point on the perimeter. Repeats. This is qualitatively different
   from the wall-following waterline cycle marketed by Maytronics on the
   T- and M-series. The Maytronics marketing for the S2000 confirms the
   absence: their commercial documentation lists for S2000 "nettoyage
   complet / fond uniquement / **fond + parois + ligne d'eau** / cycle rapide",
   where the third item is the combined `all` mode, not a pure-waterline
   mode. The HA label "Ligne d'eau" / "Water line" is therefore misleading
   on S-series; not actionable in the integration (it would break T/M users),
   just worth documenting in user-facing docs.

3. **water stop was not HA-initiated.** Between the operator's
   `set_fan_speed = water` at 15:45:49 and the firmware's `pwsState = off`
   report at 15:58:55, **no `Set power state` write appears in the capture**
   (full log inspected, not just the slice). The most likely explanation
   is that the operator pressed Stop in the Maytronics app rather than the
   HA card. This is consistent with the operator's narration. The session
   is not invalidated — it just means the stop trajectory is not
   characterised here. Worth noting as an oversight in the protocol of
   the next session if we want to capture HA-driven stops too.

4. **`pickup` is a first-class firmware mode with hard-coded duration.**
   Three behavioural signatures distinguish `pickup` from every other mode
   tested in this corpus to date:

   - The Maytronics S2000 app switches to a **dedicated "Récupérez-moi"
     screen** when the firmware reports `cleaningMode.mode = pickup`
     (vs. just updating a label, as `water` does).
   - The firmware **silently overrides** the integration's `Set cycle
time {cycleTime: 5}` write and applies `cycleTime = 12` instead.
     This is firmware-side filtering (the catalog entry `cleaningModes.pickup`
     stayed at 12 as well), not an integration bug.
   - The firmware **auto-ends** the cycle ~72 s into it, without any
     HA-side stop being emitted. Combined with the previous point, this
     means `cycleTime = 12` for pickup is a **timeout / wait-bound, not an
     active duration** — the firmware ends the cycle as soon as it has
     reached its target position (edge of pool, surfaced, motionless).
     This is qualitatively different from every other cleaning mode where
     `cycleTime` controls active runtime.

5. **Operational implication for the integration UX.** The HA-side entity
   `number.<robot>_duree_du_cycle_ramassage` is misleading on S2000: any
   value the operator sets will be silently overridden by the firmware.
   Candidate follow-up issue: hide this number entity, or mark it
   read-only / advisory, for robot families where the firmware
   pre-empts the value. Not done here — needs cross-model verification
   first (it might be respected on M700 or other families).

6. **Mode-change propagation latency varies a lot by mode.** From HA emit
   to firmware reported `pwsState = on`:

   - water (mode delta `all → water`): ~2 s reported (`on`), ~87 s to `scanning`
   - pickup (mode delta `water → pickup`): ~2 s reported (`on`), no
     `scanning` substate (pickup skips scanning, goes straight to the
     directed travel-to-edge subroutine — observable as `robotState`
     remaining at its previous value during the 72 s of activity).

7. **`nombre_de_cycles` increments on start, for every accepted mode write,
   regardless of how short the actual physical work is.** Both `water` (full
   cycle interrupted at 13 min) and `pickup` (auto-ended at 72 s) bumped the
   counter by exactly one. Useful for operator-facing dashboards: do not
   interpret the counter as "completed cleans" — it counts starts.

## Open questions

- **Is `ultra` (the 6th historical mode in the enum) also pilotable on S2000?**
  Not tested in this session. Worth a short follow-up — should the integration
  surface it? Closes-or-confirms whether to drop it from the S-series picker.
- **On a T- or M-series Maytronics, does `water` produce the documented
  wall-following waterline behaviour?** If yes, the conclusion that the HA
  label is "misleading on S-series" stands. If no, the integration is
  carrying a deprecated mode that should be removed for all families.
- **Should `number.<robot>_duree_du_cycle_ramassage` be hidden on
  firmware versions where the value is silently overridden?** Open
  product question, low-priority.
- **What is the actual firmware-side filter for same-mode `Set cycle time`
  writes?** This session does not re-explore the BUG-14 path (covered in
  the FEAT-01 session), but the pickup override is a related but distinct
  filter — worth a focused session on the firmware's `cycleInfo` write
  acceptance rules.

## Refs

- Closes [MAP-04 #52](../../../issues/52).
- Builds on [MAP-03 PR #46](../../../pull/46) — `water` is in the same
  "firmware-known, app-hidden" category as `cove`/`spot`/`wall`, but with
  a localized app label (interesting precedent for the catalog discussion).
- Cross-cuts [FEAT-01 session 2026-06-15](../2026-06-15_feat-01_stairs-validation/findings.md) — BUG-13 reconfirmed at every picker selection.
- The Maytronics S2000 commercial documentation referenced for the
  4-card picker analysis is [the official product page](https://www.maytronics.com/fr-fr/store/residential-pools/best-seller-cleaners/dolphin-s2000/99996291-EU.html).
