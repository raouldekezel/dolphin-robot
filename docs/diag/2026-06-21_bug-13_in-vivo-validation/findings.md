# BUG-13 — in-vivo validation of the decoupling fix on raoul.10

## TL;DR

End-to-end validation of [PR #86](https://github.com/raouldekezel/dolphin-robot/pull/86) on the live robot, exercising the three code branches the fix introduces and the residual question left open by the code review.

- **Pick mode while docked (silent E-B) → PASS.** Mode + cycleTime adopted by the firmware, `rTurnOnCount` unchanged, `vacuum.nono_2` stayed `docked` in HA throughout. The integration emits the exact `set + cycleTime + pause` triple #85 predicted, with the pause landing ~84 ms after the cycleTime echo. The shadow shows the firmware did transiently report `pwsState=on / robotState=init` for ~4 s before adopting our queued pause; HA never propagated that transient because the coordinator's MQTT debouncer (`cooldown=1.0 s`) coalesced the burst — see the investigation section under Action 1.
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

Two distinct mode notions are at play and the table separates them explicitly:

- **Currently running mode** — the cleaning program the robot is _physically executing_ right now. Observable through the robot's behaviour, not directly in the shadow.
- **Current next mode** — the slot the integration and the Maytronics app display as "the current mode". This is `reported.cycleInfo.cleaningMode.mode`; despite its name, on a running robot the firmware updates it to the next mode without restarting the in-flight cycle. The shadow doc §A's "État du cycle en cours" wording predates this empirical finding.

| #   | Action         | Pre-state (HA)              | Pick     | Code path                                     | Cycles  | Pause emitted?                            | Current next mode (HA / app)              | Currently running mode (physical)                                      |
| --- | -------------- | --------------------------- | -------- | --------------------------------------------- | ------- | ----------------------------------------- | ----------------------------------------- | ---------------------------------------------------------------------- |
| 1   | Pick docked    | `holdWeekly`, mode=`all`    | `stairs` | `set_cleaning_mode_silent` (E-B)              | 58 → 58 | ✅ yes (~84 ms post-cycleTime echo)       | updated to `stairs`, cycleTime 150        | — (robot stays docked, not cleaning)                                   |
| 2   | `vacuum.start` | `holdWeekly`, mode=`stairs` | —        | `_vacuum_start` → bare `set_cleaning_mode`    | 58 → 59 | ❌ no (`_silent_stop_deadline` not armed) | unchanged: `stairs`                       | starts: `stairs` (transition `holdWeekly → on / init / scanning`)      |
| 3   | Pick running   | `cleaning`, mode=`stairs`   | `floor`  | bare `set_cleaning_mode` (today's live write) | 59 → 59 | ❌ no (`is_active` branch skips silent)   | updated to `floor`, cycleTime 120         | unchanged: still `stairs` from Action 2 (no off interlude, no restart) |
| 4   | Pick running   | `cleaning`, mode=`floor`    | `all`    | bare `set_cleaning_mode` (today's live write) | 59 → 59 | ❌ no                                     | updated to `all`, cycleTime 60 (off-grid) | unchanged: still `stairs` from Action 2                                |

The four scenarios exercise every branch of the BUG-13 split: `is_active=False` → silent E-B; `is_active=True` → live; `_silent_stop_deadline` armed vs. not armed; mode-echo vs. cycleTime-echo discriminator in the observer.

The Actions 3 and 4 finding (running-robot mode write updates the "next" slot but does not restart the in-flight cycle) is **exactly the contract item 1 wording** from the design comment on #47 ("queue for next cycle; no live restart"). The live path therefore implements that contract by passthrough — the firmware itself handles the queue-for-next semantics; the integration just needs to forward the write.

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
- `reported.systemState.pwsState` transiently went `on` (firmware-side, ~4 s, see investigation below) before settling back to `holdWeekly`.
- `rTurnOnCount` remained 58.
- HA `vacuum.nono_2` stayed `docked` (recorder DB query — the firmware-side `pwsState=on` transient never propagated to the HA state machine; see investigation).
- Maytronics app reported the robot as stopped, with the default mode now `Couverture complète`.

Matches #85 E-B PASS at the level the operator perceives. Earlier sessions assumed the pause arrived strictly before the firmware's `pwsState=on` transition (#85 measured ~2.5 s firmware reaction window); this session shows the firmware actually did flip `on` briefly. The user-visible PASS therefore depends on both our quick `pause()` (which the firmware applies as soon as it's done with its init phase) and the HA-side MQTT debouncing (which hides the brief `on` reflection from the HA state machine).

### Investigation — does the firmware briefly flip `pwsState=on` during Action 1, and does HA see it?

Reviewer hypothesis (2026-06-21): the firmware should have transiently flipped `pwsState=on` after the mode write, before our `pause()` was processed; if so, `SystemDetails._get_updated_data` maps `pwsState=on` → `VacuumActivity.CLEANING`, so `vacuum.nono_2` should have briefly read `cleaning`.

**Shadow side — confirmed.** Decoded `Payload` lines from `01_silent_pick_all_to_stairs.mqtt.log`:

| Δ T₀ (UTC)   | `reported.systemState.pwsState` | `reported.systemState.robotState` |
| ------------ | ------------------------------- | --------------------------------- |
| 12:24:30.799 | `holdWeekly`                    | `notConnected`                    |
| 12:24:33.238 | **`on`**                        | **`init`**                        |
| 12:24:34.073 | **`on`**                        | **`init`**                        |
| 12:24:37.251 | `holdWeekly`                    | `notConnected`                    |

The firmware did briefly transition for ~4 s. Our `pause()` (published at 12:24:31.938) had been ACK'd before the firmware reported `on`, but the firmware's start-machine had already advanced past the point where the next `pwsState=off` write could pre-empt it — it ran a brief init, then accepted our queued stop.

**HA side — `vacuum.nono_2` stayed `docked` throughout.** Raw `states` rows from the HA recorder (`docker exec hass python3 …` against `/config/home-assistant_v2.db?mode=ro`, joining `states` ⨝ `states_meta`, window 12:20:00 → 12:45:00 UTC):

```
14:20:56.814  unavailable
14:21:00.801  unavailable
14:21:07.427  docked
14:24:40.764  docked          ← no `cleaning` row between Action 1 (14:24:30) and Action 2 Start (14:30:10)
14:30:10.327  cleaning        ← Action 2 control row (Start) — present ✓
14:33:26.956  cleaning        ← Action 3 (pick floor) — attributes change, state same
14:38:26.716  cleaning        ← Action 4 (pick all) — attributes change, state same
14:41:33.333  docked
```

`recorder.commit_interval` is the default 1 s (a 4 s state would survive it), `vacuum.nono_2` is not in the recorder exclude list (only one Navimow sensor is). The recorder saw everything that was published to the state machine.

**So why didn't HA see the transient?** `coordinator._on_mqtt_data_update` debounces shadow updates through a `Debouncer(cooldown=1.0, immediate=False)` (`coordinator.py:173-179`); each MQTT message resets the cooldown. Across the 4 s `pwsState=on` window the firmware published at 33.238, 34.073 and 37.251 — at most one debounced refresh would land around ~35.073 with `pwsState=on` still set, and HA Core's own `DataUpdateCoordinator.async_request_refresh` adds its own throttling on top. In practice the refresh that did fire after this debounce window arrived only at 12:24:40.764 with `pwsState=holdWeekly` already restored — hence the `docked → docked` row at 14:24:40 (attributes carry the new `mode=stairs`, the state value never changed).

**Operationally** this is the BUG-13 intent: the operator does not perceive a `cleaning` blip during the silent pick. The combination of E-B's quick `pause()` _and_ the integration's MQTT debouncing means the transient firmware `pwsState=on` is invisible to HA. The fix relies on the second mechanism to fully hide the firmware-side race; documenting it here so a future debouncer rewrite knows it is load-bearing for the UX, not just performance.

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

- **A mode-write while running updates only the "current next mode" slot, NOT the in-flight cycle.** The firmware accepts the write, mirrors it into `reported.cycleInfo.cleaningMode = {mode, cycleTime}` (which HA and the Maytronics app both display as "current mode"), and the next-mode/next-duration tiles refresh — but the physical robot **continues the previously-started cycle** in its original mode and cycleTime to completion. Direct operator observation at Action 4: app showed mode `Complet` with greyed presets, while the robot was still physically cleaning floor at ~5 % progress. The mid-cycle physical mode swap claimed in earlier sessions (MAP-03, 2026-06-13) is **superseded** by this observation; the previous interpretation conflated the shadow slot's value with the physically-executing program.
- **`rTurnOnCount` does not increment on a running-robot mode write** — settles the open empirical question flagged in the design review. Cycle counter stayed at 59 across both Actions 3 and 4. Direct consequence of the above: the cycle never restarted, so the counter never ticked.
- **`cycleStartTime` was not restamped** — same root cause. The cycle start observed in the shadow at 14:36:31 (`cycleStartTime=1782052352`, ≈ 14:32:32 CEST) is the firmware's stamp of the Run from Action 2; both swaps left it intact.
- **The shadow's `reported.cycleInfo.cleaningMode.mode` is therefore a misleading slot name.** Despite the doc's "État du cycle en cours" wording, on a running robot this slot holds the _next_ mode the firmware will use, not the one it is currently executing. The actually-executing mode is some internal firmware state not directly exposed in the shadow. Worth amending the shadow cartography doc §A.

This is **exactly the contract item 1 the operator wrote on #47 on 2026-06-20** ("queue for next cycle; no live restart"). The live path implements it by passthrough — the firmware itself handles the queue-for-next semantics; the integration just forwards the write.

## Notes on the Maytronics app

- After Action 1 the app correctly displayed the robot as stopped, with the "default" cleaning mode set to `Couverture complète`. The integration's silent-set + pause is therefore not perceptually distinct from a manual mode change in the app — the operator sees a chosen mode and a docked robot, which is the BUG-13 desired UX.
- After Action 4, the app showed mode `Complet` with the three duration presets (`2h / 2h30 / 3h`) all greyed out **while the robot continued the previously-started cycle**. The greying is the Appendix B finding from session 2026-06-13: the app pushed `cycleTime=60` (= configured value of `number.nono_2_duree_du_cycle_complet`) which is off the app's preset grid `{120, 150, 180}` for `Complet`. The continued-cleaning-in-old-mode is the running-robot finding documented in the previous section.
- During Action 3, the app's displayed cycle title lagged behind the firmware state for a few seconds before catching up — same async-display latency observed in earlier sessions.

## What the test does not cover

- **The overlap residual** flagged in the design review (two `set_cleaning_mode_silent` calls within the ~1.2 s BUG-08 window sharing the scalar deadline) is **not exercised here**. The behaviour is locked by `test_overlapping_silent_sets_produce_a_single_pause` in `tests/test_bug_13_decouple_mode_pick.py` — a SPIKE-02-D4-style on-robot torture run stays the empirical follow-up if needed.
- **Cycle-time edge cases on the silent path** (e.g. picking a mode whose configured `number.<robot>_duree_du_cycle_<mode>` is off-grid for the app's presets) were not characterised. Action 4 incidentally proves the live path's behaviour, but the silent path's interaction with the app's preset-matching display is open.
- **Long-window persistence** of the silent-set adoption (does the firmware retain mode + cycleTime across an hours-long idle?) was not measured — the Run in Action 2 happened ~5 minutes after the silent set, well within any plausible firmware decay window.

## Conclusion

The four scenarios exercise the full BUG-13 fix surface on real hardware. The silent E-B primitive lands as designed, the start path is unchanged, and the running-robot live-write path keeps today's app-parity behaviour. The firmware does not bump `rTurnOnCount` on a running-robot mode write — directly because the in-flight cycle is not restarted; only the "current next mode" slot is updated. The live path therefore implements the operator's contract item 1 from #47 ("queue for next cycle; no live restart") by passthrough. The fix shipped in `v1.0.26b3-raoul.10` is validated in vivo.

## See also

- [Home Assistant - Dolphin S2000](https://github.com/raouldekezel/it-documentation) — main S2000 doc
- [Home Assistant - Dolphin S2000 - AWS Shadow Structure](https://github.com/raouldekezel/it-documentation) — shadow cartography
- [PR #86](https://github.com/raouldekezel/dolphin-robot/pull/86) — the fix
- [PR #85](https://github.com/raouldekezel/dolphin-robot/pull/85) — the design experiments (E-A FAIL / E-B PASS)
- [Issue #47](https://github.com/raouldekezel/dolphin-robot/issues/47) — BUG-13
- [Issue #48](https://github.com/raouldekezel/dolphin-robot/issues/48) — BUG-14 (untouched)
- `docs/diag/2026-06-15_feat-01_stairs-validation/findings.md` — mode survives stop, cycle counter behaviour reference
- `docs/diag/2026-06-13_map-03_unknown-modes-validation/findings.md` — mode-swap-without-off baseline, app preset-grid behaviour
