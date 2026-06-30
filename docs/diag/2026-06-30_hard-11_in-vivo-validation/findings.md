# HARD-11 — in-vivo validation of v1.0.26b3-raoul.17

## TL;DR

End-to-end validation of [PR #110](https://github.com/raouldekezel/dolphin-robot/pull/110) (HARD-11 v1.3, "honest-core") on the live robot, exercising the optimistic vacuum overlay, the start-serialization guard, and the rest-edge / TTL clear paths.

- **Optimistic activity flip ✅** — `vacuum.activity` swaps `docked → cleaning` instantly on every Run click; HA more-info card swaps Start → Pause/Stop without any perceptible gap.
- **Rest-edge `holdweekly` ✅** — fires reliably 5/7 cycles, pause-ack latency 9.9–10.6 s (matches v1.2 spec band).
- **TTL fallback ✅** — 2/7 cycles (rapid Run→Stop within ~5–6 s) saw the firmware suppress `pwsState=on` entirely; no rest-edge possible; TTL + next coordinator-tick cleared the stuck overlay in **37–44 s** (vs. the overlay TTL's 120 s), confirming v1.1's tied-clear fix bounds the stuck-overlay case.
- **No BUG-19 / BUG-20 cascade ✅** — `rTurnOnCount` only bumped when the firmware reached `scanning` (1× per session: 80 → 81 for the stale-desired boot cycle, 81 → 82 for the 2-min validation cycle). All rapid Run/Stop cycles left the counter flat: the firmware self-protected, the start-serialization guard never had to refuse.
- **Start echo latency ≪ initial spec** — 2.9–13.1 s observed (= time to `pwsState=on`), not the 60 s figure carried forward from the FEAT-01 session (that was time-to-`scanning`). Optimistic overlay therefore has much less to mask in real life; the origin-moved clear typically fires within a few seconds.
- **No `Start refused` / `Pickup refused` warnings** — in this session the rest-edge always cleared the guard fast enough that no subsequent click landed inside the guard window.
- **Behaviour to track separately**: the firmware honors a stale `desired.cleaningMode.mode` left in the AWS shadow across a robot power cycle — the robot booted, picked up the 7-minute-old desired delta, started a `short` cycle on its own, and bumped `rTurnOnCount` 80 → 81. Not HARD-11 territory.

## Context

- **Date:** 2026-06-30
- **Tag installed via HACS:** `v1.0.26b3-raoul.17` (PR #110 head at `55cdc15`, includes v1.1 + v1.2 + v1.3 of HARD-11). Runtime verified: `_PAUSE_GUARD_TTL_S == 15.0`, `_PAUSE_ACK_REST_STATES == {holddelay, holdweekly, off}`.
- **Robot:** Maytronics Dolphin S2000 ("Nono 2"). Firmware reports `robotType="S4"`. Robot was powered off at session start (`switch.sps_04_nono` off), then turned on at 20:40:31 local — the firmware booted into the stale shadow desired (`mode=short` from a pre-session HA click) and ran a cycle on its own.
- **HA:** 2026.1.3 (container `hass` on intel-nuc, `network_mode: host`).
- **Capture:**
  - `docker logs hass --timestamps` filtered on `mydolphin|Set (cleaning|power|cycle)|HARD-11|refused|Pause vacuum|Start vacuum|System status recalculated|pwsState|rTurnOnCount|robotState`.
  - Coordinator logger temporarily promoted to DEBUG via `logger.set_level` so the HARD-11 `_LOGGER.debug(...)` traces (TTL expiry, rest-edge, origin-moved) appear in line.
  - HA REST API (`/api/states`) snapshots at observation points.
  - HA recorder (`/api/history/period`) for the synthetic-state and statut transition cross-check.

All times are local (Europe/Brussels, UTC+2). UTC timestamps in `docker logs` are 2 h earlier.

## Cycle catalog

Seven cycles total: one firmware-initiated stale-desired boot cycle + six operator-initiated cycles. Local clock format.

| #   | Trigger                                   | Run          | Stop         | Δ Run-Stop  | Start echo (to `pwsState=on`)   | Stop ack | Guard clear path       | rTurnOnCount |
| --- | ----------------------------------------- | ------------ | ------------ | ----------- | ------------------------------- | -------- | ---------------------- | ------------ |
| 0   | Firmware boot (stale shadow)              | ~20:40:54    | 20:42:44.138 | ~110 s      | n/a (boot)                      | 10.6 s   | rest-edge `holdweekly` | **80 → 81**  |
| 1   | HA Run                                    | 20:46:31.787 | 20:47:00.479 | 28.7 s      | 2.9 s                           | 10.0 s   | rest-edge `holdweekly` | (no change)  |
| 2   | HA Run                                    | 20:50:44.735 | 20:50:50.114 | **5.4 s**   | 5.9 s (post-Stop)               | 9.9 s    | rest-edge `holdweekly` | (no change)  |
| 3   | HA Run (mode `all`)                       | 20:51:17.767 | 20:51:41.662 | 23.9 s      | 8.3 s                           | 10.0 s   | rest-edge `holdweekly` | (no change)  |
| 4   | HA Run (mode `all`)                       | 20:52:12.796 | 20:52:18.217 | **5.4 s**   | **never (firmware suppressed)** | —        | **TTL** (+44.4 s)      | (no change)  |
| 5   | HA Run                                    | 20:54:09.683 | 20:54:15.717 | **6.0 s**   | **never (firmware suppressed)** | —        | **TTL** (+36.9 s)      | (no change)  |
| 6   | HA Run (mode `all`) — 2-min nominal cycle | 21:00:29.661 | 21:03:56.361 | **206.7 s** | 13.1 s                          | 10.0 s   | rest-edge `holdweekly` | **81 → 82**  |

Three operationally distinct shapes are exercised:

- **Cycles 0, 1, 3, 6** — nominal: firmware echoes `pwsState=on`, optimistic clears via origin-moved; on Stop the rest-edge `holdweekly` fires within ~10 s. Cycle 6 ran long enough (~3 min) to leave `init` and reach `scanning`, bumping `rTurnOnCount`.
- **Cycle 2** — Stop landed 5.4 s after Run; firmware still echoed `pwsState=on` 0.5 s _after_ the pause (5.9 s after Run), so origin-moved cleared the overlay normally and the pause-ack also caught the entering-`holdweekly` edge a few seconds later. No counter bump (`init` never reached `scanning`).
- **Cycles 4, 5** — Stop landed 5.4–6.0 s after Run; firmware suppressed `pwsState=on` entirely (no "firmware moved docked → cleaning" log line, no shadow rTurnOnCount change). With `pwsState` never leaving `off` and `calculated_state` never leaving `holdweekly`, both the origin-moved check and the rest-edge predicate stayed false by construction; the only clear path was the **pause-guard TTL** in `_reconcile_pause_guard`, which fired on the first coordinator tick past TTL (so `cap + tick_offset` ≈ 37 / 44 s). This is exactly the scenario the v1.1 tied-clear was added to bound.

## Per-test detail

### Cycle 0 — stale-desired boot

At session start (20:32:30) the robot was powered off (`switch.sps_04_nono` off, shadow `pwsState=holdWeekly`, `robotState=notConnected`, `isConnected.connected=false`, `rTurnOnCount=80`). The shadow's `desired.cleaningMode.mode = short` had been left by an HA click 7 min earlier (20:33:18) before the plug was cut — the integration's write went through to AWS but the firmware was offline and never picked it up.

At 20:40:31 the operator turned the plug back on (HA `switch.turn_on`). The firmware booted, read the shadow delta `desired.mode = short` vs. `reported.mode = all`, and treated it as a start command. By 20:40:54 the integration logged `System status recalculated, Calculated State: init, Main Unit State: on` — `pwsState` had flipped to `on` and `rTurnOnCount` had advanced to 81.

At 20:42:44.138 the operator clicked Stop. Wire-level:

```
20:42:44.138 DEBUG Pause vacuum, State: cleaning
20:42:44.138 INFO  Set power state, Desired: {'systemState': {'pwsState': 'off'}}
20:42:54.703 DEBUG HARD-11 — firmware moved cleaning → docked, clearing overlay
20:42:54.703 DEBUG HARD-11 — pause guard cleared (rest edge (holdweekly)); dropping overlay too
```

Both clears (origin-moved + rest-edge) fired on the same tick at +10.6 s. Idempotent — second clear was a no-op.

**Takeaway not covered by HARD-11**: the firmware honors a stale `desired` left in the shadow when it reconnects. The operator did not click Start; the cycle was a side-effect of the prior power loss. Worth a separate ticket if it isn't already tracked, since it interferes with diagnostic sessions that turn the plug off as a clean state.

### Cycles 1–5 — operator rapid Run/Stop

The operator issued five Run/Stop pairs over 8 minutes, with intentionally varied delays. Aggregated wire pattern per cycle:

- **Start**: `Start vacuum` (DEBUG) → `Set cleaning mode, Desired: {'cleaningMode': {'mode': X}}` (INFO). Mode is the staged `_desired_clean_mode` (defaults to whatever was last echoed by reported, possibly via the foreign-reconcile path).
- **Optimistic flip**: not log-visible directly (v1 has no debug line on arm), but its effect is observable in the recorder: `sensor.nono_2_statut` transitions to `startingpending` for the duration of the overlay window.
- **First firmware echo**: when `pwsState=on` lands, `_set_system_status_details` logs `System status recalculated` and the next reconcile logs `HARD-11 — firmware moved docked → cleaning, clearing overlay` (origin-moved path).
- **Stop**: `Pause vacuum, State: cleaning` (DEBUG) → `Set power state, Desired: {'systemState': {'pwsState': 'off'}}` (INFO).
- **Stop ack** (nominal path): on entering `holdweekly`, the reconcile logs `HARD-11 — firmware moved cleaning → docked, clearing overlay` (origin-moved on the way down) AND `HARD-11 — pause guard cleared (rest edge (holdweekly)); dropping overlay too` (rest-edge predicate fired with `prev=cleaning, current=holdweekly`).
- **Stop ack** (suppressed-start path, cycles 4/5): no `firmware moved` line, no rest-edge — instead, after the next coordinator tick past TTL, `HARD-11 — pause guard cleared (ttl); dropping overlay too` fires alone (the tied-clear includes the optimistic overlay).

The five operator cycles also exercise mode-change between cycles (Cycle 3 picks `all`, Cycle 4 stays `all`, Cycle 6 picks `all`). The staged mode is set by the `select.nono_2_mode_de_nettoyage` HA UI between Stop of one cycle and Run of the next; this is the BUG-13 write-on-commit path, validated separately in the 2026-06-21 / 2026-06-28 sessions and unchanged here.

### Cycle 6 — nominal 2-min cycle

```
21:00:29.661 DEBUG Start vacuum
21:00:29.661 INFO  Set cleaning mode, Desired: {'cleaningMode': {'mode': 'all'}}
21:00:42.788 DEBUG System status recalculated, Calculated State: init, Main Unit State: on, Robot State: …
21:00:42.788 DEBUG HARD-11 — firmware moved docked → cleaning, clearing overlay
21:01:22.699 DEBUG System status recalculated, Calculated State: init, Main Unit State: on, Robot State: …  (still init, 73 s into the cycle)
21:01:56.576 DEBUG System status recalculated, Calculated State: cleaning, Main Unit State: on, Robot State: scanning  (entered scanning)
21:03:56.361 DEBUG Pause vacuum, State: cleaning
21:03:56.361 INFO  Set power state, Desired: {'systemState': {'pwsState': 'off'}}
21:04:06.365 DEBUG HARD-11 — firmware moved cleaning → docked, clearing overlay
21:04:06.365 DEBUG HARD-11 — pause guard cleared (rest edge (holdweekly)); dropping overlay too
```

Timings:

- **Run → `pwsState=on` echo: 13.1 s.** The optimistic overlay masked these 13.1 s; the operator saw `vacuum.activity = cleaning` and chip = `Démarrage…` immediately on click.
- **`pwsState=on` → entering `scanning`: 73.8 s.** The robot spent the entire `init` phase (= "Vérification de l'environnement" / "Analyse" in FR) before motors engaged. This `init` duration is consistent with prior FEAT-01 / BUG-13 sessions.
- **Stop click → entering `holdweekly`: 10.0 s.** Same band as the other rest-edge clears.

`rTurnOnCount` advanced 81 → 82 — the cycle was long enough to commit a real counter bump.

The full chip progression observed via the synthetic-state recorder (`sensor.nono_2_etat_synthetique`):

```
18:55:00  Programmé      (holdweekly, idle)
19:00:29  Démarrage…     (startingpending, optimistic overlay armed)
19:00:42  Analyse        (init, firmware acked start)
19:01:56  Nettoyage      (cleaning, robotState=scanning)
19:03:56  Arrêt…         (pausingpending, optimistic overlay armed for Stop)
19:04:06  Programmé      (holdweekly, rest-edge cleared)
```

The two optimistic states (`startingpending`, `pausingpending`) lasted ~13 s and ~10 s respectively — short windows, but consistent with the design intent ("first transient feedback while the firmware is silent").

## Findings

### F1 — Rest-edge fires reliably for `holdweekly` on this hardware (5/5 nominal cycles)

Every nominal Stop in this session (Cycles 0, 1, 2, 3, 6) cleared the guard via the rest-edge predicate, with the trace line `HARD-11 — pause guard cleared (rest edge (holdweekly)); dropping overlay too`. The S2000 settles to `holdweekly` after a pause (consistent with the weekly schedule being active on this install). Pause-ack band: **9.9 – 10.6 s** across all five — tight enough that v1.2's choice of `_PAUSE_GUARD_TTL_S = 15.0` keeps the block protection inside the empirical worst case (10.5 s in E5a T4) with margin.

The `HOLD_DELAY` / `OFF` paths of `_PAUSE_ACK_REST_STATES` were not exercised here (no robot configured without an active weekly schedule was available in-session). They are unit-tested but remain empirically un-tested in vivo on a real robot in those configurations.

### F2 — TTL fallback bounds the suppressed-start case at ~37–44 s (vs. 120 s in v1.0)

Cycles 4 and 5: Stop landed 5–6 s after Run, the firmware never echoed `pwsState=on`, the shadow stayed at `pwsState=off` / `calculated_state=holdweekly` from the operator's perspective. By construction:

- Origin-moved cannot fire (real `vacuum_state` never left `DOCKED`).
- Rest-edge cannot fire (current is `holdweekly`, prev was `holdweekly`).
- Only path remaining: pause-guard TTL → tied overlay clear (v1.1's fix).

Observed clear delays from Stop to `pause guard cleared (ttl)`: **44.4 s (Cycle 4)** and **36.9 s (Cycle 5)**. Both well within the design budget of `TTL + tick_interval` (15 s + up to ~30 s). For comparison, the v1.0 (pre-tied-clear) overlay would have stuck for the full overlay TTL of 120 s in the same scenario.

The operator-visible UX during these windows: `vacuum.activity = cleaning` + `sensor.nono_2_statut = pausingpending` (chip = "Arrêt…"). Not ideal, but bounded, finite, and honest given the firmware genuinely did _not_ acknowledge anything.

### F3 — No BUG-19 / BUG-20 cascade observed across 5 rapid Run/Stop cycles

`rTurnOnCount` evolution across the entire session: 80 (initial) → 81 (Cycle 0 stale-desired boot) → 82 (Cycle 6 long nominal). All five intermediate operator cycles (1–5) left the counter unchanged.

This is the firmware self-protecting: short cycles where the robot never leaves `init` do not commit a counter bump. None of the rapid Run/Stop patterns triggered the `rTurnOnCount → 255` sentinel or the multi-hour shadow silence that BUG-20 documented. The start-serialization guard never needed to refuse a Run (no `Start refused` warning in any log slice), because the rest-edge clear always landed before the next operator click.

Read alongside the E5a / BUG-19 sessions, this is consistent: the cascade required either a _very_ tight intra-pick interval (~1 s in E5a T4) or a foreign trigger (scheduled cycle landing on a contaminated state). Manual clicks at the operator's tempo never reached either trigger pattern in this session.

### F4 — Start echo latency observed ≪ initial spec band

Measured time from `Set cleaning mode, …` → first `firmware moved docked → cleaning` (= `pwsState=on` echo) across the five visible cases:

| Cycle | Start echo |
| ----- | ---------- |
| 1     | 2.9 s      |
| 2     | 5.9 s      |
| 3     | 8.3 s      |
| 6     | 13.1 s     |

Range **2.9 – 13.1 s**, all well below the "60 s" figure carried forward from earlier sessions. The 60 s figure was time-to-`robotState=scanning` (i.e. time from start to the `rTurnOnCount` bump), not time-to-`pwsState=on`. HARD-11's optimistic overlay only needs to mask the `pwsState=on` gap, which is much shorter than initially designed for.

Implication: the overlay's 120 s TTL is generous. The origin-moved clear typically fires within 3–13 s. The TTL only matters in the suppressed-start case (F2), and there it's bounded by the pause-guard TTL (15 s), not the overlay TTL.

### F5 — Stale shadow `desired` survives robot power cycle (out of scope, worth tracking)

The Cycle 0 boot cycle was triggered by a stale `desired.cleaningMode.mode = short` written 7 min before the robot was powered off. When the firmware came back online, it processed the shadow delta and started the cycle on its own — no operator click was involved.

This is not an HARD-11 bug, but it interferes with diagnostic sessions that use the smart-plug as a clean-state primitive (the standard "power-cycle to reset" workaround). Suggested follow-up: add the integration-side write of `desired:null` on AWS disconnect, or document the expectation. Either way, separate ticket.

### F6 — Cosmetic: `init` rendered grey in the operator's chip dashboard

Outside HARD-11 scope, but discovered via this session: the operator's `sensor.nono_2_etat_synthetique` template mapped `init` to no color, falling through to `grey`. The 73 s `init` phase therefore looked visually static (grey → grey) between the optimistic-flip blue and the cleaning blue.

Adjustment made out-of-band (added `init` to the blue cluster) so the chip stays a consistent color through `Démarrage… → Analyse → Nettoyage`. Cosmetic only; not in the integration codebase.

## What HARD-11 v1.3 buys

- **Start UX (every cycle)**: instant button swap (Start → Pause/Stop), chip = "Démarrage…" until the firmware acknowledges (typically ≤ 13 s, sometimes ≤ 3 s). Before HARD-11, the operator clicked Start and saw nothing change until the echo landed seconds later.
- **Stop UX (nominal case, ~10 s rest-edge)**: chip = "Arrêt…" until firmware echoes `holdweekly`, then revert to "Programmé". Tight feedback loop.
- **Stop UX (worst case, rapid Run+Stop, firmware suppresses)**: chip = "Arrêt…" for ~37–44 s, then silent revert. Bounded by v1.1's tied-clear; without it, this case would lie for 120 s. Still not great visually, but design-honest given the firmware acknowledged nothing.
- **No cascade triggered**: 5 rapid Run/Stop pairs left `rTurnOnCount` flat. The guard mechanism, while never needed to actively refuse a Run in this session, did not fail-open either.

## Raw artifacts in this directory

| File                    | Content                                                                                                    |
| ----------------------- | ---------------------------------------------------------------------------------------------------------- | ----- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `findings.md`           | This document.                                                                                             |
| `cycles.tsv`            | Per-cycle timing table from the docker-logs slice (one row per cycle, columns matching the catalog above). |
| `coordinator_trace.log` | Filtered docker-logs slice covering 18:33 UTC → 19:05 UTC (= 20:33 → 21:05 local), filter `Set (cleaning   | power | cycle)\|HARD-11\|refused\|Pause vacuum\|Start vacuum\|System status recalculated`. Redactions: motor unit serial `N4720KMV…`→`REDACTED-MUSN`, wifi SSID → `REDACTED-WIFI-SSID`. |
