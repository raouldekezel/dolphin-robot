# BUG-19 — silent E-B fails on a second consecutive docked mode pick

## TL;DR

A second silent (docked) cleaning-mode pick can fail to keep the robot
docked: the firmware flips `pwsState=on` despite our committed
`pwsState=off`, and in the worst case commits a full cycle
(`rTurnOnCount` bump). The integration-side wire is byte-identical to a
successful pick — same `clientToken`, `mode → +1.0 s → cycleTime →
+50–65 ms → pause`, AWS `/update/accepted` confirmed for every write — so
the failure is firmware-side.

**The in-vivo data does NOT support a Δt threshold.** The longest interval
tested (+133 s, pick E) also breaks, while +90 s and +98 s hold — the
outcome is non-monotonic in Δt, and the `cycleStartTime` restamp does not
gate it (pick E restamps and still starts). The one variable that
separates all seven observed picks is the **target `cycleTime`**
(`≤ 60` → break; `≥ 120` → hold). This is an **unconfirmed hypothesis**
pending the deconfounding experiment in _Open questions_.

> ⚠️ **Revision note (review of 2026-06-25).** An earlier draft of this
> file claimed a `17 s < Δt < 90 s` breakage bracket and named the
> `cycleStartTime` restamp as the structural cause. Re-reading the raw
> `03_repro.mqtt.log` falsified both: pick E (+133 s) was misread as a
> clean HOLD — it actually flips `pwsState=on/init` for ~4 s and it _did_
> restamp `cycleStartTime`. The bracket, F8, and Q1's "PARTIALLY
> ANSWERED" are retracted below. F6 (historique) is downgraded to
> unreliable.

## Context

- **Date** : 2026-06-25
- **Robot** : Maytronics Dolphin S2000 (`Nono 2`), robot-type `S4`
- **Firmware** : `pwsSwVersion: 11.0004`, `muSwVersion: 9F88`
- **Fork tag of the integration under test** :
  [`v1.0.26b3-raoul.12`](https://github.com/raouldekezel/dolphin-robot/releases/tag/v1.0.26b3-raoul.12)
  (FEAT-05 shipped — `select.{robot}_clean_mode` reuses the same
  `_set_cleaning_mode` write path as `vacuum.set_fan_speed`)
- **HA version** : Home Assistant 2026.1.3 (Docker, container `hass` on intel-nuc)
- **Pre-experiment state** : robot idle after the morning weekly schedule
  fire (cycle 65, mode `all`, started at 11:00 UTC). At 11:43:16 UTC HA
  reconnects to AWS after a `docker restart hass` (unrelated dashboard
  tweak); shadow GET reports `pwsState=holdWeekly, robotState=notConnected,
rTurnOnCount=65, cleaningMode={all,150}`, robot truly docked. WiFi
  RSSI = -30 dBm.

## Actions taken

1. **`01_picks.mqtt.log` — pick A (`stairs`) and pick B (`short`)
   issued via the new `select.{robot}_clean_mode` dropdown**, +17 s
   apart (operator-driven from the Lovelace dashboard). The original
   incident slice; covers 11:42:46 → 11:45:46 UTC, PII redacted per
   `docs/diag/README.md` table.
2. **`02_historique.sensors.txt` — `sensor.{robot}_historique` snapshot
   showing the two most recent cycles** captured immediately after the
   incident (HA-side derived rollup). Originally cited as corroborating
   evidence for F6; see F6 for why it is unreliable.
3. **`03_repro.mqtt.log` — bracketing session of 5 single-mode picks**
   on the same firmware/integration build later the same day. Slice
   covers **12:09:16 → 12:19:35 UTC**, PII redacted. Picks: `stairs`
   (single after a 26 min quiet), `floor` (+98 s), `short` (+133 s),
   `stairs` (+90 s), `all` (+5 s).

All picks transit `_set_cleaning_mode` and, because
`self._system_details.is_active` reads `False` (vacuum state = `DOCKED`
from `pwsState=holdWeekly`), route to
`aws_client.set_cleaning_mode_silent` (the BUG-13 E-B path).

## Timeline

All times UTC. Source files cited per row in the rightmost column.

### Original incident (`01_picks.mqtt.log`)

Two operator picks at 11:43:54 and 11:44:11 (+17 s), `stairs → short`.

#### Pick A — `stairs` (cycleTime 150), silent path succeeds

| Time         | Event                                                                                                                         | Source              |
| ------------ | ----------------------------------------------------------------------------------------------------------------------------- | ------------------- |
| 11:43:54.950 | `Set cleaning mode, Desired: {cleaningMode: {mode: stairs}}` published with `clientToken=…b6d9e72`                            | `01_picks.mqtt.log` |
| 11:43:55.039 | `/update/accepted` mode echo (BUG-08 chain triggered in awscrt-thread)                                                        | `01_picks.mqtt.log` |
| 11:43:56.039 | `Set cycle time, Desired: {cycleInfo: {cycleTime: 150}}` published (BUG-08 reactive, +1.000 s after mode publish)             | `01_picks.mqtt.log` |
| 11:43:56.045 | `/update/accepted` cycleTime echo, `_silent_stop_due` ⇒ `True`, `pause()` called                                              | `01_picks.mqtt.log` |
| 11:43:56.098 | `Set power state, Desired: {systemState: {pwsState: off}}` published (silent E-B pause, +53 ms after cycleTime echo)          | `01_picks.mqtt.log` |
| 11:43:56.146 | `/update/accepted` pwsState echo                                                                                              | `01_picks.mqtt.log` |
| 11:44:00.339 | `reported.systemState.pwsState = holdWeekly`, `rTurnOnCount = 65` (unchanged) — **robot stayed docked, silent E-B confirmed** | `01_picks.mqtt.log` |

Pick A reproduces #87's silent-pick PASS scenario byte-for-byte: mode +
cycleTime + pause within ~1.15 s, robot reports `holdWeekly`,
`rTurnOnCount` does not bump.

#### Pick B — `short` (cycleTime 60), silent path published, robot starts anyway

| Time         | Event                                                                                                                                                                   | Source              |
| ------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------- |
| 11:44:11.558 | `Set cleaning mode, Desired: {cleaningMode: {mode: short}}` published with same session `clientToken=…b6d9e72`                                                          | `01_picks.mqtt.log` |
| 11:44:11.637 | `/update/accepted` mode echo                                                                                                                                            | `01_picks.mqtt.log` |
| 11:44:12.637 | `Set cycle time, Desired: {cycleInfo: {cycleTime: 60}}` published (BUG-08 reactive, +1.000 s after mode publish)                                                        | `01_picks.mqtt.log` |
| 11:44:12.641 | `/update/accepted` cycleTime echo, `_silent_stop_due` ⇒ `True`, `pause()` called                                                                                        | `01_picks.mqtt.log` |
| 11:44:12.699 | `Set power state, Desired: {systemState: {pwsState: off}}` published (+58 ms after cycleTime echo — within the #87 spec)                                                | `01_picks.mqtt.log` |
| 11:44:12.756 | `/update/accepted` pwsState echo (AWS roundtrip confirmed)                                                                                                              | `01_picks.mqtt.log` |
| 11:44:13.271 | `reported.cycleInfo = {mode: short, cycleTime: 60}` — firmware adopted mode + cycleTime, `cycleStartTime` **carried over from pick A (11:44:00, now 13 s in the past)** | `01_picks.mqtt.log` |
| 11:44:14.072 | **`reported.systemState.pwsState = on, robotState = init, rTurnOnCount = 65`** — firmware transitioned ON 1.316 s after our `pwsState=off` was AWS-echoed back to us    | `01_picks.mqtt.log` |
| 11:44:59.582 | `pwsResponse cycleStart` dynamic payload                                                                                                                                | `01_picks.mqtt.log` |
| 11:45:03.990 | Operator-issued `pause` (MainThread, from `vacuum.pause`)                                                                                                               | `01_picks.mqtt.log` |
| 11:45:06.941 | `reported.rTurnOnCount = 66, mode: short` — cycle 66 was committed by the firmware                                                                                      | `01_picks.mqtt.log` |

The integration-side wire pattern is **identical** to pick A: same
publisher, same BUG-08 chain timing (mode→cycleTime = 1.000 s,
cycleTime→pause = 58 ms vs 53 ms for pick A), same `clientToken`, same
shadow path. AWS echoed our `pwsState=off` write back at 11:44:12.756 —
1.316 s before the robot reports `pwsState=on` at 11:44:14.072. Yet the
firmware started the cycle.

### Bracketing session (`03_repro.mqtt.log`)

Same firmware/integration build, 5 picks at varied Δt. All 5 publish the
full `mode → cycleTime → pwsState=off` chain on the silent path; the
differentiator is the firmware's response. **Outcome column reflects the
raw `reported.systemState.pwsState` transitions, re-verified line by line.**

#### Per-pick summary

| #   | Time UTC     | Mode     | cycleTime | Δt from prior              | `cycleStartTime` after pick            | `pwsState` after               | Outcome                     |
| --- | ------------ | -------- | --------- | -------------------------- | -------------------------------------- | ------------------------------ | --------------------------- |
| C   | 12:10:05.770 | `stairs` | 150       | n/a (first, ~26 min quiet) | restamped `1782396544`                 | `holdWeekly` ✓                 | **HOLD**                    |
| D   | 12:11:43.504 | `floor`  | 120       | +98 s                      | restamped `1782396672`                 | `holdWeekly` ✓                 | **HOLD**                    |
| E   | 12:13:56.970 | `short`  | **60**    | +133 s                     | restamped `1782396800`                 | `on` → `holdWeekly` ~4 s later | **BREAK** (self-abort ~4 s) |
| F   | 12:15:26.278 | `stairs` | 150       | +90 s                      | restamped `1782396928`                 | `holdWeekly` ✓                 | **HOLD**                    |
| G   | 12:15:31.361 | `all`    | **60**    | +5 s                       | **NOT restamped** (stays `1782396928`) | `on` → `holdWeekly` ~3 s later | **BREAK** (self-abort ~3 s) |

**Correction:** an earlier draft listed pick E as HOLD. The raw log shows
`reported.pwsState=on, robotState=init` at 12:13:58.995 (0.87 s after our
`pwsState=off` published at 12:13:58.125), persisting at 12:13:59.792,
back to `holdWeekly` at 12:14:02.970. Pick E breaks, and it restamped
`cycleStartTime` to a fresh `1782396800` — so restamp does **not** imply
HOLD.

#### Pick G — the +5 s reproduction

Identical wire shape to picks C/D/E/F (BUG-08 chain in 1.061 s, pause
+65 ms after cycleTime), firmware response diverges:

| Time         | Event                                                                                                                        | Source              |
| ------------ | ---------------------------------------------------------------------------------------------------------------------------- | ------------------- |
| 12:15:31.361 | `Set cleaning mode, Desired: {cleaningMode: {mode: all}}` (pick F is +5 s prior)                                             | `03_repro.mqtt.log` |
| 12:15:32.422 | `Set cycle time, Desired: {cycleInfo: {cycleTime: 60}}` (BUG-08 reactive, +1.061 s)                                          | `03_repro.mqtt.log` |
| 12:15:32.487 | `Set power state, Desired: {systemState: {pwsState: off}}` (silent E-B pause, +65 ms after cycleTime publish)                | `03_repro.mqtt.log` |
| 12:15:33.680 | `reported.cycleInfo = {mode: all, cycleTime: 60}, cycleStartTime = 1782396928` ← **inherited unchanged from pick F**         | `03_repro.mqtt.log` |
| 12:15:33.680 | `reported.systemState = {pwsState: on, robotState: init, rTurnOnCount: 66}` — **firmware transitioned ON despite our pause** | `03_repro.mqtt.log` |
| 12:15:36.745 | `reported.systemState = {pwsState: holdWeekly}` — robot back to dock after ~3 s on the shadow side (operator reports ~10 s)  | `03_repro.mqtt.log` |
| —            | `rTurnOnCount` stays at `66` — the run was too brief for the firmware to commit a new cycle                                  |                     |

#### Outcome correlates with target `cycleTime`, not Δt or restamp

Re-tabulating all seven picks against the three candidate variables:

| Variable                                      | Fits HOLD/BREAK split | Counterexample        |
| --------------------------------------------- | --------------------- | --------------------- |
| **`cycleTime`** (≤60 break, ≥120 hold)        | **7/7**               | none                  |
| Δt (≤17 s break, ≥90 s hold)                  | 6/7                   | E (+133 s) breaks     |
| `cycleStartTime` restamp (no-restamp ⇒ break) | 6/7                   | E restamps and breaks |

Every `cycleTime=60` pick broke (B, E, G — Δt = +17/+133/+5, so the effect
is independent of Δt within the tested set); every `cycleTime ≥ 120` pick
held (A, C, D, F). The `cycleStartTime` restamp is itself Δt-gated
(no-restamp at small Δt: B +17 s, G +5 s; restamp at large Δt: D/E/F), but
it is **not** the HOLD/BREAK discriminator — pick E is the clean
disproof. See _Open questions_ for the hypothesis and the experiment that
would confirm or kill it.

## Findings

- **F1 — Not a FEAT-05 regression.** `select.{robot}_clean_mode`
  dispatches via `MyDolphinPlusSelectEntity.async_select_option`
  (`select.py:53-55`) → `async_execute_device_action(SERVICE_SELECT_OPTION,
option)` → `_set_cleaning_mode`. `vacuum.set_fan_speed` dispatches via
  `MyDolphinPlusVacuumEntity.async_set_fan_speed` (`vacuum.py:76-77`) →
  `async_execute_device_action(SERVICE_SET_FAN_SPEED, fan_speed)` →
  `_set_cleaning_mode`. Both land on the same handler. The race is
  firmware-side and pre-dates FEAT-05; #93 changes nothing about
  `set_cleaning_mode_silent`, the BUG-08 reactive chain, or the `pause()`
  shape discriminator.

- **F2 — Integration-side wire pattern is byte-for-byte identical
  across the two incident picks.** Same `clientToken`, same publisher
  thread pattern (MainThread for mode publish, Dummy-N for the BUG-08
  reactive chain), same `mode → +1.000 s → cycleTime → +53–58 ms → pause`
  cadence, both within the #87-validated envelope. AWS roundtrip
  confirmed both pause writes (`/update/accepted` echoed at 11:43:56.146
  and 11:44:12.756). The deadline-arming + cycleTime-echo-gated `pause()`
  mechanism behaved as designed.

- **F3 — Pick A succeeded (E-B silent confirmed in vivo, replicating
  #87); pick B failed.** The differences between the two are temporal
  context, prior shadow state (`cycleStartTime` stale on B), **and the
  target `cycleTime` (A = 150, B = 60)**. Pick B updates
  `cycleInfo.cleaningMode = {short, 60}` but `cycleStartTime` is not
  restamped by the firmware (11:44:13.271 echo); the firmware then
  transitions `pwsState` to `on` 0.8 s later despite our `pwsState=off`
  already being committed to its desired slot.

- **F4 — The firmware-side race is not the AWS roundtrip.** AWS
  `/update/accepted` for our `pwsState=off` lands at 11:44:12.756, a full
  1.316 s before the robot reports `pwsState=on` at 11:44:14.072. The
  robot is online (`WIFI_RSSI=-30`, `isConnected.connected=true` in the
  same window). So either the firmware applies mode/cycleTime atomically
  with a "start now" side-effect that bypasses the pending `pwsState=off`,
  or it queues `pwsState=off` behind a "cycle start trigger" that wins the
  local race.

- **F5 — The single-pick #87 in-vivo validation does not cover this
  shape.** #87's "silent pick (docked, `all → stairs`)" scenario is one
  pick from a clean shadow, with `cycleTime` 150. The
  two-back-to-back-picks scenario was unit-tested in
  `test_overlapping_silent_sets_produce_a_single_pause` for **sub-1.2 s**
  picks (where the second pick shares the first's `_silent_stop_deadline`
  and only one `pause()` fires). Neither the multi-second-apart shape nor
  a low-`cycleTime` (`short`/60) silent pick is unit-tested or
  in-vivo-validated.

- **F6 — `sensor.{robot}_historique` is unreliable; not usable as
  evidence here.** The mode→label mapping is unambiguous (`fr.json`):
  `stairs` = "Couverture complète", `all` = "Complet". The snapshot
  (`02_historique.sensors.txt`) shows **both** rows as "Couverture
  complète", including the 11:00 cycle — which ran in `all`
  (`cycleInfo.cleaningMode={all,150}` in the baseline shadow). The
  historique `mode` field therefore mislabels a cycle whose mode is known,
  so it cannot establish that the incident cycle executed pick A's
  `stairs`. The `start` timestamp (13:44) does match pick A's
  `cycleStartTime` stamp, but the `mode` attribution is not trustworthy.
  This sensor is a HA-side template (not in this repo) and is not
  verifiable here. **Earlier "corroborating / confirming" framing
  retracted.**

- **F7 — Repro reproduces the failure on demand but does NOT bracket a Δt
  threshold.** The longest interval tested (+133 s, pick E) breaks while
  +90 s (F) and +98 s (D) hold. The outcome is non-monotonic in Δt, so the
  earlier `17 s < Δt < 90 s` claim is withdrawn. The failure is
  reproducible (3 BREAK picks: B, E, G), but not bracketed by Δt.

- **F8 — RETRACTED: `cycleStartTime` restamp is NOT the structural
  cause.** Pick E restamped `cycleStartTime` to a fresh-forward
  `1782396800` (12:13:58.141) and still flipped `pwsState=on`. The
  restamp is itself Δt-gated (small Δt → no restamp: B, G; large Δt →
  restamp: D, E, F), but it does not determine HOLD vs BREAK. The
  candidate fix in Q2 (republish/zero `cycleStartTime`) loses its
  rationale. **The variable that fits the split is `cycleTime`** (see the
  per-pick correlation table and Open questions).

- **F9 — Picks E and G self-aborted at ~3–4 s (operator-confirmed); pick
  B committed a real cycle and ran until the operator stopped it.** E and
  G return to `holdWeekly` ~4 s / ~3 s after `on/init`, `rTurnOnCount`
  unchanged. **The operator confirms he never intervenes before ~60 s**,
  so these sub-4 s returns are genuine firmware self-aborts, not operator
  pauses. Pick B is different: it bumped `rTurnOnCount` 65→66 (a committed
  cycle) and was stopped by an operator `vacuum.pause` (logged at +49.9 s
  from `pwsState=on`; operator recalls ~60 s — a ~10 s recollection/clock
  gap, immaterial here). So among the three `cycleTime=60` BREAKs, B
  committed-and-ran while E and G micro-started and self-aborted — a
  run-length asymmetry **not** explained by `cycleTime` or restamp (B and
  G are both no-restamp, ct 60; B ran, G aborted). Tracked as Q5.

## Open questions

- **Q1 (OPEN).** What firmware-side property decides whether the silent
  `pwsState=off` holds? In-vivo, the outcome correlates with the target
  **`cycleTime`** (≤ 60 → break; ≥ 120 → hold), 7/7, while Δt and the
  `cycleStartTime` restamp each fit only 6/7. This is a **hypothesis**
  (H1), not a cause — the data has empty cells. The test plan that would
  confirm or kill it is tracked in
  [issue #96](https://github.com/raouldekezel/dolphin-robot/issues/96).

  > **H1 — outcome tracks the target `cycleTime`, not the inter-pick
  > interval.** Caveat: no high-`cycleTime` pick has been tested at small
  > Δt, and no _lone_ low-`cycleTime` pick has been tested. No firmware
  > mechanism is asserted.
  >
  > **H1′ (operator hypothesis) — "the firmware balks at cycleTimes the
  > app never offers."** The Maytronics app exposes a duration picker only
  > for the long/coverage family — 2 h / 2 h 30 / 3 h (= 120 / 150 / 180
  > min); `short` ("rapide") offers no duration choice at all. Against
  > _that_ reference set the split is clean and **7/7**: every pick with
  > `cycleTime ∈ {120,150,180}` held (A/C/D/F); every pick with
  > `cycleTime ∉` that set broke (B/E/G) — **including G** (`all` written
  > at 60: a long-family mode at an out-of-menu duration), which rules out
  > "it's the `short` mode itself." On the current data this is
  > observationally identical to H1 ("value 60"), since 60 is the only
  > sub-120 value tested — E4a/E4b separate the two. (An earlier draft
  > rebutted this against the firmware `cleaningModes` defaults — a
  > different reference set where `floor=150` — and wrongly flagged pick D
  > as a counterexample; retracted.)

- **Q2 (weakened).** Does republishing `cycleStartTime` (or zeroing it) on
  the second pick prevent the start? With F8 retracted, the restamp is no
  longer implicated, so this is demoted from "prime fix direction" to a
  speculative branch. Lower priority than testing H1.

- **Q3.** Would chaining a **second** `pwsState=off` write a few hundred
  ms after the first one rescue the late race, or would the firmware
  ignore the duplicate? (Independent of H1; a candidate mitigation
  regardless of the trigger — see E5 in #96.)

- **Q4.** Does the issue reproduce when both picks are issued via
  `vacuum.set_fan_speed` (the pre-FEAT-05 entry point), confirming F1
  empirically rather than just from code-path inspection?

- **Q5.** Why do the observed BREAKs self-abort at ~3–4 s (E, G) while pick
  B committed and ran to the +49.9 s operator pause? Possibly a Δt or
  cycleTime interaction on run-length; uncharacterised.

### Test plan

The deconfounding experiment plan (E1, E2, E3, E4a/b/c, N1, and the
fix-direction probe E5) is the live checklist in
**[issue #96](https://github.com/raouldekezel/dolphin-robot/issues/96)**
— the single source of truth for what to run next. This file is a
session record, not a forward to-do; the plan is maintained in the issue.

## Refs

- [BUG-13](https://github.com/raouldekezel/dolphin-robot/issues/47) —
  picking a cleaning mode in the vacuum combo box starts a cleaning
  cycle when the robot is docked (parent design).
- [BUG-13 in-vivo validation #87](https://github.com/raouldekezel/dolphin-robot/pull/87) —
  single-pick silent E-B PASS on `v1.0.26b3-raoul.10` (mode `stairs`,
  cycleTime 150 — note: a high-`cycleTime` pick, per H1).
- [BUG-08](https://github.com/raouldekezel/dolphin-robot/issues/17) —
  reactive `Set cycle time` chain; executes correctly on every pick here
  (same +1.000 s offset).
- [SPIKE-02](https://github.com/raouldekezel/dolphin-robot/issues/70) —
  `clientToken`-gated provenance; both picks carry the session token
  end-to-end (E4 gate not exercised — no `/update/rejected`).
- [FEAT-05 #93](https://github.com/raouldekezel/dolphin-robot/pull/93) —
  shipped 2026-06-25 as `v1.0.26b3-raoul.12`; under test here. **Not the
  cause** (see F1).
