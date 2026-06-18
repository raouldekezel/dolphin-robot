# SPIKE-02 pre-D2 — close every sequencing / atomicity / offline / ack uncertainty before proposing D2

## TL;DR

**Six PASS at AWS level, plus a clean atomic isolation re-test (E7) that
flips the D2 architecture: the firmware does NOT honor a `cycleTime` field
bundled with a `cleaningMode.mode` field in the same shadow update — even
with the integration's reactive chain removed. End-to-end, the device
silently kept its previous cycleTime (150) instead of our requested
T=75.** Sequenced mode-then-cycleTime writes are therefore mandatory; the
"remove BUG-08 + atomic combined" hand-off from the [pre-D2 results
comment](https://github.com/raouldekezel/dolphin-robot/issues/70#issuecomment-4742855181)
and its [E7 charter follow-up](https://github.com/raouldekezel/dolphin-robot/issues/70#issuecomment-4743131266)
are both retracted by E7's empirical outcome — see the **D2 hand-off
(corrected by E7)** section below.

The other six experiments stand: `desired:null` is empirically
PWS-firmware-authored (E3b: zero events during a 102 s PWS-offline window;
one arrived ~3 s after the SPS-04 plug came back on). AWS echoes our
injected `clientToken` opaquely on `/update/accepted` and on
`/update/rejected`, treats every well-formed update as a new event
(no no-op dedup), and the per-session UUID variant is viable (E5).

## Context

- **Date:** 2026-06-18 (15:56–16:09 CEST)
- **Robot:** Maytronics Dolphin S2000 (Nono 2). Firmware reports
  `robotType:"S4"`, `pwsSwVersion:"11.0004"`, `muSwVersion:"9F88"`.
  Robot starts the session in `pwsState=holdWeekly`, `robotState=notConnected`.
- **Integration fork commit:** `d3038aa` on branch `patches/spike-02-pre-d2`
  (`raouldekezel/dolphin-robot`), cut from `deploy@a01f0b5`. Probe v2 changes:
  - `_send_desired_command` consults a module-level
    `_SPIKE_FIXED_TOKEN`; when set, every desired write reuses K instead
    of minting a fresh `uuid4()` (E5/E6 token-reuse case).
  - new method `AWSClient.spike_publish(payload, client_token,
top_level_version)` publishes the literal
    `{"state":{"desired": payload}, "clientToken": token, ["version": V]}`
    document, bypassing every helper.
  - two HA services registered idempotently from `async_setup_entry`:
    `mydolphin_plus.spike_publish` and `mydolphin_plus.spike_set_fixed_token`.
- **HA:** 2026.1.3 (container `hass` on intel-nuc, `network_mode: privileged`).
- **Debug logging:** persistent `…managers.aws_client: debug` from
  `configuration.yaml`.
- **Probe lifecycle:** deployed via `/tmp/spike02-pre-d2-deploy.sh`, driven
  via `/tmp/spike02-pre-d2-helpers.sh`, rolled back via
  `/tmp/spike02-pre-d2-rollback.sh`. Post-rollback the current
  `home-assistant.log` shows zero `SPIKE-02` lines and zero
  `mydolphin_plus.spike_*` services — clean.

## Actions taken

1.  `E3b_pws-offline.mqtt.log` — `e3b_run`: cuts `switch.sps_04_nono` (mirrors
    the safe nightly `piscine_plug_nono_coupe_la_nuit` automation), waits
    20 s for the PWS to lose power, publishes `desired.cleaningMode.mode=short`
    via `spike_publish`, waits 90 s while the PWS is fully off, restores
    `switch.sps_04_nono`, captures the reconnect window.
    Argument: `E3B_MODE=short`.
2.  `E1_combined-write.mqtt.log` — `e1_run`: single shadow update with
    `desired = {cleaningMode:{mode:"stairs"}, cycleInfo:{cycleTime:90}}`
    via `spike_publish`. Starting state: `pwsState=on`, `robotState=init`
    (carry-over from the E3b reconnect). Robot is in the pool, umbilical
    connected — operator confirmed. The cycle that started on
    application was paused immediately afterwards via `vacuum.pause`.
    Arguments: `E1_MODE=stairs E1_TIME=90` (stairs' stored cycleTime in
    `cleaningModes.stairs` was 150 at the time, so T≠stored).
3.  `E2_flat-cycle-time.mqtt.log` — `e2_run`: `desired.cycleInfo.cycleTime=75`
    via `spike_publish`, **no** `cleaningMode.mode` field. Robot in
    `holdWeekly/notConnected` post-pause; current mode at the time was
    `stairs` (carry-over). Argument: `E2_TIME=75`.
4.  `E5_token-reuse.mqtt.log` — `e5_run`: `spike_set_fixed_token K=deadbeef…0123456`,
    then two `spike_publish` calls 4 s apart with the same K but distinct
    cycleTime values (T1=77, T2=78), then `spike_set_fixed_token ""`
    (clear). Robot in `holdWeekly/notConnected`.
5.  `E6_no-op-write.mqtt.log` — `e6_run`: `spike_set_fixed_token K=cafeface…abcdef`,
    then two `spike_publish` calls 2 s apart with the same K and the
    same cycleTime=82 — designed as a no-op against the current
    desired. Then `spike_set_fixed_token ""`. Robot in `holdWeekly/notConnected`.
6.  `E4_rejected-echo.mqtt.log` — `e4_run`: `spike_publish` with
    `top_level_version=1` (stale; current shadow at v961) and
    `desired.cleaningMode.mode=short`. Provokes the `409 Version
conflict` AWS rejection on a write that carries our token. Robot
    in `holdWeekly/notConnected`.
7.  `E7_atomic-combined-write.mqtt.log` — `e7_run` on the stacked
    branch `patches/spike-02-atomic` (probe v2 + **removal** of the
    BUG-08 reactive `Set cycle time` chain at `aws_client.py:456-465`,
    `sleep(1)` included). Single combined `spike_publish` of
    `desired = {cleaningMode:{mode:"all"}, cycleInfo:{cycleTime:75}}`.
    Robot is in the pool, umbilical connected; cycle was paused
    afterwards via `vacuum.pause`. T=75 distinct from both the
    previous active cycle's cycleTime (150, residual from E1) and
    every value in `cleaningModes` at the moment of publish, so any
    "75" in the device's reported output would be unambiguously ours.
    Arguments: `E7_MODE=all E7_TIME=75`.

(E1b and E3a from the original test plan were skipped: E1 PASSed at the
AWS-atomic level — no need for the back-to-back fallback ordering test;
E3a's contamination is already on the record in D4 action 1 — running it
again would have been redundant.)

## Timeline

All times CEST (UTC+02:00). Source: the six `mqtt.log` files in this
directory.

### E3b — PWS offline

| t (CEST)                     | Shadow v | Event                                                               | Notes                                                                                   |
| ---------------------------- | -------- | ------------------------------------------------------------------- | --------------------------------------------------------------------------------------- |
| ~15:57:23                    | —        | `switch.sps_04_nono` turn_off (helper)                              | PWS losing power                                                                        |
| **15:57:43.201**             | —        | `[SPIKE-02 spike_publish]` `mode=short`, token `3047a811…435c23`    | published while PWS offline                                                             |
| 15:57:43.259                 | —        | `/update/delta` (AWS broadcast)                                     | reaches integration; the PWS isn't there to receive                                     |
| 15:57:44.291                 | —        | `[SPIKE-02]` reactive `cycleTime=60`, token `c3472c78…6235`         | **BUG-08 fires (#17) — reactive chain reacts to the** **integration's own write**       |
| 15:57:43 → 15:59:26 (~103 s) | —        | **0 `desired:null` events**                                         | confirms `desired:null` is firmware-authored (no firmware reachable → none arrives)     |
| ~15:59:23                    | —        | `switch.sps_04_nono` turn_on (helper)                               | PWS boots                                                                               |
| 15:59:25.972                 | —        | `/update/delta`                                                     | post-reconnect catch-up                                                                 |
| **15:59:26.800**             | —        | **First `desired:null` arrives**                                    | ~3 s after `switch.sps_04_nono` turn_on                                                 |
| 15:59:27.207                 | —        | `reported.systemState.pwsState=holdWeekly, robotState=notConnected` | PWS booting                                                                             |
| 15:59:27.763                 | —        | Second `desired:null` (the cycleTime=60 reactive write cleared)     |                                                                                         |
| 15:59:29.675                 | —        | `reported.systemState.pwsState=on, robotState=init`                 | the PWS, on shadow-fetch, applied the buffered `mode=short` desired and started a cycle |

### E1 — combined write `{mode:stairs, cycleTime:90}`

| t (CEST)         | Shadow v | Event                                                                                                                | Notes                                                                                           |
| ---------------- | -------- | -------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------- |
| 16:04:02.805     | —        | `[SPIKE-02 spike_publish]` token `e75f044b…bd99`, payload `{cleaningMode:{mode:"stairs"}, cycleInfo:{cycleTime:90}}` | single shadow/update with both keys                                                             |
| 16:04:02.894     | **941**  | `/update/accepted` echoes our combined `desired`, our token                                                          | RTT **89 ms** — AWS treats the combined write as atomic at the shadow level                     |
| **16:04:03.894** | —        | **`[SPIKE-02]` reactive `cycleTime=150`, token `3e78205f…44da`**                                                     | **BUG-08 fires (#17) — overwrites our T=90 with stairs' stored 150**                            |
| 16:04:03.986     | 943      | `/update/accepted` echoes the reactive `cycleTime=150`, integration's token                                          |                                                                                                 |
| 16:04:05.095     | —        | first device `reported`: `pwsState=on, robotState=init, cleaningMode.mode=stairs, cycleTime=150`                     | device applied the **merged** desired (mode=stairs, cycleTime=150) — **never** saw cycleTime=90 |

### E2 — flat `cycleTime=75` alone (mode stays `stairs`)

| t (CEST)     | Shadow v | Event                                                                                            | Notes                                                                                                |
| ------------ | -------- | ------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------- |
| 16:05:24.247 | —        | `[SPIKE-02 spike_publish]` token `668ea44d…1a2f`, payload `{cycleInfo:{cycleTime:75}}`           | no `cleaningMode.mode` field → integration's reactive chain does NOT fire                            |
| 16:05:24.300 | 952      | `/update/accepted` echoes `cycleTime=75`, our token                                              | RTT **53 ms**                                                                                        |
| 16:05:24.400 | 953      | `/update/accepted` `desired:null`                                                                | PWS cleared after applying (or queuing)                                                              |
| ≥16:05:24    | —        | `reported.cleaningModes.stairs` stays 150; `reported.cycleInfo.cleaningMode.cycleTime` stays 150 | **the 75 did not surface in any visible reported slot within the observation window** — see Findings |

### E5 — token reuse, K = `deadbeef…0123456`

| t (CEST)     | Shadow v | Event                                                                          | Notes                                                   |
| ------------ | -------- | ------------------------------------------------------------------------------ | ------------------------------------------------------- |
| 16:07:20.723 | —        | `[SPIKE-02 set_fixed_token] set (deadbeef…)`                                   | module-level override engaged                           |
| 16:07:21.742 | —        | `[SPIKE-02 spike_publish]` token `deadbeef…0123456` (reused K), `cycleTime=77` |                                                         |
| 16:07:21.823 | **954**  | `/update/accepted` echoes K **unchanged**                                      | RTT **81 ms**                                           |
| 16:07:22.140 | 955      | `/update/accepted` `desired:null` (no token, PWS-authored)                     |                                                         |
| 16:07:25.762 | —        | `[SPIKE-02 spike_publish]` token `deadbeef…0123456` (same K), `cycleTime=78`   | distinct value, same K                                  |
| 16:07:25.819 | **956**  | `/update/accepted` echoes K **unchanged**, `version` increments                | RTT **57 ms** — AWS does not deduplicate on token reuse |
| 16:07:26.190 | 957      | `/update/accepted` `desired:null` (no token)                                   |                                                         |
| 16:07:29.779 | —        | `[SPIKE-02 set_fixed_token] cleared`                                           | back to per-call UUID                                   |

### E6 — same-value twice with same K, K = `cafeface…abcdef`

| t (CEST)     | Shadow v | Event                                                                                             | Notes                                                                        |
| ------------ | -------- | ------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------- |
| 16:07:44.402 | —        | `[SPIKE-02 spike_publish]` `cycleTime=82` with K (write #1)                                       |                                                                              |
| 16:07:44.495 | **958**  | `/update/accepted` echoes K, `cycleTime=82`                                                       | RTT 93 ms                                                                    |
| 16:07:44.689 | 959      | `/update/accepted` `desired:null`                                                                 | PWS cleared `desired` in **~287 ms** post-write                              |
| 16:07:46.420 | —        | `[SPIKE-02 spike_publish]` `cycleTime=82` with same K (write #2) — but `desired` was already null | not a true no-op against the current desired (PWS cleared between #1 and #2) |
| 16:07:46.501 | **960**  | `/update/accepted` echoes K, `cycleTime=82`                                                       | RTT 81 ms — AWS treats as a new event regardless of value                    |
| 16:07:46.731 | 961      | `/update/accepted` `desired:null`                                                                 |                                                                              |

### E4 — rejected echo, stale `version=1`

| t (CEST)     | Shadow v | Event                                                                                                  | Notes                                                                                                                |
| ------------ | -------- | ------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------- |
| 16:08:26.084 | —        | `[SPIKE-02 spike_publish]` token `e678257b…6a71`, `top_level_version=1`, `mode=short`                  | current shadow at v961, version=1 is far-stale                                                                       |
| 16:08:26.132 | —        | `/update/rejected` with body `{"code":409,"message":"Version conflict","clientToken":"e678257b…6a71"}` | RTT **48 ms** — **our token is echoed on the reject path**                                                           |
| 16:08:26.132 | —        | integration's `WARNING` line fires (pathological HARD-09 #66 path)                                     | with a gate on `rejected.clientToken ∈ in_flight` this would stay `WARNING`; foreign 429s would downgrade to `debug` |

### E7 — atomic combined `{mode:all, cycleTime:75}` on the BUG-08-removed branch

| t (CEST)     | Shadow v | Event                                                                                                             | Notes                                                                                                                                                                                                                   |
| ------------ | -------- | ----------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 16:51:01.375 | —        | `[SPIKE-02 spike_publish]` token `f2f7ba2d…ff5c`, payload `{cleaningMode:{mode:"all"}, cycleInfo:{cycleTime:75}}` | one combined publish, BUG-08 chain absent from this build                                                                                                                                                               |
| 16:51:01.435 | **964**  | `/update/accepted` echoes our combined `desired` (both fields), our token                                         | RTT **60 ms** — AWS makes it atomic                                                                                                                                                                                     |
| 16:51:01.597 | 965      | `/update/accepted` `desired:null`                                                                                 | PWS firmware cleared after applying                                                                                                                                                                                     |
| 16:51:03.151 | 966      | first device `reported`: `cycleInfo.cleaningMode = {mode:"all", cycleTime:150}`                                   | **cycleTime is 150, NOT our 75** — see Findings                                                                                                                                                                         |
| 16:52:11     | 973      | `cleaningModes.all` settles at 150, `floor/water/ultra` also at 150                                               | operator-confirmed: 150 is the user's last HA-stored value for "Complet"; the integration's number entities pushed those values into the shadow on HA boot (so `cleaningModes.all = 150` at the moment of our E7 write) |
| 16:52:09.936 | —        | `[SPIKE-02]` from `vacuum.pause` (`pwsState=off`) — only other SPIKE-02 line in window                            | confirms NO reactive write fired between E7 and pause — branch is correctly removed                                                                                                                                     |

## Findings

### E3b — disconnected-PWS write (Q1, Q2, `desired:null` authorship) — **PASS on all three checkpoints**

- **(a)** `/update/accepted` echoes our `desired.cleaningMode.mode=short` with our
  `clientToken=3047a811…` ~58 ms after publish, **while the PWS is fully offline**
  (the SPS-04 plug had been cut 20 s earlier). The Shadow service is cloud-side and
  accepts our `desired` regardless of device connectivity. AWS IoT offline writes
  are supported on this account.
- **(b)** **Zero `desired:null` arrived during the 102-second offline window** (15:57:43 → 15:59:25).
  The two `desired:null` events that did fire arrived at 15:59:26.800 and 15:59:27.763 —
  i.e. **~3 s after the SPS-04 turn_on** — coincident with the PWS booting,
  fetching the buffered desired delta, and applying it. This is the empirical proof
  that **`desired:null` is PWS-firmware-authored, not AWS-service-authored.**
- **(c)** On reconnect, the PWS read the shadow, processed the buffered
  `desired.cleaningMode.mode=short`, transitioned `pwsState: holdWeekly → on`,
  `robotState: notConnected → init`, and started a cleaning cycle. The offline write
  was reliably deferred-applied — the shadow's intended behaviour.
- **Implication for the D4 PR #71 follow-up:** the D4 findings table's "AWS
  service (ACK-driven cleanup)" rows for v858/v860/v867/v869/v873 must be
  retagged **"PWS firmware (robot ack via firmware clearing `desired`)"** —
  see the dedicated section below.

### E1 — combined write — **PASS at AWS level, FAIL end-to-end (BUG-08 wins) — the unlock requires D2 to also gate the reactive chain**

- **AWS atomic write: viable.** A single `shadow/update` carrying both
  `cleaningMode.mode` and `cycleInfo.cycleTime` is accepted in one shot
  (`/update/accepted` v941 at 16:04:02.894 echoes both fields plus our token,
  RTT 89 ms). The AWS Shadow service does not require sibling subtrees to be
  written separately. The "no sequence, no `time.sleep(1)`, no robot-ack wait,
  one `clientToken`" unlock from the ACK-concern reply is structurally available.
- **However, BUG-08 (#17) intercepts before the device applies.** The integration
  observes its own `/update/accepted` echo at 16:04:02.894, branch
  `aws_client.py:456-465` reacts unconditionally, fires `_set_cycle_time`
  ~1 s later (16:04:03.894), overwriting our requested `cycleTime=90` with
  the locally configured `cleaningModes.stairs = 150` (sent as its own
  `desired.cycleInfo.cycleTime=150` with a fresh token). The device, when it
  applies the desired at 16:04:05.095, sees the **merged** state
  (`mode=stairs, cycleTime=150`) — and never the requested `90`.
- **What D2 must include for the unlock to work end-to-end:** gate the reactive
  `:456-465` branch on `accepted.clientToken ∈ in_flight`. When the accepted
  carries our token, the integration must **not** react (we already wrote what
  we wanted; an atomic combined publish needs no follow-up). With that gate, a
  D2 client writing `{mode, cycleTime}` in one publish would have its `90` applied
  end-to-end.

### E2 — is `desired.cycleInfo.cycleTime` mode-relative? — **AWS-side PASS; device-side inconclusive in this run**

- **AWS side:** `/update/accepted` v952 echoes `cycleTime=75` with our token (RTT
  53 ms). The integration's reactive chain **does not** fire — confirms the
  reactive branch is gated on `cleaningMode.mode` deltas, not on `cycleInfo.cycleTime`
  alone. Post-write, `/update/accepted` v953 `desired:null` arrives quickly
  (firmware cleared).
- **Device-side: no visible slot moved within the observation window.**
  `reported.cleaningModes.stairs` stayed 150; `reported.cycleInfo.cleaningMode.cycleTime`
  stayed 150 (the current cycle's effective time, from E1's overwrite); no other slot
  showed `75`. Two plausible interpretations, neither of which this run resolves:
  1. **Queued for the next mode change.** The PWS may treat
     `desired.cycleInfo.cycleTime=T` as an advisory value applied only on the next
     `cleaningMode.mode` transition (the same "launcher picks the duration"
     semantics flagged in [`feedback_dolphin_bug08_launcher_pick_semantics`]
     and BUG-08). Under that reading, writing `T` without a paired mode change
     is a no-op until the next mode change pulls it in.
  2. **Silently discarded.** The PWS acks (clears `desired`) but discards the
     value because no active cycle and no mode change anchor it.
- **What this means for D2:** a flat `desired.cycleInfo.cycleTime=T` is not a
  reliable way to update a stored per-mode cycleTime on its own. D2 should not
  rely on it standalone — combined writes (E1 shape) are the right primitive.
- **Follow-up to settle E2 properly:** repeat with a fresh `cleaningMode.mode`
  change immediately preceding the flat cycleTime write, and observe whether
  `reported.cleaningModes.<mode>` updates to `T`. Out of scope of the current
  session — D2 does not block on it.

### E5 — token reuse — **PASS, per-session UUID is viable**

- Two distinct writes with the same K both echoed K unchanged on
  `/update/accepted` (v954, v956). `version` incremented normally (954 → 956,
  with the intermediate 955 / 957 being the firmware's `desired:null`
  cleanups). RTTs 81 ms then 57 ms — well within the D4 envelope.
- **Implication:** AWS treats `clientToken` opaquely as a per-request echo with
  no cross-request state. **A per-session UUID is therefore viable for D2**
  (mint once at startup, reuse across every write), and is the simpler design
  — the `in_flight` set collapses to a boolean "did _we_ write recently?"
  question instead of a TTL-bounded multiset.
- The reply's recommended `∈ our set` framing is still safer than `token present`
  (defensive against multiple HA instances and any future change in Maytronics
  app behaviour) — keep it.

### E6 — no-op write — **AWS does not deduplicate; the "true no-op" is not happy-path-testable**

- Both writes received `/update/accepted` with K echoed and `version` increments
  (v958 → v960). AWS does not suppress redundant writes; every well-formed
  `shadow/update` is a new event.
- **However**, the PWS firmware cleared `desired` in ~287 ms after write #1 (v958
  → v959 `desired:null`). By the time write #2 fires 2 s later, `desired` is
  already null, so write #2 is `null → {cycleTime:82}` — a real change, not a
  no-op against the current desired. A genuine no-op test would require write
  #2 to land within ~200 ms of write #1, before the PWS clears — not happy-path
  feasible from a HA service call round-trip.
- **Implication for D2 — gate behaviour on redundant writes:**
  - A token-stamped gate would fire on every write the integration sends,
    redundant or not. If the integration writes `cycleTime=T` while
    `cleaningModes.<mode>` is already `T`, AWS will accept it and the gate
    will see the echo with our token — but there's nothing to react to (the
    reactive branch already short-circuits on `cleaningMode.mode` deltas).
  - **The integration should still avoid redundant writes** (deduplicate at the
    call-site) — both to reduce AWS throttle pressure (HARD-09's 429 territory)
    and to keep the shadow `version` from incrementing for no functional reason.
    This is a hygiene point, not a SPIKE-02-blocking requirement.
- **BUG-14 (#48)** open thread: the user re-selecting the already-active mode
  via the app does **not** trigger a redundant-mode `/update/accepted` echo in
  the same way (the app may itself dedupe before publishing). E6 cannot speak to
  the app's behaviour; that's an app-side optimisation outside our control.

### E4 — rejected echo (HARD-09's positive path) — **PASS**

- The `/update/rejected` body is `{"code":409,"message":"Version conflict","clientToken":"e678257b…6a71"}`
  — **our token is echoed on the reject path** (RTT 48 ms — fastest of the session,
  AWS rejects synchronously).
- The integration's `aws_client.py:413-416` does emit a `WARNING` for this rejected,
  which is the HARD-09 pathology — but the discriminator is now in hand:
  - gate the `WARNING` on `rejected.clientToken ∈ in_flight` → keeps the
    `WARNING` for our own rejected (legitimately interesting: our write was bad);
  - foreign rejected (the device's boot-time 429 from HARD-09 #66) carries no
    token, falls through to `debug`. HARD-09 dissolves.

### E7 — atomic combined `{mode, cycleTime}` write isolation — **FAIL on (c): firmware ignores `cycleTime` bundled with a `mode` change**

The E7 charter (issue #70 comment 4743131266) predicted PASS would unlock
removing the BUG-08 reactive chain. The empirical outcome is the
explicitly anticipated FAIL: **the firmware applied `mode=all` from the
combined `desired` but ignored the `cycleTime=75` field that travelled
with it.**

- **(a) AWS atomic accepted: ✅** — `/update/accepted` v964 echoes the full
  combined `desired` (both `cleaningMode.mode=all` and
  `cycleInfo.cycleTime=75`) with our token (RTT 60 ms). The shadow service
  treats the combined write atomically.
- **(b) Reactive branch absent: ✅** — the probe removed the `:456-465`
  `Set cycle time` chain; the captured log shows no `Set cycle time` /
  `[SPIKE-02] desired write` line between our publish at 16:51:01.375 and
  the `vacuum.pause` at 16:52:09.936. The integration emitted **zero**
  reactive writes — confirmed clean.
- **(c) End-to-end cycleTime delivered: ❌** — first device `reported` at
  16:51:03.151 (v966) shows `cycleInfo.cleaningMode = {mode:"all",
cycleTime:150}`. The device started a fresh cycle on `mode=all` with
  `cycleTime = 150`, NOT our 75.

**Where does the 150 come from? — operator-confirmed mechanical
explanation.** 150 is the user's HA-stored value for the "Cycle Complet"
duration (= `cycle_time_all`). The integration's `number` entity for that
mode pushed 150 into the shadow's `cleaningModes.all` slot on HA startup
(our deploy restart at 16:50:05); by the time E7 fired at 16:51:01,
`cleaningModes.all` was already 150, and the firmware used that value as
the per-mode default when starting the new `mode=all` cycle.

The combined-`desired` `cycleInfo.cycleTime=75` field was acked
(`desired:null` cleared it) but **silently discarded by the firmware
during mode-change application**. The firmware's contract appears to be:

> **`cycleTime` in `cycleInfo` is treated as an active-cycle override
> applied to an already-running mode. When `cleaningMode.mode` is changed
> in the same `desired` document, the firmware processes the mode change
> first, starts a new cycle using `cleaningModes.<new_mode>` as the
> duration, and ignores any sibling `cycleInfo.cycleTime` field for that
> cycle.** The flat `cycleTime` field of E2 hits the same "no anchor"
> rule.

This is consistent with D4 action 2's observation: there, the integration
sent `cycleTime=120` as a **separate write 1 s AFTER** the app's
`mode=floor` write, and that one **did** land in
`reported.cycleInfo.cleaningMode.cycleTime` (60 → 120). Sequencing works;
bundling does not.

**Implication for D2: the BUG-08 reactive chain MUST be kept, not
removed.** Its mode-then-`sleep(1)`-then-`cycleTime` shape is the
sequenced write the firmware requires. The "atomic combined as the new
primitive" plank of the [E7 charter](https://github.com/raouldekezel/dolphin-robot/issues/70#issuecomment-4743131266)
and of the [pre-D2 hand-off](https://github.com/raouldekezel/dolphin-robot/issues/70#issuecomment-4742855181)
is retracted. The only legitimate fix to the BUG-08 chain is replacing
the blocking `time.sleep(1)` on the awscrt thread with a non-blocking
wait (e.g. `hass.loop.call_later` / `asyncio.sleep` on the HA event loop,
or — better — a one-shot subscriber on the next `accepted` with
`desired.cleaningMode.mode` cleared, since that signals the firmware
applied the mode and is ready for the cycleTime write).

## Open questions for D2 (corrected by E7)

- **Token shape — per-session UUID.** E5 demonstrates AWS echoes K
  opaquely; mint **one UUID4 at integration start, reuse it on every
  write for the process lifetime**. The `_in_flight` predicate is
  effectively `event.clientToken == self._our_token` — boolean, no TTL
  needed for provenance (TTL matters separately for the dropped-publish
  hygiene, but not for the gate's correctness).
- **HARD-09 — gate `WARNING` on `rejected.clientToken == self._our_token`**
  at `aws_client.py:413-416`. Foreign rejected (the device's boot-time
  429 from #66) carries no token → falls through to `debug`. E4 PASS is
  the empirical foundation.
- **BUG-08 — keep the reactive chain at `:456-465`; replace `sleep(1)`
  with a non-blocking wait.** The chain's logic was already aligned with
  the operator-decided "launcher picks the duration" semantics
  ([memory `feedback_dolphin_bug08_launcher_pick_semantics`](https://github.com/raouldekezel/dolphin-robot/issues/17)) —
  E7 now also makes it _technically_ required (no atomic substitute
  exists). The remaining concern is purely the awscrt-thread sleep.
  Replace it with:
  - `hass.loop.call_later(1.0, self._set_cycle_time, mode)` from the
    callback thread (simplest), OR
  - a one-shot subscriber on the next `/update/accepted` where the
    device's `reported.cleaningMode.mode == mode` (event-driven, no
    fixed delay), OR
  - keep firing immediately and accept that ~50% of bursts may need a
    retry (riskier — the device sometimes needs the gap).
- **No atomic combined-write primitive.** E7 retracts this option.
  Mode and cycleTime must be sent as two sequential desired writes; the
  combined shape is silently lossy at the firmware level for cycleTime.
- **Dedup at the call site (hygiene, not blocker).** Skip the write if
  the desired value already matches `reported.<...>`. Reduces AWS
  throttle pressure and avoids spurious `version` increments.
- **Out of scope of D2, follow-up sessions:**
  - **E2 re-test** with a mode change immediately preceding the flat
    `cycleTime` write to confirm the "active-cycle override" reading
    above. Not blocking — D2's design uses sequenced writes anyway.
  - **Narrow the `shadow/#` wildcard subscription** to the explicit
    reserved topics (D1 §3). Hygiene, separate ticket.

## D4 attribution correction _(follow-up to PR #71)_

The D4 findings table on PR #71 attributes the `desired:null` events on
`/update/accepted` to _"AWS service (ACK-driven cleanup of vN)"_. **E3b
checkpoint (b) settles this empirically: zero `desired:null` arrived during
the 102-second PWS-offline window, two arrived ~3 s after the PWS came back
online.** The author is the PWS firmware (not the AWS service).

The PR #71 `findings.md` rows that need adjusting:

- Action 1: v858 (12:46:24.962), v860 (12:46:25.122) — currently "AWS service (ACK-driven cleanup …)" — should read **"PWS firmware (`desired:null` clearing of vXXX)"**.
- Action 2: v867 (12:49:52.995), v869 (12:49:53.400), v873 (12:50:25.897) — same correction.

The PR #71 PR body's "Foreign events without token" tally is unaffected (the
13 still hold; only their per-row provenance label changes). The PR-level
TL;DR/Verdicts are also unaffected.

Action: post a follow-up comment on PR #71 referencing this E3b finding, and
file a small docs-only PR (`docs/fix-diag-d4-desired-null-attribution`) to
patch the table — keeping the original D4 PR closed as merged.

## Refs

- Spike: [#70 SPIKE-02](https://github.com/raouldekezel/dolphin-robot/issues/70)
- D1 — mechanisms study: [comment 4735328308](https://github.com/raouldekezel/dolphin-robot/issues/70#issuecomment-4735328308)
- D4 — test plan: [comment 4735499483](https://github.com/raouldekezel/dolphin-robot/issues/70#issuecomment-4735499483) ; execution: [comment 4741268132](https://github.com/raouldekezel/dolphin-robot/issues/70#issuecomment-4741268132)
- D5 — existing-trace examination: [comment 4735450675](https://github.com/raouldekezel/dolphin-robot/issues/70#issuecomment-4735450675)
- ACK-concern: [comment 4741484253](https://github.com/raouldekezel/dolphin-robot/issues/70#issuecomment-4741484253) ; reply: [comment 4741909560](https://github.com/raouldekezel/dolphin-robot/issues/70#issuecomment-4741909560)
- Pre-D2 experiment battery (E1–E4): [comment 4741912816](https://github.com/raouldekezel/dolphin-robot/issues/70#issuecomment-4741912816)
- Pre-D2 addendum (E5+E6): [comment 4742390479](https://github.com/raouldekezel/dolphin-robot/issues/70#issuecomment-4742390479)
- D4 diag session: [`docs/diag/2026-06-18_spike-02_clienttoken-echo/`](../2026-06-18_spike-02_clienttoken-echo/findings.md) (PR #71)
- Probe branch: `patches/spike-02-pre-d2` (commit `d3038aa`) — kept on the fork for traceability, do not merge.
- Related issues: [HARD-09 #66](https://github.com/raouldekezel/dolphin-robot/issues/66) (rejected gate), [BUG-08 #17](https://github.com/raouldekezel/dolphin-robot/issues/17) (reactive chain to gate + combined write replaces sequencing), [BUG-13 #47](https://github.com/raouldekezel/dolphin-robot/issues/47) (mode-change-starts-cycle, observed in E1), [BUG-14 #48](https://github.com/raouldekezel/dolphin-robot/issues/48) (same-mode same-duration corner — E6 commentary).
