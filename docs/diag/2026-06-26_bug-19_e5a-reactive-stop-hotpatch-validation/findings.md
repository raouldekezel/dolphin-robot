# BUG-19 (E5a) — Reactive-stop hot-patch validation: silent picks no longer race the start

## TL;DR

E5a removes the **pre-emptive** `pwsState=off` (fired by `_silent_stop_due`
on the cycleTime echo of our own BUG-08 chain) and replaces it with a
**reactive** `pwsState=off` triggered the moment the firmware actually
reports `pwsState=on` after a silent set. A 12 s monotonic guard window
gates the hook to the silent-set lifetime; an edge predicate on
`_last_reported_pws_state` ensures a single `pause()` per `on` transition.

Validation on a live HA install (intel-nuc, file-level hot-patch over
`v1.0.26b3-raoul.12`, not a tagged release) with two scenarios:

- **T1 — isolated pick** (`all → stairs`, robot at dock, 5 min idle): the
  firmware **does** flip `pwsState=holdWeekly → on, robotState=init` at
  T0 + 2.79 s. The reactive hook fires exactly once at T0 + 2.82 s; the
  firmware reports back `holdWeekly` at T0 + 6.0 s with `rTurnOnCount=71`
  **unchanged**. The pre-patch invariant "silent set never visibly flips
  to `on`" no longer holds — see _Regressions_ below.
- **T2 — double pick 18 s apart** (`stairs → floor`, +18 s, `floor → short`):
  both picks fire the reactive hook (T0_A + 2.89 s, T0_B + 3.89 s),
  both redock under 4 s, `rTurnOnCount=71` throughout. **The BUG-19 race
  is closed**: the second pick is no longer free to start a cycle.
- **T3 — rapid-pick cascade** (added 2026-06-27, see § Session 2026-06-27):
  7 picks across 6 min 35 s, the last three within 11 s. **E5a survives
  the first 5 picks cleanly** (5 reactive stops, rTOC stable at 72).
  Pick #6 at +1.1 s from pick #5 is silently absorbed by the edge
  predicate (firmware still in the `on` plateau from pick #5) —
  intentional. Pick #7 fires its reactive stop. **13.6 s after pick #7's
  reactive stop, the firmware spontaneously re-flips to `pwsState=on`
  again, with no further write from the integration.** The reactive
  hook misses this re-flip because (a) `_last_reported_pws_state` is
  still `on` (firmware never published the post-pause `holdWeekly` until
  9.2 s after pick #5's pause, by which time pick #7 had already
  re-asserted `on`) and (b) pick #7's guard window had expired 4 s
  earlier. Motor engages 14 s later (smart-plug 3 → 82 W), shadow then
  freezes at `on/notConnected/72` for ~12 min while the robot physically
  cleans, app Maytronics displays « le robot ne répond pas » — a
  spontaneous BUG-20-style stuck-init/silent-shadow event triggered by
  rapid silent picks rather than the scheduled-trigger path of #98.
  Recovered by operator-instructed PWS power-cycle (off 30 s on).
  **`rTurnOnCount` never increments** (72 → 72), so the integration's
  cycle counter still doesn't reflect the run — same sentinel-stuck
  invariant as BUG-20 #98.

Operator-facing surface is silent across T1 and T2; for T3 the smart-plug
shows the cycle unambiguously and `sensor.nono_2_statut` goes to `off`
without ever stamping a `cleaning` row in `sensor.nono_2_historique`
(no commit, no recorded session).

- HA recorder logged **zero state changes** on `vacuum.nono_2`,
  `sensor.nono_2_statut`, `..._etat_du_robot`, `..._etat_de_l_alimentation`,
  `..._nombre_de_cycles` across the full 14:44 → 14:51 UTC window. The
  brief on→off transitions did not propagate past the integration's
  coordinator (`see 03_ha_recorder_snapshot.txt`).
- `sensor.nono_2_historique` (custom command-line cycle log defined in
  `pool_package.yaml`) registered **no entry** for T1, T2_A, or T2_B.
  Latest row is `26/06 16:38 → 16:39 Complet 🛑` — the manual stop of
  the running scheduled cycle three minutes before T1.

## Context

- **Date**: 2026-06-26
- **Robot**: Maytronics Dolphin S2000 (`Nono 2`), `robotType=S4`
- **Firmware**: `pwsSwVersion="11.0004"`, `muSwVersion="9F88"`
- **Baseline installed via HACS**:
  [`v1.0.26b3-raoul.12`](https://github.com/raouldekezel/dolphin-robot/releases/tag/v1.0.26b3-raoul.12)
  (same as BUG-19 #96 and BUG-20 #98)
- **What is actually running**: that tag's
  `custom_components/mydolphin_plus/managers/aws_client.py` overwritten
  in-place inside the running container, with the E5a patch (full
  unified diff in `aws_client_e5a_hotpatch.diff`). `aws_client.py.bak`
  preserved next to it for instant rollback. Patch not committed to
  `deploy`, not in any tag — this session is a pre-PR feasibility
  validation only.
- **HA version**: Home Assistant 2026.1.3 (Docker, container `hass` on
  intel-nuc, `network_mode: host`)
- **Pre-test state**: at 14:39:50 UTC the operator paused the running
  weekly cycle from HA, leaving the robot at
  `pwsState=holdWeekly, robotState=notConnected, rTurnOnCount=71,
cycleInfo.cleaningMode={all, 120}`. T1 was launched 5 min 33 s later
  (14:45:23 UTC).
- **Source files in this session**:
  - `aws_client_e5a_hotpatch.diff` — the 6-hunk unified diff applied
    on intel-nuc, +58/−12 lines.
  - `01_t1_isolated_pick.mqtt.log` — `mydolphin_plus` lines covering T1
    (14:45:20 → 14:45:39 UTC), PII redacted (MUSN, SSID, clientToken,
    tzName).
  - `02_t2_double_pick_18s.mqtt.log` — same for T2 (14:49:00 → 14:49:45 UTC).
  - `03_ha_recorder_snapshot.txt` — HA `/api/history/period` snapshot
    over the test window for the 5 Nono entities, plus the latest 6
    rows of `sensor.nono_2_historique`.

## Patch shape

E5a touches only `managers/aws_client.py`. Six hunks:

1. New constant `_SILENT_GUARD_TTL_SECONDS = 12.0` (must exceed observed
   on-latency ~3 s with margin, stay under any plausible inter-pick
   interval).
2. Two new instance attributes in `__init__`:
   `_silent_guard_deadline: float | None`,
   `_last_reported_pws_state: str | None` (edge-gate).
3. **Pre-emptive pause removed** from the `update_accepted` branch of
   `_message_callback`. The `elif self._silent_stop_due(desired):` arm
   and its `self.pause()` are deleted; the `if mode is not None:` arm
   that chains `Set cycle time` is untouched.
4. **Reactive hook** inserted in `_message_callback`, right after the
   `for category in reported.keys(): … self._on_data_update_callback()`
   merge loop. Reads `reported.systemState.pwsState`, edge-detects the
   transition into `PowerSupplyState.ON.value`, and if `_silent_guard_active()`
   issues exactly one `self.pause()` after logging a `WARNING`.
5. `set_cleaning_mode_silent` arms `_silent_guard_deadline` instead of
   `_silent_stop_deadline`; the `try/except` rollback updated to clear
   the new field.
6. New helper `_silent_guard_active()` next to `_silent_stop_due`. TTL
   self-clears when consumed.

`_silent_stop_due`, `_silent_stop_deadline`, `_SILENT_STOP_TTL_SECONDS`
become dead code under E5a (no remaining caller) and are intentionally
left in place for this hot-patch — they can be removed in the
upstream-bound PR.

## Timeline — T1 (isolated pick, `all → stairs`)

All times UTC. `T0_T1 = 14:45:23.598`.

| t (UTC)      | Δ (s)  | Source                    | Event                                                                                                               |
| ------------ | ------ | ------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| 14:45:23.598 | 0.000  | `curl POST` from trig-nuc | `vacuum.set_fan_speed(entity_id=vacuum.nono_2, fan_speed=stairs)`                                                   |
| 14:45:23.625 | +0.027 | `aws_client` log          | `Set cleaning mode, Desired: {'cleaningMode': {'mode': 'stairs'}}` (silent path, BUG-13)                            |
| 14:45:24.688 | +1.090 | `aws_client` log          | `Set cycle time, Desired: {'cycleInfo': {'cycleTime': 150}}` (BUG-08 chain, ~1.06 s)                                |
| 14:45:26.413 | +2.815 | shadow `update/documents` | firmware report `pwsState=on, robotState=init, rTurnOnCount=71` (unchanged from 71)                                 |
| 14:45:26.414 | +2.816 | `aws_client` log          | **WARNING `BUG-19 reactive stop: firmware reported pwsState=on after a silent set; issuing a single pwsState=off`** |
| 14:45:26.414 | +2.816 | `aws_client` log          | `Set power state, Desired: {'systemState': {'pwsState': 'off'}}` (one publish only)                                 |
| 14:45:26.467 | +2.869 | shadow `update/accepted`  | own desired echo: `{'systemState': {'pwsState': 'off'}}`                                                            |
| 14:45:29.611 | +6.013 | shadow `update/documents` | firmware report `pwsState=holdWeekly, robotState=notConnected, rTurnOnCount=71` (unchanged)                         |

`vacuum.pause` was invoked exactly once across the 6 s window
(`Set power state` line count = 1). The edge predicate held: the
follow-up `pwsState=holdWeekly` report at T0 + 6.0 s did not re-arm.

`rTurnOnCount` net change: **0** (71 → 71).

## Timeline — T2 (double pick 18 s apart, `stairs → floor → short`)

All times UTC. `T0_A = 14:49:02.982`, `T0_B = T0_A + 18.018 s = 14:49:21.000`.

| t (UTC)      | Δ from T0_A (s) | Event                                                                                                      |
| ------------ | --------------- | ---------------------------------------------------------------------------------------------------------- |
| 14:49:02.982 | 0.000           | `set_fan_speed(stairs → floor)` from trig-nuc                                                              |
| 14:49:03.008 | +0.026          | `Set cleaning mode floor` (silent)                                                                         |
| 14:49:04.123 | +1.141          | `Set cycle time 120`                                                                                       |
| 14:49:05.867 | +2.885          | **WARNING `BUG-19 reactive stop` + `Set power state off`** (pick A reactive)                               |
| 14:49:08.x   | +5.x            | firmware reports back `holdWeekly` (observed in raw shadow)                                                |
| 14:49:21.000 | +18.018         | `set_fan_speed(floor → short)` from trig-nuc                                                               |
| 14:49:21.033 | +18.051         | `Set cleaning mode short` (silent)                                                                         |
| 14:49:22.113 | +19.131         | `Set cycle time 60`                                                                                        |
| 14:49:24.889 | +21.907         | **WARNING `BUG-19 reactive stop` + `Set power state off`** (pick B reactive — **the BUG-19 failure case**) |

The 18 s gap exceeds `_SILENT_GUARD_TTL_SECONDS = 12.0`, so pick A's
guard has expired at the time pick B fires. Pick B arms a fresh guard
window, the firmware flips to `on` ~2.8 s later, and the hook fires
exactly once on the rising edge.

`rTurnOnCount` net change: **0** (71 → 71 across pick A and pick B).

## Operator-visible surface

`03_ha_recorder_snapshot.txt`:

```
=== vacuum.nono_2 ===
  2026-06-26T14:44:00+00:00  docked
=== sensor.nono_2_statut ===
  2026-06-26T14:44:00+00:00  holdweekly
=== sensor.nono_2_etat_du_robot ===
  2026-06-26T14:44:00+00:00  notconnected
=== sensor.nono_2_etat_de_l_alimentation ===
  2026-06-26T14:44:00+00:00  holdweekly
=== sensor.nono_2_nombre_de_cycles ===
  2026-06-26T14:44:00+00:00  71
```

Each of the 5 entities reports a single row at the start of the window
and no further transitions through 14:51 UTC. The brief firmware `on`
states (≈ 3 s at T1, ≈ 3 s on each of pick A and pick B in T2) were
absorbed inside the integration's coordinator — the `pwsState=on` shadow
update is observed by `_message_callback`, the reactive hook publishes
`pwsState=off`, the next merged snapshot already carries
`pwsState=holdWeekly`, so the corresponding `sensor.nono_2_etat_de_l_alimentation`
attribute never deltas. Same chain for `vacuum.nono_2` and the others.

`sensor.nono_2_historique` (custom command-line sensor reading the HA
recorder over 30 days, defined in `home-assistant/data/config/packages/pool_package.yaml`,
backed by `scripts/nono_history.py`) latest 6 rows after T2:

```
{'start': '26/06 16:38', 'mode': 'Complet', 'duration': '1min', 'end': '16:39', 'outcome': '🛑'}
{'start': '26/06 15:49', 'mode': 'Complet', 'duration': '48min', 'end': '16:38', 'outcome': '🛑'}
{'start': '26/06 15:00', 'mode': 'Complet', 'duration': '24min', 'end': '15:25', 'outcome': '🛑'}
{'start': '26/06 11:00', 'mode': 'Complet', 'duration': '3h51', 'end': '14:51', 'outcome': '✅'}
{'start': '26/06 08:46', 'mode': 'Sol uniquement', 'duration': '20s', 'end': '08:46', 'outcome': '🛑'}
{'start': '26/06 08:44', 'mode': 'Sol uniquement', 'duration': '51s', 'end': '08:45', 'outcome': '🛑'}
```

(times in CEST = UTC+2). Latest row is `26/06 16:38 → 16:39 🛑` — the
operator's manual pause of the running BUG-20 cycle 3 minutes before T1.
**No row for any of T1 (16:45), T2_A (16:49:02), T2_B (16:49:21)** —
none of the three brief flips produced a run-detectable interval in the
historique session detector (`vacuum.nono_2 ∈ {cleaning, returning}` for
a continuous span). That detector requires a `cleaning` state on
`vacuum.nono_2`; since the recorder never wrote one, no candidate
session exists.

## Findings

### F1 — BUG-19 race is closed by reactive design

The pre-emptive design was structurally vulnerable to a second silent
set whose own `pwsState=off` lost the race against the firmware's
`pwsState=on` adoption of the **first** silent set. By moving from
"publish `off` on a proxy event we believe correlates with the start"
to "publish `off` on the actual `pwsState=on` we observe", the patch
removes that correlated-event race entirely. T2 demonstrates the
expected outcome: each of two silent sets 18 s apart is independently
caught and redocked, with `rTurnOnCount=71` throughout.

### F2 — Operator surface is unaffected

The reactive `off` lands within ~3 s of the firmware's `on`. The
integration's coordinator absorbs both transitions before any
`async_write_ha_state()` propagates them to the entity layer (the
intra-callback `self._on_data_update_callback()` fires after the merge
of the `reported` keys, and the next callback already carries the
post-pause `holdWeekly` snapshot). Net effect: 5/5 monitored sensors
show zero state changes across the full test window, and the custom
historique sensor produces zero new rows. The patch is invisible to the
dashboard.

### F3 — Regression: isolated silent picks now produce a brief, visible firmware on→off

The pre-E5a invariant on a single silent set was "firmware adopts mode

- cycleTime, never flips `pwsState=on`". That invariant relied on the
  pre-emptive `pwsState=off` landing in the firmware's desired before the
  firmware decided to flip its reported. With the pre-emptive removed,
  the firmware **always** flips to `on` for ≈ 3 s before the reactive
  hook clears it. Observable consequences:

* **Counter**: `rTurnOnCount` does **not** increment in either T1 or T2
  (71 throughout) — the firmware counts the cycle only on durable `on`,
  not on the 3 s blip. So the user-facing "Nombre de cycles" stays clean.
* **Logs**: every silent pick now emits a `WARNING BUG-19 reactive stop`
  line; the previous quiet path is gone. This is intentional surface
  for diagnostic visibility, but worth a note in user-facing docs.
* **Cycle physical execution**: the brief 3 s `on` window does not
  start motors (the cleaning subsystem needs longer initialisation —
  cf. BUG-20 stuck-init analysis showing motor start is gated by
  `cycleStartTimeUTC` stamping which we never see here). To be
  re-confirmed with power-trace evidence on a future session.
* **`cycleStartTime` stamping**: the firmware does stamp
  `cycleStartTime` / `cycleStartTimeUTC` during the brief `on`
  (visible in raw shadow at T0 + 2.81 s for T1). That stamp remains
  visible in the `cycleInfo` of subsequent snapshots even after the
  `pause()` clears `pwsState`. Cosmetic, but means
  `cycleInfo.cycleStartTimeUTC` is no longer a reliable "last actual
  cycle" timestamp on a fork with E5a — track via `rTurnOnCount`
  delta instead.

### F4 — Guard TTL bound rationale

`_SILENT_GUARD_TTL_SECONDS = 12.0`. Lower bound: must exceed the worst
observed on-latency post silent set (T1 = 2.82 s, T2_A = 2.89 s,
T2_B = 3.89 s) with margin for AWS RTT degradation. Upper bound: must
stay under any plausible inter-pick interval — the user's mental model
of "pick mode X, then change my mind and pick Y" can land within
seconds; 12 s is well below typical reconsideration windows but high
enough to cover degraded networks. Tested at 18 s gap (T2), the
expired-guard path behaves correctly: pick A's guard cleared, pick B's
new guard fired its own reactive `off`.

### F5 — Edge predicate prevents reactive-stop self-loop on steady `on`

`_last_reported_pws_state` is updated **before** the
`_silent_guard_active()` check. A snapshot that already carried
`pwsState=on` from a previous merge would short-circuit (`became_on =
False`). Not exercised in this session — there is no path in T1/T2
where two consecutive `reported.systemState.pwsState=on` snapshots
arrive without an intervening transition — but the code path is
verifiable by inspection in `aws_client_e5a_hotpatch.diff` lines 28–48.

## Open questions for the upstream-bound PR

- **Should F3 be addressed before merging?** The brief firmware flip is
  cosmetic but introduces a counter-intuitive ~3 s `pwsState=on` window
  on every silent pick. Options:
  - (a) Accept it — simplest, current state.
  - (b) Add a 1–2 s `loop.call_later` delay before publishing the silent
    `Set cleaning mode` so the cycleTime side-write lands first and the
    firmware adopts mode + cycleTime in a single transaction (not yet
    proven to suppress the `on` flip — would need its own session).
  - (c) Combine `set_cleaning_mode` and `set_cycle_time` into one
    desired write (`{cleaningMode, cycleInfo.cycleTime}`) — SPIKE-02 E7
    has already ruled this out as the firmware silently ignores the
    sibling `cycleTime` in that combined shape; do not pursue.
- **Power-trace verification of F3** — confirm with a smart-plug trace
  that the 3 s `on` window does not draw motor power. If it does, the
  user-facing "no cycle ran" claim weakens.
- **Dead-code removal**: `_silent_stop_due`, `_silent_stop_deadline`,
  `_SILENT_STOP_TTL_SECONDS` should be removed in the PR commit that
  ports this hot-patch upstream, not in this validation diag.
- **`cleaningMode` mirror in `cycleInfo`** — pick A left the shadow
  with `cycleInfo.cleaningMode={floor, 120}` and pick B with
  `{short, 60}`. Persistence semantics are unchanged from BUG-18 —
  the operator-left state at end of T2 is `{short, 60}`, which the next
  weekly schedule fire would honour.

## Closing state (2026-06-26 T1/T2 session)

Restored before EOS: nothing to restore — the patch is intentionally
left in place at the operator's request. The robot is `docked`,
`statut=holdweekly`, `cycleInfo.cleaningMode={short, 60}` (operator-left
from pick B). Backup `aws_client.py.bak` is intact next to the patched
file for instant rollback should the next scheduled cycle misbehave.

---

## Session 2026-06-27 — Rapid-pick cascade triggers BUG-20-style stuck-init (T3)

24 h after the T1/T2 session, the operator manually reproduced a rapid
cascade of silent picks via the HA UI to stress the patch under realistic
"changed my mind several times" usage. The patch behaved as designed for
the first five picks and for the second-pick-during-on-plateau case
(picks #5/#6/#7), but a **spontaneous post-pause firmware re-flip to
`pwsState=on` 13.6 s after the last reactive stop** escaped both
guard gates and started a real cleaning cycle. The shadow then went
silent for the entire run, exactly matching the [BUG-20 #98](https://github.com/raouldekezel/orga/dolphin-robot/issues/98)
stuck-init pattern — but triggered by silent picks, not by the firmware
weekly scheduler.

### Context

- **Date**: 2026-06-27
- **Patch**: unchanged from 2026-06-26 (E5a on `v1.0.26b3-raoul.12`).
- **Robot pre-session state** (observer baseline at 12:27:33 UTC):
  `pwsState=holdWeekly, robotState=notConnected, rTurnOnCount=72,
cycleInfo.cleaningMode={all, 120}`. The 09:00 UTC weekly scheduled
  cycle had already completed normally that morning (`rTurnOnCount` 71→72).
- **Source files added in this section**:
  - `04_t3_rapid_pick_cascade_2026-06-27.mqtt.log` — full
    `mydolphin_plus` log (895 lines), PII redacted, covering
    12:20 → 12:55 UTC.
  - `05_power_trace_2026-06-27.txt` — smart-plug
    `sensor.sps_04_nono_puissance` decimated to deltas ≥ 5 W over
    12:27 → 13:00 UTC.
  - `06_observer_output_2026-06-27.txt` — the live observer stream
    captured during the session (47 lines, pre-power-cycle), the form
    the operator narrated against.

### Operator narrative (verbatim)

After picks #1–#7:

> j'ai fais un tas de changements. le robot n'a pas eu l'air de demarrer,
> le nombre de cycles n'a pas augmenté, par contre le cycle time a changé
> vers 60 quand je me suis mis en rapide. […] stop. j'ai pas fais de
> nettoyage j'ai faus des changements rapides et le robot a démarré

A moment later, looking at the Maytronics app:

> l'appli dolphin met un grand triangle "le robot ne repond pas"

### Timeline (CEST = UTC + 2)

All "shadow" rows are deltas extracted by the observer from
`Payload: {...}` lines. "HA→fw" rows are integration writes. "smart-plug"
rows pulled from `05_power_trace_2026-06-27.txt`. Δ is relative to the
previous row in the table.

| #   | t (CEST)            | Δ           | Source         | Event                                                                                                                                                                                                                                                        |
| --- | ------------------- | ----------- | -------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
|     | 14:27:33.910        | —           | shadow         | baseline `pwsState=holdWeekly robotState=notConnected rTOC=72`, `cycleInfo={all, 120}` (post-morning-weekly carry)                                                                                                                                           |
| 1   | 14:27:58.936        | +25 s       | HA → fw        | `Set cleaning mode floor` (silent path)                                                                                                                                                                                                                      |
|     | 14:27:59.993        | +1.057      | HA → fw        | `Set cycle time 120` (BUG-08 chain)                                                                                                                                                                                                                          |
|     | 14:28:01.097        | +2.161      | shadow         | `pwsState=on robotState=init rTOC=72`, `cycleInfo={floor, 120, startTime=…0496}`                                                                                                                                                                             |
|     | 14:28:01.097        | +0          | **patch**      | **BUG-19 reactive stop** → `Set power state pwsState=off`                                                                                                                                                                                                    |
|     | 14:28:04.971        | +3.874      | shadow         | `pwsState=holdWeekly rTOC=72` ✅                                                                                                                                                                                                                             |
| 2   | 14:28:45.723        | +40.752     | HA → fw        | `Set cleaning mode stairs` → cycleTime 150 → on/init at +2.745 → **reactive stop** → holdWeekly at +5.619 ✅                                                                                                                                                 |
| 3   | 14:29:34.838        | +43.496     | HA → fw        | `Set cleaning mode short` → cycleTime 60 → on/init at +2.339 → **reactive stop** → holdWeekly at +6.118 ✅                                                                                                                                                   |
| 4   | 14:30:30.856        | +49.900     | HA → fw        | `Set cleaning mode floor` → cycleTime 120 → on/init at +2.590 → **reactive stop** → holdWeekly at +5.730 ✅                                                                                                                                                  |
| 5   | 14:34:22.333        | +3 m 52     | HA → fw        | `Set cleaning mode all` (after operator's 3 m 52 idle window)                                                                                                                                                                                                |
|     | 14:34:23.435        | +1.102      | HA → fw        | `Set cycle time 120`                                                                                                                                                                                                                                         |
|     | 14:34:25.293        | +2.960      | shadow         | `pwsState=on robotState=init rTOC=72`, `cycleInfo={all, 120, startTime=…0880}` (new startTime)                                                                                                                                                               |
|     | 14:34:25.293        | +0          | **patch**      | **BUG-19 reactive stop #5** → `pwsState=off`                                                                                                                                                                                                                 |
| 6   | 14:34:26.393        | **+1.100**  | HA → fw        | `Set cleaning mode stairs` ⚠️ pick within pick #5's recovery window                                                                                                                                                                                          |
|     | 14:34:27.452        | +1.059      | HA → fw        | `Set cycle time 150`                                                                                                                                                                                                                                         |
|     | 14:34:28.192        | +1.799      | shadow         | `cycleInfo={stairs, 150, startTime=…0880}` (mode/cycleTime accepted, no `systemState` delta — pwsState still `on`)                                                                                                                                           |
|     | —                   | —           | (none)         | **No reactive stop for pick #6** — `_last_reported_pws_state=on`, edge predicate `on→on` is false. Intentional.                                                                                                                                              |
| 7   | 14:34:33.420        | +7.027      | HA → fw        | `Set cleaning mode all` ⚠️ still inside pick #5's `on` plateau                                                                                                                                                                                               |
|     | 14:34:34.519        | +1.099      | HA → fw        | `Set cycle time 120`                                                                                                                                                                                                                                         |
|     | 14:34:34.525        | +1.105      | shadow         | `pwsState=holdWeekly rTOC=72` — late response to pick #5's `pause()`, **9.232 s** after the off publish                                                                                                                                                      |
|     | 14:34:35.003        | +1.583      | shadow         | `cycleInfo={all, 120, startTime=…0880}` (pick #7's mode applied)                                                                                                                                                                                             |
|     | 14:34:35.745        | +2.325      | shadow         | `pwsState=on robotState=init rTOC=72`                                                                                                                                                                                                                        |
|     | 14:34:35.746        | +0.001      | **patch**      | **BUG-19 reactive stop #6** → `pwsState=off` (for pick #7, edge held: holdWeekly → on)                                                                                                                                                                       |
| ⚠   | **14:34:49.376**    | **+13.630** | **shadow**     | **`pwsState=on robotState=notConnected rTOC=72`** — fw spontaneously re-asserts `on` with no fresh integration write                                                                                                                                         |
|     | —                   | —           | (none)         | **No reactive stop.** Edge predicate: `_last_reported_pws_state=on`, incoming `on` → `became_on=False`. AND guard window from pick #7 had expired at 14:34:45.420 (12 s TTL), so even if edge had fired, `_silent_guard_active()` would have returned False. |
|     | 14:35:03.743        | +14.367     | smart-plug     | **3.6 → 81.67 W jump — motor engages** (cycle physically running)                                                                                                                                                                                            |
|     | 14:35:09 → 14:46:24 | (~11 min)   | smart-plug     | sustained 15–135 W oscillations (S2000 brush + drive cycle active)                                                                                                                                                                                           |
|     | (entire run)        | —           | shadow         | **silent**: no further `pwsState`/`robotState`/`rTurnOnCount` deltas after 14:34:49 (same invariant as BUG-20 #98)                                                                                                                                           |
|     | (entire run)        | —           | Maytronics app | "le robot ne répond pas" (cloud → robot link lost)                                                                                                                                                                                                           |
|     | 14:46:28.205        | +12 min     | HA → switch    | `switch.turn_off switch.sps_04_nono` (operator-instructed recovery)                                                                                                                                                                                          |
|     | 14:46:57.657        | +29.452     | smart-plug     | 135.75 → **0.00 W** (PWS unpowered)                                                                                                                                                                                                                          |
|     | 14:46:58.264        | +0.607      | HA → switch    | `switch.turn_on switch.sps_04_nono` (30 s off window achieved)                                                                                                                                                                                               |
|     | 14:47:11.909        | +13.645     | shadow         | **Fresh shadow republish**: `pwsState=holdWeekly robotState=notConnected rTOC=72`, `cycleInfo={stairs, 150, startTime=None}`                                                                                                                                 |
|     | 14:47:24.306        | +12.397     | shadow         | `cycleInfo={stairs, 150, startTime=…0880}` — pre-power-cycle startTime restored (BUG-18 persistence semantics)                                                                                                                                               |

### Findings

#### F6 — E5a is not sufficient on rapid cascades

The patch's reactive hook depends on **observing** a `pwsState=on`
transition during an armed guard window. Both gates failed at 14:34:49:

- **Edge predicate failed** because the firmware's reported `pwsState`
  was already `on` (set by pick #7's flip at 14:34:35.745 and never
  cleared — the post-pause `holdWeekly` from pick #5 arrived at
  14:34:34.519 _before_ pick #7's `on`, so the last observed value was
  permanently `on` through the rest of the session).
- **Guard window failed** because pick #7's `_silent_guard_deadline`
  expired at 14:34:45.420 (T0 + 12 s), 4 seconds before the spontaneous
  re-`on`. Even an edge-true would have been suppressed by
  `_silent_guard_active()`.

The spontaneous re-flip itself is firmware-internal — no write of ours
correlates with it (verified in `04_t3_rapid_pick_cascade_2026-06-27.mqtt.log`).
A plausible reading: the firmware's response queue had backed up under
the rapid mode/cycleTime/off churn (visible from the 9.2 s pick-#5
pause-response latency vs. the 3–6 s baseline observed in T1/T2/picks-1–4),
and emitted a stale or recovery `pwsState=on` once the queue drained.

This is a **new failure mode of the patch design**, not a manifestation
of BUG-19 #96 proper. BUG-19 was about a same-side race (our second
pause losing to our first set's `on` adoption). T3's failure is a
delayed firmware-side event that arrives _after_ the patch's whole
state machine has reset.

#### F7 — Recovery via PWS power-cycle is clean

`switch.sps_04_nono` cut the PWS for 30 s. The shadow republished at
14:47:11.909 (13.6 s after power-on) with `pwsState=holdWeekly,
rTurnOnCount=72`, confirming:

- The fw did not commit the stuck-init cycle (`rTurnOnCount` stays
  at 72 across the whole event).
- Persistence preserved `cycleInfo.cleaningMode={stairs, 150}` — pick #6's
  values, **not** pick #7's `{all, 120}` that the integration had last
  written. Echoes BUG-18 (#88) reboot-persistence semantics where the
  last-applied `cycleInfo` survives the reboot.
- The post-reconnect snapshot at 14:47:24 restored `cycleStartTime=…0880`
  — the abandoned cycle's start stamp persists too.

#### F8 — Cycle counter remains the only reliable commit signal

Through the entire 12 min of motor activity, `rTurnOnCount` did not
increment. The integration's `sensor.nono_2_nombre_de_cycles` correctly
stays at 72 (its last_changed is from 09:06 UTC, before the session).
`sensor.nono_2_historique` registers no row for the T3 run either: the
custom command-line sensor's session detector watches
`vacuum.nono_2 ∈ {cleaning, returning}` continuous spans, and while
`vacuum.nono_2` did briefly state `cleaning` (at 12:34:32 UTC, ~3 s
before pick #7's reactive stop), the brief flicker did not form a
detectable session interval.

Consequence for any user-facing dashboard: a stuck-init run is
invisible from inside the integration. Only the upstream smart-plug
proves the cycle happened. Mirrors the F4 invariant from BUG-20 #98.

### Open questions for the upstream-bound PR (expanded)

- **Should the guard window be longer than 12 s?** Pick #5's pause
  response latency was 9.2 s vs. 3–6 s baseline. A guard window large
  enough to cover that latency (say 20–30 s) would still not have caught
  the +13.6 s spontaneous re-`on` of T3 — and stretching it indefinitely
  reintroduces the "stale guard catches an unrelated `on`" failure mode
  the original 12 s was meant to bound. **Likely not the right axis.**
- **Should the edge predicate use `desired.systemState.pwsState=off`
  acknowledgement as the reset signal** (instead of `reported.pwsState !=
on`)? Would correctly re-arm after our pause acks, regardless of
  whether the firmware's `holdWeekly` reply has arrived. Worth
  prototyping in the upstream PR.
- **Is rapid-cascade detection** (e.g. ≥ N silent picks within M seconds
  → publish an extra defensive `pwsState=off` on a long timer) **a
  worthwhile defense in depth?** Adds another race surface; probably
  not.
- **Should the integration surface stuck-init detection?** A small
  monitor inside the coordinator that triggers an alert when (a)
  smart-plug power is high (or otherwise inferable) while (b)
  `pwsState=on` and (c) `rTurnOnCount` hasn't incremented for > N
  seconds would catch both BUG-20 #98 and T3 from inside the
  integration. Cross-cuts entity boundaries (HA sensors of arbitrary
  smart plugs are not the integration's data) — feasibility caveat.

### Closing state (2026-06-27 T3 session)

PWS power-cycle recovered the robot cleanly. Patch left in place,
backup `aws_client.py.bak` still intact. `vacuum.nono_2 = docked`,
`pwsState=holdWeekly`, `rTurnOnCount=72`, `cycleInfo={stairs, 150}`
(persisted from pick #6). No commit recorded for the ~12 min stuck-init
run; only the smart-plug remembers it.

---

## Session 2026-06-27 (later — T4) — Minimal reproducer (2 picks) + operator-confirmed root-cause hypothesis

About 2 h after T3 recovery, the operator ran a deliberately scoped
follow-up to isolate the trigger: rapid picks but **only between modes
of similar cycleTime** (`all`, `floor`, `stairs`, `water`, `ultra` —
all 120–150 min; `short` and `pickup` excluded). The hypothesis under
test: was T3's stuck-init caused by the _variety_ of cycleTimes in the
cascade (60/120/150/60/120) or by the _cadence_ itself?

**Answer: cadence — 2 picks are enough.** The failure reproduced on
the second pick, with both picks landing on long-cycle modes.

The operator then phrased the root-cause hypothesis in plain terms:

> c'est quand on envoie un nouveau cycle alors que l'ancien n'est pas
> encore complètement demarré

— **a new silent set is fired while the previous one's start → pause
mini-cycle has not been fully observed by the firmware.** The timing
evidence in T3 and T4 supports this exactly.

### Source files added in this section

- `07_t4_pickN_during_pause_2026-06-27.mqtt.log` — full
  `mydolphin_plus` log slice (452 lines), PII redacted, covering
  16:50 → 17:03 CEST (includes both picks, stuck-init, power-cycle,
  reconnect).
- `08_t4_observer_output_2026-06-27.txt` — observer stream (31 lines)
  covering the same window plus the 16:37 MQTT hangup and 16:38 reco
  preceding T4.
- `09_t4_power_trace_2026-06-27.txt` — smart-plug power decimated to
  deltas ≥ 5 W over 16:52 → 17:10 CEST.

### Timeline (CEST = UTC + 2)

Pre-T4 baseline (post-T3-recovery + an MQTT hangup at 16:37 cleanly
recovered at 16:38). The integration's view of the shadow at the start
of T4 was `pwsState=holdWeekly, rTurnOnCount=73, cycleInfo={stairs,
150}` (persisted from T3 pick #6). **`rTurnOnCount=73` is significant:
it had been 72 at T3 recovery; the in-flight cycle that the operator's
power-cycle interrupted at T3 was _partially_ counted by the firmware
on reboot.**

| #   | t (CEST)     | Δ          | Source         | Event                                                                                                                                                                                                                                                                                     |
| --- | ------------ | ---------- | -------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | 16:53:46.892 | —          | HA → fw        | `Set cleaning mode all` (silent path)                                                                                                                                                                                                                                                     |
|     | 16:53:47.991 | +1.099     | HA → fw        | `Set cycle time 120` (BUG-08 chain)                                                                                                                                                                                                                                                       |
|     | 16:53:48.090 | +0.099     | shadow         | `cycleInfo={all, 150, startTime=…9200}` — fw echo, cycleTime still 150 (stairs persistence) — pre-BUG-08 state                                                                                                                                                                            |
|     | 16:53:48.792 | +0.702     | shadow         | `pwsState=holdWeekly rTOC=73`, `cycleInfo={all, 120, startTime=…9200}` — BUG-08 chain landed                                                                                                                                                                                              |
|     | 16:53:49.548 | +0.756     | shadow         | `pwsState=on robotState=init rTOC=73`                                                                                                                                                                                                                                                     |
|     | 16:53:49.549 | +0.001     | **patch**      | **BUG-19 reactive stop** → `Set power state pwsState=off`                                                                                                                                                                                                                                 |
| 2   | 16:53:50.542 | **+0.993** | HA → fw        | `Set cleaning mode floor` — **fired 0.99 s after pick #1's pause publish, before the firmware has had any chance to acknowledge the pause**                                                                                                                                               |
|     | 16:53:51.611 | +1.069     | HA → fw        | `Set cycle time 120`                                                                                                                                                                                                                                                                      |
|     | 16:53:52.515 | +0.904     | shadow         | `cycleInfo={floor, 120, startTime=…9200}` (fw applies pick #2's mode)                                                                                                                                                                                                                     |
|     | 16:54:00.006 | +7.491     | shadow         | `pwsState=holdWeekly rTOC=73` — **late post-pause response to pick #1, 10.46 s after the pause publish (vs. 3–6 s baseline)**                                                                                                                                                             |
|     | 16:54:04.858 | +4.852     | shadow         | `pwsState=on robotState=init rTOC=73` — fw flip for pick #2, **14.32 s after pick #2 mode write** (vs. 2.79 s baseline)                                                                                                                                                                   |
|     | —            | —          | (none)         | **No reactive stop.** `_silent_guard_active()` returns False: pick #2's guard expired at 16:54:02.542 — **2.32 s before the actual `on`.** Edge predicate would have fired (holdWeekly→on at 16:54:00.006 set `_last_reported_pws_state=holdWeekly` first), but the guard gate killed it. |
|     | 16:54:13.729 | +8.871     | shadow         | `pwsState=on robotState=notConnected rTOC=73` — robot enters the pool / cycle starting physically                                                                                                                                                                                         |
|     | (entire run) | —          | shadow         | **silent** — no further deltas through power-cycle                                                                                                                                                                                                                                        |
|     | (entire run) | —          | Maytronics app | "déconnecté du robot" (same as T3)                                                                                                                                                                                                                                                        |
|     | 17:01:24     | +6 min 50  | smart-plug     | sustained ≥ 20 W (cycle physically running)                                                                                                                                                                                                                                               |
|     | 17:01:29.670 | +5 s       | HA → switch    | `switch.turn_off switch.sps_04_nono` (operator-instructed recovery)                                                                                                                                                                                                                       |
|     | 17:01:59.750 | +30.080    | HA → switch    | `switch.turn_on switch.sps_04_nono`                                                                                                                                                                                                                                                       |
|     | 17:02:13.481 | +13.731    | shadow         | **Fresh shadow republish: `pwsState=holdWeekly robotState=notConnected rTurnOnCount=72`** — **`rTOC` decremented 73 → 72**, the in-flight stuck-init cycle is **rolled back** by the reboot (the increment was not persisted before completion).                                          |
|     | 17:02:25.015 | +11.534    | shadow         | `cycleInfo={stairs, 150, startTime=…9200}` — same persisted startTime as before the T4 attempt; pick #2's `{floor, 120}` did **not** survive the reboot, the pre-T4 `{stairs, 150}` resurfaced                                                                                            |

### Findings

#### F9 — Minimal reproducer (CLOSES T3's open question on cadence vs. cycleTime variety)

T3 raised the question of whether the failure mode was driven by the
mix of cycleTimes or just by the cadence. **T4 settles it: 2 silent
picks with ≤ 1 s gap, both on long-cycle modes, reproduce the
stuck-init outcome identically.** Variety of cycleTimes is not
required. The trigger is **pick #2 fired before the firmware has
acknowledged pick #1's reactive `pause()`**.

Concretely: when the integration publishes `set_cleaning_mode_silent(X)`,
its hidden contract is a three-step mini-cycle the firmware must
complete in order:

1. firmware reports `pwsState=on` (the start the patch is racing to
   stop);
2. integration publishes reactive `pwsState=off`;
3. firmware reports `pwsState=holdWeekly` (acknowledges the pause).

If a second silent set arrives **between step 2 and step 3**, the
firmware's response queue is forced to merge two `on/off` mini-cycles,
the post-pause `holdWeekly` arrives 7–11 s late (vs. 3–6 s baseline),
the `on` for the second pick arrives 12–15 s late (vs. 2–3 s baseline)
— well after the second pick's 12 s `_silent_guard_deadline` — and the
reactive hook misses the actual cycle start.

The 12 s guard TTL was sized to cover the _normal_ on-latency of an
isolated silent set (T1: 2.79 s, T2 picks A/B: 2.89/3.89 s, T4 pick #1:
2.66 s). It does not cover the _degraded_ on-latency under back-to-back
contention.

#### F10 — `rTurnOnCount` is not crash-consistent across PWS reboots

T3 power-cycled at `rTOC=72` mid-stuck-init; recovery republished with
`rTOC=72`. T4 started at `rTOC=73` (so the firmware _did_ increment
between T3's recovery and T4's start — presumably during the BUG-20-like
12 min stuck cycle of T3, the increment happened at cycle start). T4
power-cycled at `rTOC=73` mid-stuck-init; recovery republished with
**`rTOC=72`**. So:

- The increment is staged when the firmware enters the cycle (it's why
  T3's recovery saw 72 — the cycle had been counted as 73 in volatile
  memory by the time the operator power-cycled).
- It is _not_ persisted to non-volatile storage until the cycle
  completes (it's why T4's reboot rolled `rTOC` back to 72 — the T3
  cycle had been the last to write to NVRAM, the T4 in-flight increment
  was wiped).
- For the integration, this means `rTurnOnCount` deltas observed in
  real time are not reliable as "cycle started" markers; only deltas
  observed **after** a `cycleStartTimeUTC` advance + a normal cycle
  completion can be trusted as durable commits.
- F8's claim ("only the smart-plug remembers a stuck-init run")
  strengthens: even the in-memory `rTOC` increment is lost on
  power-cycle recovery, leaving zero durable evidence inside the
  integration / shadow path that the run happened.

#### F11 — Operator-formulated fix direction (BUG-13 deepening)

Root cause restated from the operator's hypothesis:
**`set_cleaning_mode_silent` is not safe to call while the previous
`set_cleaning_mode_silent`'s mini-cycle is in flight.**

Fix direction candidate for the upstream-bound PR — serialize the
silent-set entry point on the **same** state that the reactive hook
already tracks:

```python
def set_cleaning_mode_silent(self, clean_mode: CleanModes) -> None:
    if self._silent_guard_active():
        # The previous silent set's mini-cycle hasn't observed its
        # post-pause holdWeekly yet. Queue or drop the call instead of
        # firing a fresh mode-write that the firmware can't sequence.
        _LOGGER.warning(
            "Silent set %s coalesced: previous silent set still in flight",
            clean_mode,
        )
        return
    ...
```

The "queue vs. drop" decision is a user-experience call: a drop is
safer (no risk of firing the queued pick after the operator changed
their mind a third time), but loses the operator's intent silently. A
short queue with a timeout would split the difference. Either way
needs its own validation session before merge.

A pre-condition for either path: **the guard window must be extended
to cover the worst observed post-pause holdWeekly latency under
contention** (≥ 12 s), so `_silent_guard_active()` correctly stays True
through the danger zone. T3 saw 9.2 s, T4 saw 10.5 s — a 15 s TTL
covers observed data; 20 s is a safer cap.

### Closing state (2026-06-27 T4 session)

PWS power-cycle recovered the robot cleanly at 17:02:13. `rTurnOnCount`
back to 72, `pwsState=holdWeekly`, `cycleInfo={stairs, 150}`. The
robot has been re-validated docked and idle. Patch still in place.
`aws_client.py.bak` still intact.

This session **closes** the cadence-vs-cycleTime open question from
T3 and **opens** F11 as the recommended fix direction for the
upstream-bound code PR.
