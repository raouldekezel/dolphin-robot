# SPIKE-02 pre-D2 — close every sequencing / atomicity / offline / ack uncertainty before proposing D2

## TL;DR

**Six PASS, one mostly-PASS-one-caveat (E2 device-side slot inconclusive), one
unexpected sub-finding (BUG-08's reactive chain breaks the E1 atomic-write
unlock unless D2 also gates the reactive branches on the new
provenance signal).** `desired:null` is empirically PWS-firmware-authored
(E3b: zero of them appeared during a 102-second PWS-offline window;
one arrived ~3 s after the SPS-04 plug came back on). AWS echoes our
injected `clientToken` opaquely on `/update/accepted` and on
`/update/rejected`, and treats every well-formed update as a new
event (no no-op deduplication). The `clientToken` pattern surveyed
in D1 is viable — and the per-session UUID variant is viable too.

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

## Open questions for D2

- **Token shape — per-session UUID.** E5 demonstrates AWS echoes K opaquely;
  use **one UUID4 minted at integration start, reused across all writes for the
  process lifetime**, instead of per-call UUIDs. Simpler `in_flight` set
  (effectively boolean: "did we write recently?"), one less moving part. Keep
  the `∈ our set` framing rather than "token present" for safety.
- **Reactive-branch gates.** Both `aws_client.py:413-416` (HARD-09) and
  `aws_client.py:456-465` (BUG-08) must gate on
  `accepted.clientToken ∈ in_flight` (resp. `rejected.clientToken ∈ in_flight`).
  E1 + E4 are the empirical foundation. The exact predicate is
  `if (event.clientToken is not None and event.clientToken == self._our_token):
     do_not_react()`.
- **Atomic combined writes — the new primitive for mode+cycleTime.** Once the
  reactive branch is gated, the integration should write `mode` + `cycleTime`
  in ONE `desired` document (sibling subtrees) on every mode change instead of
  the current "mode then `sleep(1)` then cycleTime" sequence. E1 proves the
  combined shape is honoured by AWS atomically. Side benefits: no awscrt-thread
  `time.sleep(1)` (closes BUG-08's dangling thread concern too), correct
  behaviour with the robot disconnected (the buffered combined desired is
  applied as one on reconnect — E3b shape), one accepted to dedupe on instead
  of two.
- **Dedup at the call site (hygiene, not blocker).** Skip the write if the
  desired value already matches the current `reported.<...>` value. Reduces
  AWS throttle pressure and avoids spurious `version` increments.
- **Out of scope of D2, follow-up sessions:**
  - **E2 re-test** with a mode change immediately preceding the flat
    `cycleTime` write — settles the "mode-relative vs queued vs discarded"
    interpretation. Not blocking — combined writes (E1 shape) sidestep the
    flat-cycleTime path anyway.
  - **Narrow the `shadow/#` wildcard subscription** to the explicit reserved
    topics (D1 §3). Hygiene, separate ticket.

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
