# BUG-13 — in-vivo validation of the decoupling fix on raoul.10

## TL;DR

End-to-end validation of [PR #86](https://github.com/raouldekezel/dolphin-robot/pull/86) on the live robot, exercising the three code branches the fix introduces and the residual question left open by the code review.

- **Pick mode while docked (silent E-B) → PASS.** Mode + cycleTime adopted by the firmware, robot stayed `holdWeekly`, `rTurnOnCount` unchanged. The integration emits the exact `set + cycleTime + pause` triple #85 predicted, with the pause landing ~84 ms after the cycleTime echo and well before the firmware would flip `pwsState=on`.
- **Start (`vacuum.start` on docked robot) → PASS.** Normal `Set cleaning mode → Set cycle time` chain, **no** trailing `Set power state` (the new BUG-13 stop branch correctly does not fire on the start path). Robot transitioned `holdWeekly → on / init / scanning`, `rTurnOnCount` incremented by 1.
- **Pick mode while running → PASS, and the review's open question is settled.** Two consecutive live mode-swaps (`stairs → floor`, then `floor → all`) ran the today-preserved live-write path. The firmware adopted each new mode without an `off` interlude. **`rTurnOnCount` did not bump on either swap** (59 → 59 → 59), and `cycleStartTime` was not restamped — the firmware treats a mode-write on a running robot as a continuation, not a restart.

## Context

- **Date:** 2026-06-21
- **Tag installed via HACS:** `v1.0.26b3-raoul.10` (BUG-13 fix + BUG-13 decoupling diag from #85).
- **Robot:** Maytronics Dolphin S2000 ("Nono 2"). Firmware reports `robotType="S4"`, `pwsSwVersion="11.0004"`, `muSwVersion="9F88"`.
- **HA:** 2026.1.3 (container `hass` on intel-nuc, `network_mode: host`).
- **Debug logging:** persistent `custom_components.mydolphin_plus.managers.aws_client: debug` from `configuration.yaml`.
- **Capture:** `docker logs -f hass --since 0s --timestamps` filtered on mydolphin_plus + shadow markers, two contiguous windows: 12:24:15 → ~12:30 UTC (silent-pick + Run), 12:32:46 → ~12:42 UTC (running-branch swaps).

## Branch coverage

| #   | Action         | Pre-state                   | Pick     | Code path                                     | Cycles  | Pause emitted?                            | Outcome                                       |
| --- | -------------- | --------------------------- | -------- | --------------------------------------------- | ------- | ----------------------------------------- | --------------------------------------------- |
| 1   | Pick docked    | `holdWeekly`, mode=`all`    | `stairs` | `set_cleaning_mode_silent` (E-B)              | 58 → 58 | ✅ yes (~84 ms post-cycleTime echo)       | mode + cycleTime adopted, robot stayed docked |
| 2   | `vacuum.start` | `holdWeekly`, mode=`stairs` | —        | `_vacuum_start` → bare `set_cleaning_mode`    | 58 → 59 | ❌ no (`_silent_stop_deadline` not armed) | robot transitioned to `on / init / scanning`  |
| 3   | Pick running   | `cleaning`, mode=`stairs`   | `floor`  | bare `set_cleaning_mode` (today's live write) | 59 → 59 | ❌ no (`is_active` branch skips silent)   | mode swapped mid-cycle, no off interlude      |
| 4   | Pick running   | `cleaning`, mode=`floor`    | `all`    | bare `set_cleaning_mode` (today's live write) | 59 → 59 | ❌ no                                     | mode swapped mid-cycle, no off interlude      |

The four scenarios exercise every branch of the BUG-13 split: `is_active=False` → silent E-B; `is_active=True` → live; `_silent_stop_deadline` armed vs. not armed; mode-echo vs. cycleTime-echo discriminator in the observer.

## Action 1 — pick docked (silent E-B)

`01_silent_pick_all_to_stairs.mqtt.log`. Operator picked `Couverture complète` (= `stairs`) in the HA vacuum combo while the robot was `holdWeekly` with `reported.cycleInfo.cleaningMode.mode = "all"`.

| Δ T₀                             | Source | Event                                                                                                                                                                             |
| -------------------------------- | ------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **+0.000 s** (14:24:30.761 CEST) | HA     | `Set cleaning mode, Desired: {cleaningMode: {mode: stairs}}` — the silent helper publishes via the standard `set_cleaning_mode`, having armed `_silent_stop_deadline = now + 5 s` |
| +0.038 s                         | AWS    | `/update/accepted` v1 echoes `reported.cycleInfo.cleaningMode = {mode:"all", cycleTime:150}` (pre-write state)                                                                    |
| +0.091 s                         | AWS    | `/update/accepted` v2 echoes our `desired.cleaningMode.mode = "stairs"` with our token — BUG-08 chain branch fires (`sleep(1)`)                                                   |
| **+1.093 s** (14:24:31.854)      | HA     | `Set cycle time, Desired: {cycleInfo: {cycleTime: 150}}` — BUG-08 chain emits the configured cycle time                                                                           |
| +0.011 s after                   | AWS    | `/update/accepted` echoes our cycleTime write with our token — **BUG-13 observer fires the new branch**, clears `_silent_stop_deadline`, calls `pause()`                          |
| **+1.177 s** (14:24:31.938)      | HA     | `Set power state, Desired: {systemState: {pwsState: off}}` — E-B stop write                                                                                                       |

Post-conditions:

- `reported.cycleInfo.cleaningMode = {mode:"stairs", cycleTime:150}` — mode + cycleTime adopted.
- `reported.systemState.pwsState` stayed `holdWeekly` (no `on` flip).
- `rTurnOnCount` remained 58.
- HA `vacuum.nono_2` stayed `docked`.
- Maytronics app reported the robot as stopped, with the default mode now `Couverture complète`.

Matches #85 E-B PASS exactly. The integration's timing landed the pause comfortably inside the firmware's pre-`pwsState=on` window (~2.5 s post mode write per #85), and the `_silent_stop_deadline` self-cleared on the cycleTime echo as designed.

## Action 2 — `vacuum.start` on docked robot

`02_run_start_holdweekly_to_cleaning.mqtt.log`. Operator clicked Start in the vacuum more-info dialog. Robot was `holdWeekly`, mode `stairs` (post-Action 1).

| Δ T₀                             | Source   | Event                                                                                                                                                                                     |
| -------------------------------- | -------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **+0.000 s** (14:30:00.323 CEST) | HA       | `Set cleaning mode, Desired: {cleaningMode: {mode: stairs}}` — `_vacuum_start` calls the **bare** `set_cleaning_mode`, **not** the silent helper, so `_silent_stop_deadline` is left None |
| **+1.057 s** (14:30:01.380)      | HA       | `Set cycle time, Desired: {cycleInfo: {cycleTime: 150}}` — BUG-08 chain (provenance-gated, ours)                                                                                          |
| **no Set power state**           | —        | The BUG-13 observer evaluates `_silent_stop_due` on the cycleTime echo: deadline is None → returns False → `pause()` is **not** called                                                    |
| +3.117 s                         | firmware | `reported.systemState = {pwsState:"on", robotState:"init", rTurnOnCount: still 58 in this echo}`                                                                                          |
| +~150 s (estimated)              | firmware | `rTurnOnCount` increments to 59 (observed via subsequent dynamic payloads)                                                                                                                |

Post-conditions:

- HA `vacuum.nono_2` transitioned to `cleaning` with `mode=stairs`.
- `sensor.nono_2_nombre_de_cycles` = 59 (was 58).

The split between the silent path and Start is intact: the bare `set_cleaning_mode` does not arm the silent deadline, so the cycleTime echo is recognised by BUG-08 only and the new BUG-13 branch is structurally unreachable on this path. Cycle counter correctly bumps for a fresh cycle.

## Actions 3 & 4 — pick mode while running

`03_live_mode_swap_stairs_to_floor.mqtt.log` (`stairs → floor`) and `04_live_mode_swap_floor_to_all.mqtt.log` (`floor → all`). Operator picked a new mode in the HA combo while `vacuum.nono_2` was `cleaning`.

| #   | Time                                   | Mode write | BUG-08 cycleTime | `Set power state`? | Cycles before/after |
| --- | -------------------------------------- | ---------- | ---------------- | ------------------ | ------------------- |
| 3   | 14:33:16.950 → 14:33:18.020 (+1.070 s) | `floor`    | 120 min          | none               | 59 → 59             |
| 4   | 14:38:16.711 → 14:38:17.776 (+1.065 s) | `all`      | 60 min           | none               | 59 → 59             |

Both events follow the **today-preserved live-write path**: `coordinator._set_cleaning_mode` sees `self._system_details.is_active == True` and routes to the bare `aws_client.set_cleaning_mode`. The silent deadline is never armed, the BUG-13 observer never fires, behaviour is identical to pre-fix.

Empirical findings:

- **The firmware adopts a mode-write on a running robot in-flight, with no `off` interlude** — matches MAP-03 finding from session 2026-06-13.
- **`rTurnOnCount` does not increment on a running-robot mode-swap** — this answers the open empirical question flagged in the design review. The firmware treats it as a continuation of the existing cycle, not a fresh start. The integration's HA `sensor.nono_2_nombre_de_cycles` was 59 before, between, and after both swaps.
- **`cycleStartTime` was not restamped** by either swap. The cycle start observed in the shadow at 14:36:31 (`cycleStartTime=1782052352`, ≈ 14:32:32 CEST) is the firmware's stamp of the Run from Action 2 actually taking effect; the swap to `floor` at 14:33:16 left it intact. The same invariant held across the swap to `all` at 14:38:16.

## Notes on the Maytronics app

- After Action 1 the app correctly displayed the robot as stopped, with the "default" cleaning mode set to `Couverture complète`. The integration's silent-set + pause is therefore not perceptually distinct from a manual mode change in the app — the operator sees a chosen mode and a docked robot, which is the BUG-13 desired UX.
- After Action 4, the app showed mode `Complet` with the three duration presets (`2h / 2h30 / 3h`) all greyed out. The integration pushed `cycleTime=60` (= configured value of `number.nono_2_duree_du_cycle_complet`), which is off the app's preset grid. This reproduces the Appendix B finding from session 2026-06-13 (`docs/diag/2026-06-13_map-03_unknown-modes-validation/findings.md`): the app compares `reported.cleaningModes.<mode>` against its own preset list and highlights matches only. Not a fix-introduced regression.
- During Action 3, the app's displayed cycle title lagged behind the firmware state for a few seconds before catching up — same async-display latency observed in earlier sessions.

## What the test does not cover

- **The overlap residual** flagged in the design review (two `set_cleaning_mode_silent` calls within the ~1.2 s BUG-08 window sharing the scalar deadline) is **not exercised here**. The behaviour is locked by `test_overlapping_silent_sets_produce_a_single_pause` in `tests/test_bug_13_decouple_mode_pick.py` — a SPIKE-02-D4-style on-robot torture run stays the empirical follow-up if needed.
- **Cycle-time edge cases on the silent path** (e.g. picking a mode whose configured `number.<robot>_duree_du_cycle_<mode>` is off-grid for the app's presets) were not characterised. Action 4 incidentally proves the live path's behaviour, but the silent path's interaction with the app's preset-matching display is open.
- **Long-window persistence** of the silent-set adoption (does the firmware retain mode + cycleTime across an hours-long idle?) was not measured — the Run in Action 2 happened ~5 minutes after the silent set, well within any plausible firmware decay window.

## Conclusion

The four scenarios exercise the full BUG-13 fix surface on real hardware. The silent E-B primitive lands as designed, the start path is unchanged, the running-robot live-write path keeps today's app-parity behaviour, and the firmware does not bump `rTurnOnCount` on a running-robot mode-swap (settling the review's open question). The fix shipped in `v1.0.26b3-raoul.10` is validated in vivo.

## See also

- [Home Assistant - Dolphin S2000](https://github.com/raouldekezel/it-documentation) — main S2000 doc
- [Home Assistant - Dolphin S2000 - AWS Shadow Structure](https://github.com/raouldekezel/it-documentation) — shadow cartography
- [PR #86](https://github.com/raouldekezel/dolphin-robot/pull/86) — the fix
- [PR #85](https://github.com/raouldekezel/dolphin-robot/pull/85) — the design experiments (E-A FAIL / E-B PASS)
- [Issue #47](https://github.com/raouldekezel/dolphin-robot/issues/47) — BUG-13
- [Issue #48](https://github.com/raouldekezel/dolphin-robot/issues/48) — BUG-14 (untouched)
- `docs/diag/2026-06-15_feat-01_stairs-validation/findings.md` — mode survives stop, cycle counter behaviour reference
- `docs/diag/2026-06-13_map-03_unknown-modes-validation/findings.md` — mode-swap-without-off baseline, app preset-grid behaviour
