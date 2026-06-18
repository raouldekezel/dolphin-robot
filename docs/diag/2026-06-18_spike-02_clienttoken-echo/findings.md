# SPIKE-02 D4 — `clientToken` echo confirmation on the Maytronics deployment

## TL;DR

AWS echoes our injected `clientToken` unchanged on `/update/accepted` for
this Maytronics thing (3/3 our writes echoed, round-trip 86–111 ms);
neither the Maytronics app nor the device stamps a token (0/9 foreign
events carry one in the captured window); the `token ∈ our in-flight
set` discriminator separates the two cleanly. **The `clientToken`
pattern surveyed in D1 is viable on this deployment.**

## Context

- **Date:** 2026-06-18
- **Robot:** Maytronics Dolphin S2000 (Nono 2) — firmware reports
  `robotType:"S4"`, `pwsSwVersion:"11.0004"`, `muSwVersion:"9F88"`.
- **Integration fork commit:** `ac50410` on branch `patches/spike-02-d4`
  (`raouldekezel/dolphin-robot`), cut from `deploy@a01f0b5`. The probe
  stamps a UUID4 hex `clientToken` on every `_send_desired_command`
  write and logs it via `_LOGGER.info("[SPIKE-02 D4] desired write
  clientToken=%s, payload=%s", token, payload)`. Throwaway — reverted
  immediately after the session via the rollback script.
- **HA:** 2026.1.3 (container `hass` on intel-nuc, `network_mode:
  privileged`).
- **Pre-experiment state:** `v1.0.26b3-raoul.7` was running. Robot in
  `holdWeekly`/`notConnected` at session start.
- **Debug logging:** persistent
  `custom_components.mydolphin_plus.managers.aws_client: debug` from
  `configuration.yaml` (cf. IT doc `Home Assistant - Dolphin S2000.md`).

## Actions taken

01. `01_ha-initiated-mode-change.mqtt.log` — `vacuum.set_fan_speed`
    via HA REST API (`POST /api/services/vacuum/set_fan_speed`,
    `fan_speed=short` on `vacuum.nono_2`; current mode was `all`).
    BUG-13 incidentally fired (mode change triggered a cycle on the
    docked robot) — stopped immediately with `vacuum.pause` once
    enough echoes had landed. Three of our writes hit the wire in this
    capture: the user's `cleaningMode.mode=short`, the integration's
    reactive `cycleInfo.cycleTime=60` (BUG-08 chain, +1.087 s later),
    and the pause's `systemState.pwsState=off`.
02. `02_app-initiated-mode-change.mqtt.log` — from the Maytronics
    MyDolphin Plus app on the phone: "Nettoyage Fond" started (the
    user's app does not separate mode-pick from start), then stopped.
    Two app-initiated `desired` writes hit the wire:
    `cleaningMode.mode=floor` and `systemState.pwsState=off`. Our build
    is still the probe, so the integration's reactive `cycleInfo.cycleTime=120`
    (BUG-08) is visible in-between with its own token, providing a clean
    side-by-side comparison.

## Timeline

All times CEST (UTC+02:00). Source: the two `mqtt.log` files in this
directory.

### Action 1 — HA-initiated mode change

| t (CEST) | Shadow v | Event | Payload key fields |
|---|---|---|---|
| 12:46:23.868 | — | `[SPIKE-02 D4]` publish (us) | `desired.cleaningMode.mode=short`, **`clientToken=f70efdfc…04ea`** |
| 12:46:23.954 | 857 | `/update/accepted` | `desired.cleaningMode.mode=short`, **`clientToken=f70efdfc…04ea`** ← our token echoed |
| 12:46:24.955 | — | `[SPIKE-02 D4]` publish (us, BUG-08 reactive +1.001 s) | `desired.cycleInfo.cycleTime=60`, **`clientToken=7a73c858…cee2`** |
| 12:46:24.962 | 858 | `/update/accepted` | `desired:null` (ACK-driven cleanup of v857), no token |
| 12:46:25.066 | 859 | `/update/accepted` | `desired.cycleInfo.cycleTime=60`, **`clientToken=7a73c858…cee2`** ← our token echoed |
| 12:46:25.122 | 860 | `/update/accepted` | `desired:null` (ACK-driven cleanup of v859), no token |
| 12:46:26.681 | 861 | `/update/accepted` | `reported.systemState.pwsState=on, robotState=init, cleaningMode.mode=short, cycleTime=60`, no token (device) |
| 12:47:10.118 | — | `[SPIKE-02 D4]` publish (us, `vacuum.pause`) | `desired.systemState.pwsState=off`, **`clientToken=61862fce…6956`** |
| 12:47:10.206 | 862 | `/update/accepted` | `desired.systemState.pwsState=off`, **`clientToken=61862fce…6956`** ← our token echoed |
| 12:47:14.484 | 864 | `/update/accepted` | `reported.systemState.pwsState=holdWeekly, robotState=notConnected`, no token (device) |

### Action 2 — app-initiated mode change + stop

| t (CEST) | Shadow v | Event | Payload key fields |
|---|---|---|---|
| 12:49:51.991 | 866 | `/update/accepted` | `desired.cleaningMode.mode=floor`, **no token** ← app |
| 12:49:52.992 | — | `[SPIKE-02 D4]` publish (us, BUG-08 reactive +1.001 s) | `desired.cycleInfo.cycleTime=120`, **`clientToken=a4a54719…12be3`** |
| 12:49:52.995 | 867 | `/update/accepted` | `desired:null` (ACK-driven cleanup of v866), no token |
| 12:49:53.090 | 868 | `/update/accepted` | `desired.cycleInfo.cycleTime=120`, **`clientToken=a4a54719…12be3`** ← our token echoed |
| 12:49:53.400 | 869 | `/update/accepted` | `desired:null` (ACK-driven cleanup of v868), no token |
| 12:49:53.769 | 870 | `/update/accepted` | `reported.pwsState=on, robotState=init, cleaningMode.mode=floor, cycleTime=120`, no token (device) |
| 12:49:54.525 | 871 | `/update/accepted` | `reported` continuation, no token (device) |
| 12:50:25.810 | 872 | `/update/accepted` | `desired.systemState.pwsState=off`, **no token** ← app (stop) |
| 12:50:25.897 | 873 | `/update/accepted` | `desired:null` (ACK-driven cleanup of v872), no token |
| 12:50:27.789 | 874 | `/update/accepted` | `reported.pwsState=holdWeekly, robotState=notConnected`, no token (device) |

## Findings

**PRIMARY — does AWS echo our injected `clientToken` on `/update/accepted` for this Maytronics thing? YES.**

- 3 of our writes hit the wire across the two captures
  (`cleaningMode.mode=short` at 12:46:23.868, `cycleInfo.cycleTime=60`
  at 12:46:24.955, `systemState.pwsState=off` at 12:47:10.118 — all in
  action 1; plus `cycleInfo.cycleTime=120` at 12:49:52.992 in action 2,
  i.e. 4 our-writes total). Each was echoed unchanged on the matching
  `/update/accepted` payload as `…,"timestamp":N,"clientToken":"<hex>"}`,
  identical to the token logged by the probe. The echo is independent
  of robot connection state (the first echo at 12:46:23.954 landed
  while `robotState=notConnected`).
- Observed round-trip latency (publish → echo on accepted):
  - 12:46:23.868 → 12:46:23.954 = **86 ms**
  - 12:46:24.955 → 12:46:25.066 = **111 ms**
  - 12:47:10.118 → 12:47:10.206 = **88 ms**
  - 12:49:52.992 → 12:49:53.090 = **98 ms**
  - Mean ≈ 96 ms, max observed ≈ 111 ms. A D2 TTL needs to be much
    larger than this — but the TTL is also bounded by the QoS-0
    dropped-publish window, not by the happy-path latency, so the
    happy-path number is informational only.

**DISCRIMINATOR — does "token ∈ our set" separate our writes from foreign events live? YES, cleanly.**

- Action 1 capture: **3 our-events with our tokens, 5 foreign events
  without token** (2 ACK-driven `desired:null` cleanups, 2 device
  `reported` payloads, 1 stop ACK-driven cleanup at the end).
- Action 2 capture: **1 our-event with token, 8 foreign events without
  token** (2 app-initiated `desired` writes, 3 ACK-driven `desired:null`
  cleanups, 3 device `reported` payloads).
- Across both captures: **4 our-events / 13 foreign events / 0 false
  positive on either side.** The discriminator behaves as the AWS
  documentation predicts (`clientToken` present only if a client token
  was used on that update — [Device Shadow documents](https://docs.aws.amazon.com/iot/latest/developerguide/device-shadow-document.html))
  and the empirical D5 inference from historical traces holds in
  real-time.
- Confirms the choice surveyed in D1: even on a setup where the device
  or app started stamping tokens later, `∈ our set` would stay safe
  whereas a simple "token present" check would not. Sticking with
  `∈ our set` for robustness, as recommended in D1 §2c.

**BUG-08 visible in the wild — the integration's reactive write fires on app-initiated mode changes, unconditionally.**

- In action 2, the app pushes `desired.cleaningMode.mode=floor` at
  12:49:51.991 (the user did not change cycleTime). The integration's
  branch `:456-465` of `_message_callback` fires the `Set cycle time`
  chain unconditionally at 12:49:52.992 — exactly 1.001 s later, the
  signature `time.sleep(1)` of that branch — overwriting whatever the
  app had set with the integration's locally configured
  `cycle_time_floor=120`. The `[SPIKE-02 D4]` line at 12:49:52.992
  proves this write goes out as `/_send_desired_command` (this is the
  reactive cycle-time chain, not a user-driven write). Independent
  reconfirmation of BUG-08 (#17) on the wire, with a clear discriminator
  signal that would have prevented it: the trigger had no `clientToken`,
  the reaction did.

**No regressions in the probe build.**

- Integration loaded cleanly on probe deploy (no exceptions in the
  startup window). The `Payload:` lines on `/update/accepted` after
  our writes show the existing accepted-branch handler tolerates the
  added top-level `clientToken` key without complaint (it iterates
  `state.reported`, not the root). No HARD-10 `NoneType` errors in
  startup. Rolled back cleanly via
  `/tmp/spike02-d4-rollback.sh` to `v1.0.26b3-raoul.7`.

## Open questions

- **D2 — TTL value for the in-flight token set.** The happy-path
  round-trip observed here is ≤ 111 ms, but TTL is bounded by the QoS-0
  dropped-publish window (publish silently dropped → no echo ever
  arrives → leaked token if no TTL). Suggest D2 picks a TTL on the
  order of seconds (10–30 s?) — defensible upper bound on a real cloud
  round-trip including a single TCP-level retransmit, well below any
  reasonable user perception of "stale" state. Concrete value is D2's
  decision.
- **D3 — the reactive `Set cycle time` site at `aws_client.py:456-465`
  is in scope for the pattern.** Confirmed in real-time by action 2:
  it triggers on app-initiated mode changes (because the integration
  has no provenance signal). After D2 ships, this branch must gate on
  `accepted.clientToken ∈ in_flight` → only run when we wrote the mode
  ourselves. The HARD-09 `rejected → WARNING` branch at `:413-416` is
  the second known site, identical gating shape.
- **D3 — possible third site: the `_send_dynamic_command` path.**
  Not exercised in this session (no joystick / navigate calls). Worth
  checking whether `/dynamic` topics carry a comparable correlation
  primitive or whether the pattern is shadow-specific.
- **Hygiene — narrow the `shadow/#` wildcard.** D1 §3 noted AWS
  recommends against the wildcard. Independent of the provenance fix
  but worth bundling into the same architectural pass (separate ticket?).

## Refs

- Spike: [#70 SPIKE-02](https://github.com/raouldekezel/dolphin-robot/issues/70)
- D1 — mechanisms study: [#70 comment 4735328308](https://github.com/raouldekezel/dolphin-robot/issues/70#issuecomment-4735328308)
- D4 — test plan (this session's charter): [#70 comment 4735499483](https://github.com/raouldekezel/dolphin-robot/issues/70#issuecomment-4735499483)
- D5 — existing-trace examination: [#70 comment 4735450675](https://github.com/raouldekezel/dolphin-robot/issues/70#issuecomment-4735450675)
- Related: [HARD-09 #66](https://github.com/raouldekezel/dolphin-robot/issues/66), [BUG-08 #17](https://github.com/raouldekezel/dolphin-robot/issues/17), [BUG-13 #47](https://github.com/raouldekezel/dolphin-robot/issues/47)
- Probe branch: `patches/spike-02-d4` (commit `ac50410`), rolled back live.
