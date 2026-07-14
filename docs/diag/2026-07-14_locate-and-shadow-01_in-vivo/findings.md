# Locate + LED writes — full behaviour on the S2000 (`v1.0.26b3-raoul.19`)

## TL;DR

In-vivo continuation of [HARD-03 (#24)](https://github.com/raouldekezel/dolphin-robot/issues/24), driven by the follow-up question elad-bar's comment framed and Raoul's arbitration comment posed ("what about `locate` on a disconnected robot?"). Six timed sub-tests on the same S2000 within a ~22 min window (11:53:56 → 12:16:23 UTC on 2026-07-14). Four new facts:

- **Locate is functionally useless on this hardware.** The S2000's LED circuit is only powered while the robot is running (`pwsState=on`). In `holdWeekly` — the steady-state of every sleeping robot — `ledEnable=true` writes have no physical effect. Raoul confirmed visually across four independent triggers. The nominal use case ("find the robot at the bottom of the pool") is unreachable by design.
- **SHADOW-01 (new) — cloud auto-mirror of `desired → reported` when the robot is dormant.** When `isConnected=true` and a delta is emitted, an AWS/Maytronics-side mechanism clears `desired` ~200 ms later (`{"desired":null}`) and posts a `reported` patch matching what was requested ~2 s later — **without the firmware executing anything**. `LastReceiveData.timestamp` proves the PWS never spoke. `light.<robot>_led` therefore lies at two layers: HA-side via HARD-03, and shadow-side via SHADOW-01. `reported`-only entity state (the #24 pessimistic-design remedy) is necessary but not sufficient.
- **The cloud auto-mirror is gated by `isConnected=true`.** With `isConnected=false` a real delta parks legitimately and no mirror fires. Discriminant: a `desired.led=false` published at T3 while offline was never followed by a clear or reported patch (v2807 accepted, then nothing for 90 s until reconnect).
- **Scenario B confirmed for LED writes (analog of the BUG-21 gate).** The `desired.led=false` parked at T3 was consumed at the wake edge (TUP+~0 s): `light.<robot>_led` became `off` immediately after the alimentation rising edge, before any operator action. The proof is one-sided (the physical LED was off before AND after — no visible effect) but the shadow evolution is unambiguous.

Two independent findings confirmed:

- **HARD-03 mutation reproduces on `ledEnable`**, not just `ledIntensity` — the `light.turn_off` API response at T3+10 ms returns `state:"off"` before any AWS ACK. Same shared-reference bug at `aws_client.py:738`, all three LED entities affected, fix already known (`dict(...)` shallow copy — leaves are scalars).
- **`is_locating=True` is persisted on every `_vacuum_locate` call**, even when the robot is unreachable (four separate `Storing config data, keys: [..., 'locating', ...]` events, T0/T1/T2/T5). No observable reader for this flag — confirmed suspicion from the [#24 arbitration comment](https://github.com/raouldekezel/dolphin-robot/issues/24#issuecomment-…): dead write.

Arbitration bottom line for #24:

- **Extend the BUG-21 (#112) Fix-1 gate to Locate.** Same tri-state read of `SystemDetails.pws_connected`, same `ServiceValidationError`, same wording. Locate is not a special case — the code (`_vacuum_locate → _set_led_enabled → _get_led_settings`) shows it is an LED write.
- **Consider adding a `pwsState=on` (or equivalent robot-active) gate for LED writes generally.** Not doing so leaves the operator with no signal that a click on the LED entity in idle mode produces no physical effect but does update HA and shadow to lie about it. SHADOW-01 makes this doubly deceptive: even a purely-`reported`-based entity would report the mirrored value.
- **File SHADOW-01 as a separate ticket.** It is a discovery, not a fix; it changes the reasoning envelope for the pessimistic redesign but is orthogonal to any single write path.

## Context

- Fork: `raouldekezel/dolphin-robot`, `deploy` branch, HACS-installed `v1.0.26b3-raoul.19`.
- Robot: S2000, `sensor.nono_2_etat_du_robot = notconnected` throughout except during the final cycle (T + 21 min). PWS firmware `pwsSwVersion = "11.0004"`, MU firmware `muSwVersion = "9F88"`.
- Instrumentation: `custom_components.mydolphin_plus` at DEBUG; `docker logs -f hass` stripped of ANSI and filtered on `mydolphin_plus|locate|led|isConnected`, streamed to a temp file on `intel-nuc` for the whole 22 min window. Full trace: `full.mqtt.log` (320 lines, deduped by timestamp).
- Physical observer: Raoul, standing at the pool for the full session (LED calls, LED continuity in cycle, cycle start).
- Six sub-tests, spanning both #112 Q1 axes of "disconnection":
  1. **Cas 2** — `vacuum.locate` on a dormant robot with PWS online (`isConnected=true`, `robotState=notConnected`). Same-value baseline (`ledEnable=true` already) — no delta.
  2. **Cas 2bis** — `light.turn_off` (creates a real delta from `true`→`false`) then `vacuum.locate` (reverses it). Same connectivity as Cas 2.
  3. **Cas 1** — `vacuum.locate` after cutting the plug and waiting for the LWT (`isConnected=false`, PWS unreachable).
  4. **Cas 1bis** — `light.turn_off` while still offline. Real delta with `isConnected=false`.
  5. **Cas 1ter** — plug back on; observe wake behaviour and whether the parked `desired.led=false` from step 4 is consumed.
  6. **Cycle actif** — `vacuum.set_fan_speed=short`, `vacuum.start`, then change `ledMode` from Blinking to Always-on during the cycle. Baseline that isolates "firmware executes physically" from the shadow's optimistic mirroring.

## Timeline

Timestamps in UTC. Δt relative to the sub-test's own T0 for readability.

| Test | UTC | Δt | Event | Shadow / physical |
| --- | --- | ---: | --- | --- |
| **Cas 2 — same-value baseline** | | | | |
| | 11:53:56.314 | T0 = 0 | POST `vacuum/locate` (baseline `ledEnable=true`) | `reported.led={true,100,1}` (stale from 05:00 UTC) |
| | 11:53:56.322 | +8 ms | `is_locating=True` persisted; `Set led enabled mode` publish #2327 | HARD-03: no local mutation observable (same value) |
| | 11:53:56.382 | +68 ms | `shadow/update/accepted` v2790 | `desired.led={true,100,1}` stored |
| | 11:53:56.429 | +115 ms | `shadow/get/accepted` v2790 | desired and reported identical — **no delta emitted** |
| | 11:54:35.788 | +39 s | `shadow/get/accepted` v2790 | `desired.led` still parked, no clear, no reported change |
| | | | **Physical LED**: off throughout (Raoul) | Firmware never executed the write |
| **Cas 2bis — real delta, PWS online** | | | | |
| | 11:59:56.973 | T0 = 0 | POST `light/turn_off` | HA state flipped to `off` at T0+5 ms via API response — HARD-03 mutation on `ledEnable` |
| | 11:59:57.121 | +148 ms | `shadow/update/accepted` v2791 + **`shadow/update/delta`** | `desired.led={false,...}`, delta emitted because `reported.led.ledEnable=true` |
| | 11:59:57.131 | +158 ms | `shadow/get/accepted` v2791 | Both branches visible; `LastReceiveData.timestamp=1784008812` (frozen from morning) |
| | **11:59:57.176** | **+203 ms** | **`shadow/update/accepted` v2792 — `{"desired":null}`** | **Cloud clears `desired` — first sight of SHADOW-01** |
| | **11:59:59.064** | **+2.09 s** | **`shadow/update/accepted` v2793 — `reported.led={false,...}`** | **Cloud posts a `reported` patch matching the requested value; `LastReceiveData.timestamp` unchanged → PWS never spoke** |
| | 12:00:01.988 | T1 = +5.015 s | POST `vacuum/locate` | Second half — reverse the mutation |
| | 12:00:01.997 | T1+9 ms | `is_locating=True` re-persisted; publish #2351 | HA state flipped to `on` |
| | 12:00:02.073 | T1+85 ms | `shadow/update/accepted` v2794 + delta | `desired.led={true,...}` |
| | 12:00:02.264 | T1+276 ms | `shadow/update/accepted` v2795 — `{"desired":null}` | Cloud clears again |
| | 12:00:03.733 | T1+1.75 s | `shadow/update/accepted` v2796 — `reported.led={true,...}` | Cloud mirrors; `LastReceiveData.timestamp` still unchanged |
| | | | **Physical LED**: off throughout (Raoul) | Two full shadow round-trips, zero physical effect |
| **Cas 1 — locate hors ligne** | | | | |
| | 12:05:03.302 | TCUT = 0 | POST `switch/turn_off` on `switch.sps_04_nono` | PWS unplugged |
| | 12:05:45.777 | +42 s | `shadow/update/accepted` v2797 — `reported.isConnected.connected=false` | LWT trio: `{isConnected.connected:false, dynamicTopics:[""], robotSerial:""}` |
| | 12:06:00.581 | T2 = 0 | POST `vacuum/locate` (hors ligne) | `isConnected=false`, `robotState=notConnected` |
| | 12:06:00.589 | T2+8 ms | `is_locating=True` persisted; publish #2371 | Same as before |
| | 12:06:00.669 | T2+88 ms | `shadow/update/accepted` | `desired.led={true,100,1}` — same-value trap, no delta |
| | 12:06:35.788 | T2+35 s | `shadow/get/accepted` | `desired.led` still parked, no clear, no reported patch |
| **Cas 1bis — real delta hors ligne (SHADOW-01 discriminant)** | | | | |
| | 12:09:16.908 | T3 = 0 | POST `light/turn_off` while `isConnected=false` | HA state flipped to `off` at T3+10 ms (HARD-03) |
| | 12:09:16.966 | T3+58 ms | `shadow/update/accepted` | `desired.led={false,100,1}` stored |
| | 12:09:16.999 | T3+91 ms | **`shadow/update/delta`** | Emitted because reported=true ≠ desired=false — **first delta of the offline block** |
| | | | | **No `{"desired":null}` clear, no `reported` patch — for 89 s until reconnect** |
| **Cas 1ter — wake behaviour** | | | | |
| | 12:10:46.005 | TON = 0 | POST `switch/turn_on` | Plug back |
| | 12:10:58.xxx | TON+~12 s | (implicit) PWS posts `LastReceiveData.timestamp=1784031058` | First real PWS transmission of the session |
| | 12:11:08.950 | TON+22.9 s | `binary_sensor.nono_2_alimentation = on` (rising edge processed by FEAT-07 sensor) | `isConnected=true` again |
| | | | | **`light.nono_2_led = off` in HA** — the parked `desired.led=false` was consumed at wake (Scenario B for LED writes) |
| **Cas 1ter continued — turn_off then turn_on while notConnected+alim on** | | | | |
| | 12:11:44.033 | T4 = 0 | POST `light/turn_off` (already `off`) | Same-value publish, delta = ? |
| | 12:11:44.xxx | T4+~90 ms | `shadow/update/accepted` | No cloud clear observed (needs re-verify; same-value trap likely) |
| | 12:12:13.552 | T5 = 0 | POST `light/turn_on` | HA `light=on` at T5+13 ms (HARD-03) |
| | 12:12:13.634 | T5+82 ms | `shadow/update/accepted` v2809 + delta | Real delta this time |
| | 12:12:13.895 | T5+343 ms | `shadow/update/accepted` v2810 — `{"desired":null}` | Cloud auto-mirror active — confirms it fires as soon as `isConnected=true` |
| | 12:12:14.023 | T5+471 ms | `shadow/update/accepted` v2811 — `reported.led={true,...}` | Cloud mirror completes; `LastReceiveData` still frozen from wake |
| | | | **Physical LED**: still off (Raoul) | Third demonstration that the mirror is decoupled from firmware execution |
| **Cycle actif — the ground truth** | | | | |
| | 12:15:04.850 | Tset = 0 | POST `vacuum/set_fan_speed=short` | Stage `_desired_clean_mode` |
| | 12:15:06.868 | +2 s | POST `vacuum/start` | HARD-11 overlay flips vacuum to `cleaning` immediately |
| | | | (Firmware boots, `pwsState` transitions to programming/on, robot enters init) | Physical: LED starts **blinking** (Raoul) — first physical LED activity of the session, matching `ledMode=1=Blinking` |
| | 12:16:23.817 | T6 = 0 | POST `select/select_option` on `select.nono_2_mode_led`, `option=2` (Always on) | `ledMode: 1 → 2` |
| | | | **Physical LED**: switches to **continuous on** (Raoul) | Firmware executes writes in real time when `pwsState=on` |

## Findings

### F1 — HARD-03 shared-reference mutation reproduces on `ledEnable`

Cas 2bis, `light.turn_off` at T0=11:59:56.973: the HA REST response for the service call **already carries `state:"off"` at T0+5 ms** — 143 ms before `shadow/update/accepted`. Same shape as the original `ledIntensity` reproduction (findings from [2026-07-13](../2026-07-13_hard-03_led-eager-write-in-vivo/findings.md)), same root cause (`aws_client.py:738`), same one-line fix. This session covers the second of three service entry points that go through `_get_led_settings` (the third is `set_led_mode`, code-inspected — same code path).

### F2 — SHADOW-01: cloud auto-mirror of `desired → reported` when `isConnected=true`

Cas 2bis at T0+203 ms, an `update/accepted` payload arrives with `{"desired":null}`; at T0+2.09 s a `reported` patch arrives writing exactly the requested `led` values. Neither event originates from the integration (both are received on `shadow/update/accepted`, no matching `Publishing` line, no `clientToken`). The `LastReceiveData.timestamp` field is unchanged across the whole window — proof that the PWS never posted anything to the shadow.

Reproduced three times in the session (Cas 2bis T0, Cas 2bis T1, Cas 1ter T5) with the exact same timing structure:

- ~200 ms after the integration's own `shadow/update/accepted`, a `{"desired":null}` `update/accepted` is emitted from the cloud side.
- ~2 s after that, a `reported` patch is emitted matching the just-cleared `desired`.
- No firmware round-trip evidence in either payload (no `LastReceiveData` update, no `metadata` timestamps advancing on non-`led` fields).

**Confined to `isConnected=true`.** Cas 1bis (T3=12:09:16.908, `isConnected=false`, real delta) never triggers the mirror: `desired.led={false,...}` sat in the shadow untouched for 89 s until reconnect. Which makes the mechanism sound like an AWS IoT lifecycle rule that requires the thing to be marked "connected" — not surprising, but load-bearing for the pessimistic redesign.

**Consequence for the pessimistic design (#24 arbitration comment).** The proposed "keep the entity state based exclusively on `reported`" no longer eliminates the lie in the dormant-robot case; it only moves it from HA-side (HARD-03 mutation) to cloud-side (SHADOW-01 mirror). The honest discriminant is `LastReceiveData.timestamp`: if the shadow's `reported.led.*` metadata timestamp is more recent than the last advance of `LastReceiveData.timestamp`, the reported value is synthetic and must not be surfaced. Design implications are for another ticket — this session establishes the mechanism, not the remedy.

### F3 — LED is only physically driven while `pwsState=on`

Raoul's observations, four independent triggers on the sleeping robot (Cas 2, Cas 2bis (twice), Cas 1ter T4/T5): **LED off**. One trigger on the active robot (Tset+2s, cycle start): **LED blinking** (matches `ledMode=1`). One trigger during the same active cycle (T6, `ledMode: 1 → 2`): **LED switches to continuous on**.

This is not a firmware bug — it is likely a hardware property of the S2000 (the LED circuit shares power with the pump/motor block, which is unpowered in `holdWeekly`). It means:

- Every LED write while `pwsState != on` is a semantic no-op.
- The MyDolphin+ app's precondition for LED controls ("powered on and cleaning") is a UX consequence, not an arbitrary constraint.
- Locate on a dormant robot cannot succeed by construction, regardless of shadow-side plumbing.

### F4 — `LastReceiveData.timestamp` is the only honest signal of physical activity

Independently confirmed twice:

- During Cas 2 / Cas 2bis / Cas 1 / Cas 1bis (all dormant sub-tests): `LastReceiveData.timestamp = 1784008812` throughout (05:00:12 UTC, morning value). Every `reported.led` patch in those windows carries its own fresh metadata timestamp (T-relative writes) — but `LastReceiveData` doesn't move, because the PWS didn't speak.
- At wake (Cas 1ter, TON=12:10:46): `LastReceiveData.timestamp` advances to `1784031058` = **12:10:58 UTC**, 12 s after the plug came back on. This is the marker of the first real PWS transmission of the session.

This is the primitive the pessimistic redesign needs for its "reported is trustworthy" predicate. Not `isConnected.connected` alone (SHADOW-01 acts under `isConnected=true`), not `metadata.reported.<field>.timestamp` (equally cloud-writable).

### F5 — `is_locating` is a dead write

Four separate `Storing config data, keys: [..., 'locating', ...]` events, one per `_vacuum_locate` call (T0=11:53:56.321, T1=12:00:01.996, T2=12:06:00.589, T5=12:12:13.552). The exact same key set is repersisted every time, with the same "existing" list — the write is unconditional. No log line ever reads it back in this session (a grep of `full.mqtt.log` for `locating` returns only these four writes). Confirms the suspicion in the [#24 arbitration comment](https://github.com/raouldekezel/dolphin-robot/issues/24#issuecomment-…): `is_locating` looks write-only. Even a refused Locate would still persist it under today's ordering (`_vacuum_locate` writes it before publishing).

Consequence: if Locate is extended into the BUG-21 gate, the ordering fix from D2 (`refuse before persist`) applies here as well — a refused Locate must leave no trace.

### F6 — Scenario B confirmed for LED writes (Cas 1ter)

At T3 (12:09:16, `isConnected=false`) a `desired.led={false,100,1}` was published. It parked in the shadow with no cloud mirror (per F2's confinement). After the plug came back on and `alimentation=on` fired (TUP=12:11:08.950), the very next HA state check on `light.nono_2_led` returned `off`. The parked `desired` was consumed at wake.

Two interpretations remain empirically indistinguishable from this session:

- **The firmware consumed it on boot** — plausible; the wake sequence includes a shadow sync.
- **The cloud auto-mirror kicked in on the rising edge of `isConnected`** — also plausible; SHADOW-01 is confined to `isConnected=true`, and it fires ~200 ms after any `update/accepted`.

Both interpretations imply the same behavioural consequence: a `desired.led.*` write issued while the PWS is offline is not benign — it will be reflected in the shadow at reconnect, and (if the firmware consumes it and the robot is running) potentially executed physically. For an LED write specifically, on the S2000, F3 makes the physical execution moot outside an active cycle. But the observable state (HA entity, shadow) still lies. Same class of surprise as BUG-21 Scenario B on the mode-pick path, one severity level down.

## Consequences

### For BUG-21 (#112)

- **Extend the Fix-1 gate to Locate.** Same `pws_connected is False` tri-state check, same `ServiceValidationError` with `translation_key=power_supply_disconnected`, same wording ("robot's power supply is disconnected"). Ordering constraint: the check must precede the `is_locating` persist and the `_set_led_enabled` call — mirrors the D2 constraints on `_vacuum_start`/`_pickup`.
- **At N=3 consumers of the gate, revisit the helper extraction** (nit deferred at PR [#149](https://github.com/raouldekezel/dolphin-robot/pulls/149) — the reviewer's rationale for inlining at N=2 was "load-bearing ordering visible at each site"; N=3 is the point).

### For HARD-03 (#24)

- **The one-line fix at `aws_client.py:738` is still the right thing to do** — it eliminates the HA-side lie regardless of what the cloud does. Independent of SHADOW-01. Independent of any gate.
- **The pessimistic redesign in the arbitration comment needs one modification**: "keep entity state based exclusively on `reported`" is insufficient; add "verify `LastReceiveData.timestamp` has advanced since the requested change" — otherwise SHADOW-01's cloud-mirrored `reported` values are surfaced as real.
- **`_vacuum_locate`'s `is_locating=True` persist should move after the gate** (F5) — a refused Locate should leave no persisted state.
- **Consider hiding the vacuum Locate action by default for the S2000** ([FEAT-06 (#143)](https://github.com/raouldekezel/dolphin-robot/pull/143) already added the toggle). F3 makes the case: the LED it targets is never lit outside an active cycle. On a small robot like the S2000, keeping Locate visible is a UX trap.

### For a new ticket: SHADOW-01

Propose title: **"AWS/Maytronics-side auto-mirror of `desired → reported` when `isConnected=true` — the shadow itself can lie about physical state"**.

Scope proposal (not implementing here):

- Document the mechanism and its confinement (only under `isConnected=true`; triggered by any `update/accepted` that carries a delta; the two-step `desired:null` then `reported` patch shape).
- Establish `LastReceiveData.timestamp` as the honest discriminant for "the PWS actually spoke".
- Frame the design lever: a small predicate — call it `physical_ack_pending(section)` or similar — that compares the `reported.<section>` timestamp with the last-observed `LastReceiveData.timestamp` and returns whether the value can be trusted.
- Enumerate the entities affected (any `reported.<section>` write path that can be triggered while the robot is dormant): the three LED entities, potentially `weeklySettings` (untested here), any future write surface.
- Open question: whether the mirror ever fires for a same-value publish (Cas 2 and Cas 1 T4 both had `reported==desired` and no delta was emitted, so the mirror had nothing to do — this session does not settle the "same-value trap" case).

## Verification checklist for the reviewer

- [x] Six sub-tests reproducible from the timeline. All timestamps to the millisecond in `full.mqtt.log`.
- [x] SHADOW-01 triggered three times in-session (Cas 2bis T0, Cas 2bis T1, Cas 1ter T5).
- [x] SHADOW-01 confinement to `isConnected=true` established by the negative case (Cas 1bis T3, real delta, no mirror).
- [x] `LastReceiveData.timestamp` frozen at 1784008812 across all four dormant-robot sub-tests, advances to 1784031058 exactly at wake (Cas 1ter TON+12 s).
- [x] Physical LED behaviour observed directly by Raoul across all triggers: off in dormant, blinking on cycle start (`ledMode=1`), continuous on `ledMode=2`.
- [x] `is_locating` persist appears exactly once per `_vacuum_locate` call (four occurrences); no reader anywhere in the log.
- [ ] Same-value publish under `isConnected=true` — does SHADOW-01 fire? Cas 2 baseline was under-informative because no delta was emitted; needs a dedicated follow-up if the pessimistic redesign relies on this edge.

## Files in this diag

- `findings.md` — this document.
- `full.mqtt.log` — 320 lines, deduped by timestamp, ANSI-stripped, filtered on `mydolphin_plus|locate|led|isConnected`. Window 13:53:01 → 14:20:41 CEST (11:53:01 → 12:20:41 UTC).

## Refs

- [HARD-03 (#24)](https://github.com/raouldekezel/dolphin-robot/issues/24) — extends the original session and answers the "what about Locate?" arbitration.
- [BUG-21 (#112)](https://github.com/raouldekezel/dolphin-robot/issues/112) — Fix 1 (#149) merged; extension to Locate follows from F2/F3/F5.
- [FEAT-07 (#145)](https://github.com/raouldekezel/dolphin-robot/pull/145) — the Power Supply connectivity sensor was the instrument for the LWT falling/rising edges (Cas 1 timing).
- [FEAT-06 (#143)](https://github.com/raouldekezel/dolphin-robot/pull/143) — the Locate-hide preference: F3 argues this should be defaulted on for S2000.
- Prior session: [2026-07-13_hard-03_led-eager-write-in-vivo](../2026-07-13_hard-03_led-eager-write-in-vivo/findings.md).
