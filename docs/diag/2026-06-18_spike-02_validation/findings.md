# SPIKE-02 validation — v1.0.26b3-raoul.8 live on the S2000

## TL;DR

Live end-to-end validation of [PR #73 / v1.0.26b3-raoul.8](https://github.com/raouldekezel/dolphin-robot/releases/tag/v1.0.26b3-raoul.8)
on the real S2000 immediately after the release was installed via HACS.
Two cycles back-to-back — one from HA, one from the Maytronics app —
prove the provenance gate behaves asymmetrically as designed:

- **SPIKE-02 (#70) — provenance signal: PROVEN.** Every HA-initiated
  desired write carries a `clientToken` and AWS echoes it on
  `/update/accepted`. App-initiated writes carry no token and are
  cleanly classifiable as foreign.
- **BUG-08 (#17) — gate behaviour: PROVEN.** The reactive `Set cycle
time` chain fires ~1 s after the HA-initiated mode change (with our
  token), and does NOT fire on the app-initiated mode change. The app's
  own `cycleTime=120` write at +2.3 s lands uncontested — "launcher
  picks the duration" is now an invariant.
- **HARD-09 (#66) — DEBUG-downgrade for foreign rejected:
  NOT YET PROVEN.** Zero `/rejected` events occurred in the validation
  window (the PWS was not power-cycled, so the device's boot-time `429
Too Many Requests` never fired). Needs a separate window covering
  either the next nightly `Plug Nono coupé la nuit` automation cut or a
  deliberate provocation. See the **Open questions** section.

## Context

- **Date:** 2026-06-18 19:27–19:31 CEST
- **Robot:** Maytronics Dolphin S2000 (Nono 2). `robotType:"S4"`,
  `pwsSwVersion:"11.0004"`, `muSwVersion:"9F88"`.
- **Integration:** `v1.0.26b3-raoul.8` (commit `b1bf747` on `deploy`),
  installed via HACS Redownload immediately before the test. First
  release with the `clientToken` provenance machinery.
- **HA:** 2026.1.3 (container `hass` on intel-nuc, `network_mode: privileged`).
- **Debug logging:** persistent `…managers.aws_client: debug` from
  `configuration.yaml` — confirmed by the `Payload: …` lines in both
  captures (only emitted at DEBUG).
- **Robot starts** in `pwsState=holdWeekly`, `robotState=notConnected`.
  The cycles are short (operator paused after a few seconds) and the
  motor is in the pool, umbilical connected.
- **HA-stored cycle times:** `cycle_time_all = 60`. The app's own
  picker is set to `120` (2 h, default Maytronics value on this
  account). T=60 (HA) ≠ T=120 (app) by design — any "120" appearing in
  the device's reported is unambiguously the app's value, any "60" is
  unambiguously HA's.

## Actions taken

1.  `01_ha-initiated-all-60.mqtt.log` — operator selected mode "Complet"
    on the HA vacuum card (mode `all`), then paused via the HA pause
    button ~60 s later. Captures the integration's outbound writes
    (`Set cleaning mode`, the reactive `Set cycle time`, `Set power
state`) and AWS's `/update/accepted` echoes.
2.  `02_app-initiated-all-120.mqtt.log` — operator selected mode
    "Complet" 2 h (120 min) on the MyDolphin Plus app, then stopped
    from the app ~30 s later. Captures every shadow event reaching the
    integration during the app cycle.

## Timeline

All times CEST (UTC+02:00). Tokens redacted to `<TOKEN>` (the same
32-hex string is reused across all our writes in a given session — that
is the per-session UUID4 minted in `AWSClient.initialize`, validated
behaviourally by SPIKE-02 E5).

### Action 1 — HA-initiated cycle

| t (CEST)     | Source                 | Event                                                                                                                                    | clientToken?       | Reaction                                                           |
| ------------ | ---------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- | ------------------ | ------------------------------------------------------------------ |
| 19:28:05.959 | HA                     | `Set cleaning mode, Desired: {'cleaningMode': {'mode': 'all'}}` (integration's own log line)                                             | —                  | —                                                                  |
| 19:28:05.961 | HA                     | `Published message #10 to …/shadow/update, Data: {"state": {"desired": {"cleaningMode": {"mode": "all"}}}, "clientToken": "<TOKEN>"}`    | **present (ours)** | this is what AWS will echo back                                    |
| (~19:28:06)  | AWS                    | `/update/accepted` echoes `desired.cleaningMode.mode=all` with our token                                                                 | **present (ours)** | integration sees own echo → BUG-08 gate True                       |
| 19:28:07.077 | Integration (reactive) | `Set cycle time, Desired: {'cycleInfo': {'cycleTime': 60}}` ← +1.118 s after the mode echo (the `sleep(1)` + processing)                 | —                  | proves the reactive chain still fires on HA-initiated mode changes |
| 19:28:07.095 | HA                     | `Published message #12 to …/shadow/update, Data: {"state": {"desired": {"cycleInfo": {"cycleTime": 60}}}, "clientToken": "<TOKEN>"}`     | **present (ours)** | HA's stored duration applied                                       |
| 19:29:05.040 | HA                     | `Set power state, Desired: {'systemState': {'pwsState': 'off'}}` (vacuum.pause)                                                          | —                  | —                                                                  |
| 19:29:05.044 | HA                     | `Published message #16 to …/shadow/update, Data: {"state": {"desired": {"systemState": {"pwsState": "off"}}}, "clientToken": "<TOKEN>"}` | **present (ours)** | stop with our token                                                |

### Action 2 — app-initiated cycle

| t (CEST)                                             | Source       | Event                                                                                                                         | clientToken? | Reaction                               |
| ---------------------------------------------------- | ------------ | ----------------------------------------------------------------------------------------------------------------------------- | ------------ | -------------------------------------- |
| 19:30:22.738                                         | App          | `/update/accepted` v995: `Payload: {"state":{"desired":{"cleaningMode":{"mode":"all"}}},…}`                                   | **absent**   | —                                      |
| 19:30:22.952                                         | PWS firmware | `/update/accepted` v996: `desired:null` (firmware clears desired after applying the mode)                                     | absent       | —                                      |
| **(no integration log lines between v995 and v998)** | —            | **No `Set cleaning mode` / `Set cycle time` / `Published message` line fires** ← the gate skipped the reactive chain.         | —            | **proves BUG-08 gate True on foreign** |
| 19:30:25.054                                         | App          | `/update/accepted` v998: `Payload: {"state":{"desired":{"cycleInfo":{"cycleTime":120}}},…}` ← the app pushed its OWN duration | **absent**   | —                                      |
| 19:30:25.419                                         | PWS firmware | v999: `desired:null`                                                                                                          | absent       | —                                      |
| 19:30:52.682                                         | App          | v1001: `Payload: {"state":{"desired":{"systemState":{"pwsState":"off"}}},…}` (app's stop button)                              | **absent**   | —                                      |
| 19:30:52.737                                         | AWS / device | v1001 reported: includes `cycleInfo.cleaningMode = {mode:"all", cycleTime:120}` — the device applied the app's 120            | —            | end-to-end: app's chosen duration wins |
| 19:30:52.892                                         | PWS firmware | v1002: `desired:null`                                                                                                         | absent       | —                                      |

## Findings

### SPIKE-02 (#70) — provenance signal: **PROVEN**

- Action 1 captures shows the literal shape of every outbound HA-initiated
  document: `{"state": {…}, "clientToken": "<32-hex>"}`. The stamping happens
  at the `_send_desired_command` chokepoint as the patch intended.
- The same `<TOKEN>` value is reused across the four publishes of the
  HA cycle (mode write + reactive cycleTime + pause + an internal
  systemState write) — i.e. the per-session UUID4 from `AWSClient.initialize`,
  unchanged for the integration's process lifetime, as E5 predicted.
- Foreign action 2 captures show `desired` payloads from the app and
  `desired:null` payloads from the PWS firmware all arriving without a
  `clientToken` field — exactly the empirical pattern D5 inferred and
  this validation reconfirms live on raoul.8.

### BUG-08 (#17) — gate behaviour: **PROVEN**

- **HA path (action 1):** the integration emits `Set cycle time` at
  19:28:07.077 — **1.118 s** after the mode echo at ~19:28:05.96 (one
  `sleep(1)` + ~0.1 s of processing). The reactive chain fires for our
  own mode change, as designed.
- **App path (action 2):** the app writes `mode=all` at 19:30:22 and
  `cycleTime=120` at 19:30:25 (the app's own `Set cycle time`
  equivalent, +2.3 s after its mode write). Between those two events,
  **the integration emits no `Set cleaning mode`, no `Set cycle time`,
  no outbound `Published message`** — the reactive chain skipped the
  app's mode echo because it carried no token. The app's `cycleTime=120`
  therefore arrives uncontested, and the device's `reported.cycleInfo.cleaningMode`
  at 19:30:52 carries `mode=all, cycleTime=120` — the app's value, not
  HA's stored `60`.
- **Net:** "launcher picks the duration" is now an **invariant** of the
  design — the originator of the mode change is the only writer of the
  cycle's duration. Pre-raoul.8, this outcome held by accident via a
  last-write-wins race; post-raoul.8, no race.

### HARD-09 (#66) — DEBUG-downgrade: **NOT YET PROVEN**

The validation window contains **zero `/rejected` events** (`grep -c
"shadow/update/rejected"` on both `.mqtt.log` files: 0). The PWS was
not power-cycled, so the canonical foreign-rejection trigger — the
device's post-boot `429 Too Many Requests` documented in #66 — never
fired. Likewise, the integration didn't publish anything stale enough
to be rejected by AWS, so no our-rejection either.

The HARD-09 gate is unit-tested in
`tests/test_spike_02_only_react_to_ours.py` (three behavioural tests
covering matching token → WARN, no token → DEBUG, foreign token →
DEBUG), so the **mechanism** is verified. What the unit tests can't
verify is that, in vivo, the gate's _predicate_ correctly classifies
the device's real boot 429 — that requires observing the device's
actual rejected payload and checking it doesn't carry our token.

### Bonus — no log noise during the test

Across both captures (~30 s of HA cycle + ~30 s of app cycle + 3 min
of idle `shadow/get` polls in between), zero `Rejected message`
`WARNING` lines. The previous noise on `holdWeekly`/`notConnected`
states (78 hits on install day, 28 per reinstall before raoul.8) does
not reappear post-fix in this window. Not a proof of HARD-09 — that
needs a foreign rejected to be observed — but a useful "no regression"
data point.

## Open questions

### HARD-09 (#66) — how to close the loop empirically

Two viable paths, both small:

1. **Wait for the next nightly `automation.piscine_plug_nono_coupe_la_nuit`
   cut**. The next morning's reconnect should fire the device's boot
   429 documented in #66. Capture the `/rejected` payload and verify
   (a) it carries no `clientToken`, (b) the integration logs it at
   DEBUG, not WARN.
2. **Provoke deliberately**, the same way SPIKE-02 E4 did: a one-shot
   throwaway publish with a stale top-level `version` to force a 409,
   plus a one-shot publish with a _foreign_ token (or no token, via a
   tweaked probe) to verify the gate's negative path. ~30 minutes of
   work, no robot manipulation needed.

Either path closes HARD-09 with the same confidence the other two
deliverables now have. The current diag session does not block on it.

### Other follow-ups (already tracked)

- The `time.sleep(1)` in the BUG-08 chain stays. The "characterise the
  1 s gap before removing" follow-up lives in
  [[dolphin-bug08-launcher-pick-semantics]] memory and is unrelated to
  this validation.

## Refs

- Release: [`v1.0.26b3-raoul.8`](https://github.com/raouldekezel/dolphin-robot/releases/tag/v1.0.26b3-raoul.8)
- PR shipping the fix: [#73](https://github.com/raouldekezel/dolphin-robot/pull/73)
- Closed by this fix: [#70 SPIKE-02](https://github.com/raouldekezel/dolphin-robot/issues/70),
  [#66 HARD-09](https://github.com/raouldekezel/dolphin-robot/issues/66),
  [#17 BUG-08](https://github.com/raouldekezel/dolphin-robot/issues/17)
- Prior empirical sessions: [`docs/diag/2026-06-18_spike-02_clienttoken-echo/`](../2026-06-18_spike-02_clienttoken-echo/findings.md)
  (D4 — first proof of the AWS echo) and
  [`docs/diag/2026-06-18_spike-02_pre-d2/`](../2026-06-18_spike-02_pre-d2/findings.md)
  (pre-D2 + E7 — every blocking uncertainty addressed before the design)
