# SPIKE-02 — HARD-09 proven in vivo via deliberate provocation (E4-style)

## TL;DR

[HARD-09 (#66)](https://github.com/raouldekezel/dolphin-robot/issues/66)
is now PROVEN empirically on v1.0.26b3-raoul.8. Three deliberate
`/update/rejected` events were captured by stamping a stale top-level
`version=1` on writes with three distinct provenance shapes; the
integration's gate routed them exactly as designed:

- **A — clientToken matches `self._our_token`** → `WARNING` ✅
- **B — clientToken is a foreign hex** → `DEBUG` (silenced) ✅
- **C — no clientToken field at all** → `DEBUG` (silenced) ✅

This closes the "in-vivo classification" gap left open by the
2026-06-18 [validation session](../2026-06-18_spike-02_validation/findings.md)
(which observed zero rejected events). The HARD-09 gate works on real
AWS-side rejections, not just on unit-test stubs.

## Context

- **Date:** 2026-06-18 23:49–23:50 CEST
- **Robot:** Maytronics Dolphin S2000 (Nono 2), `robotType:"S4"`.
- **Integration:** `v1.0.26b3-raoul.8` ([release notes](https://github.com/raouldekezel/dolphin-robot/releases/tag/v1.0.26b3-raoul.8)),
  with a throwaway provocation probe stacked on top to expose a
  `mydolphin_plus.spike_publish(payload, client_token, top_level_version)`
  HA service. The probe lives on the throwaway branch
  `patches/spike-02-hard-09-provoke` (`f5d2845`), deployed via
  `docker cp` and rolled back live to deploy HEAD before this session
  was committed.
- **HA:** 2026.1.3 (container `hass` on intel-nuc, `network_mode: privileged`).
- **Debug logging:** persistent `…managers.aws_client: debug` from
  `configuration.yaml`.

The probe is intentionally minimal: a single helper that bypasses
`_send_desired_command`'s auto-stamping and lets the caller choose what
to stamp (or omit). The sentinel `client_token="ours"` substitutes
`self._our_token` (so we can construct the only positive-path test
without leaking the token out of the integration's process); any other
non-empty string is used as-is (negative path with a foreign token);
empty string omits the `clientToken` field entirely (negative path with
no token, the device's boot-429 shape).

## Actions taken

1.  `01_provocations-A-B-C.mqtt.log` — three back-to-back service calls
    to `mydolphin_plus.spike_publish`:
    - **A** — `payload={"cleaningMode":{"mode":"short"}}`, `client_token="ours"`, `top_level_version=1`. Forces `409 Version conflict`; rejected payload carries our token.
    - **B** — same `payload`, `client_token="deadbeefcafefacefeedfacebadc0ffeefake0001"`, `top_level_version=1`. Forces 409; rejected carries the foreign token verbatim.
    - **C** — same `payload`, `client_token=""` → field omitted, `top_level_version=1`. Forces 409; rejected has no `clientToken` field at all (the shape the device's boot-429 takes — empirically confirmed via SPIKE-02 D5 on 107 captured payloads).

## Timeline

All times CEST (UTC+02:00). Tokens redacted in the report; the raw
log keeps them for evidence (32-hex strings, no PII).

| t (CEST)         | Source       | Event                                                                                                                                            | level            | gate decision               |
| ---------------- | ------------ | ------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------- | --------------------------- |
| 23:49:58.439     | probe        | `[SPIKE-02 spike_publish]` `clientToken=e79cf60708…d8d7` `version=1` `payload={cleaningMode.mode=short}`                                         | INFO             | —                           |
| 23:49:58.441     | publish      | `Published message #9 to …/shadow/update, Data: { …, "clientToken": "e79cf60708…d8d7", "version": 1 }`                                           | INFO             | —                           |
| **23:49:58.490** | AWS rejected | Topic: `…/shadow/update/rejected`                                                                                                                | DEBUG            | —                           |
| **23:49:58.490** | gate (A)     | `Rejected message for …/shadow/update/rejected, Message: {"code":409,"message":"Version conflict","clientToken":"e79cf60708…d8d7"}`              | **`WARNING`** ✅ | matches `_our_token` → WARN |
| 23:50:02.457     | probe        | `[SPIKE-02 spike_publish]` `clientToken=deadbeefcafefacefeedfacebadc0ffeefake0001` `version=1`                                                   | INFO             | —                           |
| 23:50:02.459     | publish      | `Published … "clientToken": "deadbeef…fake0001", "version": 1`                                                                                   | INFO             | —                           |
| **23:50:02.515** | AWS rejected | Topic: `…/shadow/update/rejected`                                                                                                                | DEBUG            | —                           |
| **23:50:02.516** | gate (B)     | `Rejected message for …/shadow/update/rejected (not ours), Message: {"code":409,"message":"Version conflict","clientToken":"deadbeef…fake0001"}` | **`DEBUG`** ✅   | token ≠ ours → DEBUG        |
| 23:50:06.473     | probe        | `[SPIKE-02 spike_publish]` `clientToken=None` `version=1`                                                                                        | INFO             | —                           |
| 23:50:06.475     | publish      | `Published … {"state": …, "version": 1}` (NO clientToken field)                                                                                  | INFO             | —                           |
| **23:50:06.529** | AWS rejected | Topic: `…/shadow/update/rejected`                                                                                                                | DEBUG            | —                           |
| **23:50:06.529** | gate (C)     | `Rejected message for …/shadow/update/rejected (not ours), Message: {"code":409,"message":"Version conflict"}` (no clientToken in body)          | **`DEBUG`** ✅   | no token → DEBUG            |

Round-trip latencies: A 51 ms, B 58 ms, C 56 ms. Consistent with the
earlier E4 measurement (48 ms on `v1.0.26b3-raoul.7` + probe). AWS
rejects synchronously.

## Findings

### HARD-09 (#66) — PROVEN

- **A (ours):** the gate sees `accepted.clientToken == self._our_token` →
  emits the original `WARNING Rejected message for …` line at level
  `WARNING`. The integration tells the operator "your write failed",
  which is the actionable signal HARD-09 explicitly wants to keep.
- **B (foreign token):** the gate sees a token that does NOT match →
  emits `Rejected message for … (not ours), …` at level `DEBUG`. Any
  other HA instance sharing the same Maytronics account, or any future
  app rejection (none observed today on this Maytronics SDK), is
  silenced at user-log level.
- **C (no token):** the gate falls through to `DEBUG` via the same
  branch — `payload_data.get(DATA_CLIENT_TOKEN)` returns `None`,
  `_event_is_ours` returns `False`. **This is the shape of the
  device's boot-time `429 Too Many Requests` documented in #66** (per
  D5: neither the app nor the PWS firmware stamps a `clientToken`). On
  the next nightly `Plug Nono coupé la nuit` cut, the device's boot
  429 will land in this exact case and be silenced.
- The integration emits the discriminating string `(not ours)` on the
  DEBUG path, so a log scan can distinguish "the gate correctly
  silenced a foreign rejection" from "no rejection happened at all".

The empirical demonstration goes one step beyond the unit-test
coverage in `tests/test_spike_02_only_react_to_ours.py`: those tests
mock `payload_data` as a Python dict, whereas this session shows the
full round-trip — our probe publishes a real shadow document, AWS's
managed Shadow service constructs the real `/rejected` payload, the
awscrt SDK delivers it, the integration parses and gates it. No
behaviour relies on a stub anywhere.

### Bonus — Maytronics REST transient outage observed mid-session

Between the probe deploy (23:46:25) and the first provocation
(23:47:08), a Maytronics `user-svc.b2c.svc:9056` `Connection refused`
caused the AWS client's `initialize()` to abort partway, leaving
`self._topic_data = None`. The probe's first three calls then errored
with `AttributeError: 'NoneType' object has no attribute 'update'`
inside `spike_publish.self._publish(...)` (no rejection observed
because nothing reached AWS). The integration's existing 1-minute
backoff (`reconnection attempt #1, waiting 1 minute(s) before retry`)
recovered cleanly at 23:48:29; the second round of provocations at
23:49:58 / 23:50:02 / 23:50:06 ran on the healthy connection. Not a
regression — Maytronics's REST endpoint had a transient hiccup,
visible because we restarted HA during it.

## Open questions

None. HARD-09 was the last open follow-up of the spike; with this
session it is empirically proven on the same level as SPIKE-02 (#70)
and BUG-08 (#17). The remaining items in the
[validation findings](../2026-06-18_spike-02_validation/findings.md)
were either already closed (validation of SPIKE-02 + BUG-08 in vivo)
or were sleep-refactor work tracked in
[[dolphin-bug08-launcher-pick-semantics]] and explicitly out of
scope.

## Refs

- Issue: [#66 HARD-09](https://github.com/raouldekezel/dolphin-robot/issues/66)
- Spike root: [#70 SPIKE-02](https://github.com/raouldekezel/dolphin-robot/issues/70)
- Release that shipped the gate: [`v1.0.26b3-raoul.8`](https://github.com/raouldekezel/dolphin-robot/releases/tag/v1.0.26b3-raoul.8)
- PR that implemented the gate: [#73](https://github.com/raouldekezel/dolphin-robot/pull/73)
- Unit tests covering the same predicate: [`tests/test_spike_02_only_react_to_ours.py`](https://github.com/raouldekezel/dolphin-robot/blob/deploy/tests/test_spike_02_only_react_to_ours.py)
- Earlier validation (HA + app cycles, no rejected observed): [`docs/diag/2026-06-18_spike-02_validation/`](../2026-06-18_spike-02_validation/findings.md)
- The reference SPIKE-02 D4 / pre-D2 sessions: [`docs/diag/2026-06-18_spike-02_clienttoken-echo/`](../2026-06-18_spike-02_clienttoken-echo/findings.md) and [`docs/diag/2026-06-18_spike-02_pre-d2/`](../2026-06-18_spike-02_pre-d2/findings.md)
- Provocation probe (throwaway): `patches/spike-02-hard-09-provoke` (`f5d2845`)
