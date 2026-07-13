# HARD-03 — LED entity value jumps before the AWS ACK, robot offline (`v1.0.26b3-raoul.19`)

## TL;DR

Live confirmation of [HARD-03 (#24)](https://github.com/raouldekezel/dolphin-robot/issues/24) on the S2000 while the robot is `notConnected` (AWS broker is up). Setting `number.<robot>_intensite_led` from 100 → 25 in HA:

- **T + 2 ms** — HA entity already reads 25.0 (`base_entity: Data for … : {'state': 25.0}`), **before** the AWS publish is acknowledged and 145 ms **before** `shadow/update/accepted` arrives.
- The `Set led intensity` INFO log carries `{'led': {'ledEnable': True, 'ledIntensity': 25.0, 'ledMode': 1}}` — the shared-reference mutation described in the ticket is what wrote the 25.0 into `self.data[DATA_SECTION_LED]` and propagated it to the entity via the same-tick coordinator refresh (0.001 s, MainThread).
- The robot never receives the delta (offline). AWS accepts and stores `desired.led.ledIntensity = 25.0` (`shadow/update/accepted` v2499 at T + 145 ms) and answers a follow-up `shadow/get/accepted` at **T + 163 ms** carrying `reported.led.ledIntensity = 100` unchanged. The entity, however, is not re-derived until **T + 10 s** (`base_entity: Data for … {'state': 100}`) — this log excerpt does not establish what fills the gap between the shadow response arriving and the entity being updated.
- At **T + 14 s** another `update/accepted` v2500 clears `desired = {}`. The initiating client is not visible in this log excerpt (no `clientToken` on the accepted payload, no publish visible at that timestamp on our side).
- Under real broker degradation (home network / cloud outage), the phantom value can persist until fresh shadow data arrives, the integration is reloaded, or a reconnect replaces the mutated local state.

Reproduces the pattern for all three LED entities (`light.<robot>_led`, `number.<robot>_intensite_led`, `select.<robot>_mode_led`), since `set_led_mode` / `set_led_intensity` / `set_led_enabled` all funnel through the same `_get_led_settings` helper (`aws_client.py:639-652, 731-743`).

## Context

Operator state at trigger time:

- Fork: `raouldekezel/dolphin-robot`, `deploy` branch, HACS-installed `v1.0.26b3-raoul.19`.
- Robot: S2000, `sensor.nono_2_etat_du_robot = notconnected` since 12:27 UTC.
- Broker: `binary_sensor.nono_2_broker_aws = on` (cloud side still healthy).
- Debug: `custom_components.mydolphin_plus` at DEBUG level via `POST /api/services/logger/set_level` at 12:45 UTC.
- Trigger: manual change of `number.nono_2_intensite_led` from 100 to 25 via the HA UI tile at 14:46:55 CEST (12:46:55 UTC).

The offline-robot-but-online-broker scenario is exactly the case elad-bar asked for on the ticket ("Can you pls explain the use case you experienced …"): AWS accepts and stores the `desired` shadow, but the delta is never applied because the thing is not connected.

## Timeline

Aligned from `aws_client.mqtt.log` (log lines) and `state_history.tsv` (recorder). Timezone is CEST for the log column (Europe/Brussels), UTC on the recorder column.

|            Δt | UTC              | Event                                                                                   | Source                     |
| ------------: | ---------------- | --------------------------------------------------------------------------------------- | -------------------------- |
|             0 | 12:46:55.615     | `Set led intensity, Desired: {'led': {'ledEnable': True, 'ledIntensity': 25.0, …}}`     | aws_client.mqtt.log:1      |
|        + 0 ms | 12:46:55.615     | Publish `$aws/things/<thing-name>/shadow/update` (MainThread)                           | aws_client.mqtt.log:2      |
|        + 1 ms | 12:46:55.616     | Publish `$aws/things/<thing-name>/shadow/get` (MainThread)                              | aws_client.mqtt.log:4      |
|        + 1 ms | 12:46:55.616     | Coordinator refresh runs synchronously in the same MainThread frame                     | aws_client.mqtt.log:5      |
|    **+ 2 ms** | **12:46:55.617** | **`Data for number_<thing-id>_nono_2_intensite_led: {'state': 25.0}`**                  | **aws_client.mqtt.log:6**  |
|        + 2 ms | 12:46:55.617     | Recorder ingests `state = 25.0`                                                         | state_history.tsv:3        |
|        + 5 ms | 12:46:55.620     | AWS SDK `packet_id: 67` confirmed                                                       | aws_client.mqtt.log:7      |
|      + 145 ms | 12:46:55.760     | `shadow/update/accepted` v2499 (`desired.led.ledIntensity=25.0`, `clientToken` matches) | aws_client.mqtt.log:13-14  |
|      + 149 ms | 12:46:55.764     | `shadow/update/delta` (AWS keeps the delta pending — the thing is not connected)        | aws_client.mqtt.log:15     |
|      + 163 ms | 12:46:55.778     | `shadow/get/accepted` (`reported.led.ledIntensity = 100` — unchanged)                   | aws_client.mqtt.log:18     |
|      + 1.17 s | 12:46:56.781     | Debounced MQTT refresh runs                                                             | aws_client.mqtt.log:19     |
|     + 10.00 s | 12:47:05.619     | Coordinator refresh                                                                     | aws_client.mqtt.log:20     |
| **+ 10.00 s** | **12:47:05.620** | **`Data for number_<thing-id>_nono_2_intensite_led: {'state': 100}`**                   | **aws_client.mqtt.log:21** |
|     + 10.00 s | 12:47:05.621     | Recorder ingests `state = 100`                                                          | state_history.tsv:4        |
|     + 14.25 s | 12:47:09.869     | `shadow/update/accepted` v2500 (`desired = {}` — initiating client not visible in log)  | aws_client.mqtt.log:25-26  |

Two things worth pinning:

1. The `Data for … {'state': 25.0}` log line at **T + 2 ms** proves the entity read the mutated value before _any_ cloud round-trip completed. Nothing else in the code path writes to `self.data[DATA_SECTION_LED]` in that window — the mutation can only come from `request_data[key] = value` at `aws_client.py:739` operating on the shared reference returned by `self.data.get(DATA_SECTION_LED, default_data)`.
2. The rollback at **T + 10 s** is not a shadow delta from the robot (offline throughout). The `shadow/get/accepted` carrying `reported.led.ledIntensity = 100` arrives much earlier (T + 163 ms), and this log excerpt does not establish what fills the ~10 s gap between that response and the entity re-derivation. On a healthy robot the same rollback would come from the `reported` update the robot pushes _after_ it applies the delta; here the same mechanism happens to mask the bug by short-circuiting on the stale `reported`.

## Chain of causation (in code)

`aws_client.py:731-743`:

```python
def _get_led_settings(self, key, value):
    default_data = {
        DATA_LED_ENABLE: DEFAULT_ENABLE,
        DATA_LED_INTENSITY: DEFAULT_LED_INTENSITY,
        DATA_LED_MODE: LED_MODE_BLINKING,
    }

    request_data = self.data.get(DATA_SECTION_LED, default_data)  # → same reference as self.data[LED]
    request_data[key] = value                                     # → mutates self.data[LED][key]

    data = {DATA_SECTION_LED: request_data}
    return data
```

1. `self.data.get(DATA_SECTION_LED, default_data)` returns the dict **stored** in `self.data`, not a copy. When `DATA_SECTION_LED` is already present (always after the first shadow refresh), `request_data` **is** `self.data[DATA_SECTION_LED]`.
2. `request_data[key] = value` mutates `self.data[DATA_SECTION_LED][key]` in place. The "payload to send" and the "HA-visible state" are the same object.
3. The very next coordinator refresh (same MainThread frame, 0.001 s later) walks `self.data`, sees `led.ledIntensity = 25.0`, and pushes it to the entity via `base_entity.py`.
4. AWS ACK arrives 145 ms later. It cannot undo anything — the eager write has already committed to HA state.
5. The eventual rollback is the shadow reading `reported` (stale on the robot side, since the robot is offline). It does _not_ verify that the delta was applied; it only reflects whatever `reported` currently holds. On a fully-online robot the same rollback would arrive when the robot updates `reported` after applying the command — but during that window (network latency + robot processing time) the entity still lies.

## Impact on the three LED entities

`aws_client.py` calls `_get_led_settings` from three service methods:

- `set_led_mode` (line 639) → mutates `self.data[LED][DATA_LED_MODE]`
- `set_led_intensity` (line 645) → mutates `self.data[LED][DATA_LED_INTENSITY]`
- `set_led_enabled` (line 651) → mutates `self.data[LED][DATA_LED_ENABLE]`

So the same pathology is expected on:

- `number.<robot>_intensite_led` — verified in this session.
- `select.<robot>_mode_led` — same code path, not re-verified.
- `light.<robot>_led` — same code path, not re-verified.

## Scope check — is the pattern anywhere else?

`grep -n 'self.data.get(DATA_SECTION' custom_components/mydolphin_plus/managers/aws_client.py` returns exactly one hit, the buggy one at line 738. Every other `_set_*` / `_get_*` builder in `aws_client.py` constructs the payload dict from scratch. Fix is chirurgical: one line, three entities.

## Fix (proposed)

Shallow-copy is sufficient — the leaf values are scalars (`bool`, `int`, `str`), no nested mutability:

```python
request_data = dict(self.data.get(DATA_SECTION_LED, default_data))
request_data[key] = value
```

or explicit `.copy()`. No test scaffold change beyond a regression that asserts `self.data[DATA_SECTION_LED]` is untouched immediately after `set_led_intensity` returns and before any shadow ACK arrives.

## Ready-to-post reply for elad-bar on #24

> Live reproduction on my S2000 (offline, `robot_state=notConnected`, but AWS broker still up):
>
> - HA entity `state` jumps 100 → 25.0 at T + 2 ms after the service call — **before** `shadow/update/accepted` is received (T + 145 ms). The eager write can only come from mutating the shared reference returned by `self.data.get(DATA_SECTION_LED)` at `aws_client.py:738`; nothing else in the code path writes state before the ACK.
> - Robot is unreachable throughout, so `reported.led.ledIntensity` stays at 100. `shadow/get/accepted` carrying that unchanged value arrives at T + 163 ms, but the entity is not re-derived to 100 until T + 10 s (the excerpt does not establish what fills the gap). Either way, the "correction" is not a shadow delta from the robot — it's the poll masking the mutation.
> - Under real broker degradation (home network / cloud outage), the phantom value can persist until fresh shadow data arrives, the integration is reloaded, or a reconnect replaces the mutated local state.
>
> Timeline attached (12:46:55.617 → 25.0, 12:47:05.621 → 100). Fix as originally proposed: `request_data = dict(self.data.get(DATA_SECTION_LED, default_data))` — leaf values are scalars, so shallow copy suffices.

## Files in this diag

- `findings.md` — this document.
- `aws_client.mqtt.log` — 35 debug lines from `home-assistant.log`, `14:46:55` → `14:47:15` CEST.
- `state_history.tsv` — 3-point extract of `number.nono_2_intensite_led` from the HA recorder.

## Refs

- Issue: [HARD-03 (#24)](https://github.com/raouldekezel/dolphin-robot/issues/24)
- Doctrinal cousin: [SPIKE-02 (#70)](https://github.com/raouldekezel/dolphin-robot/issues/70) — "only act on events the integration initiated"; here the variant is _state mutation_, not _shadow callback_.
- Companion IT note: `Home Assistant - Dolphin S2000.md` (HARD-03 section).
