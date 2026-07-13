# HARD-03 — LED entity value jumps before the AWS ACK, robot offline (`v1.0.26b3-raoul.19`)

## TL;DR

Live confirmation of [HARD-03 (#24)](https://github.com/raouldekezel/dolphin-robot/issues/24) on the S2000 while the robot is `notConnected` (AWS broker is up). Setting `number.<robot>_intensite_led` from 100 → 25 in HA:

- **T + 2 ms** — HA entity already reads 25.0 (`base_entity: Data for … : {'state': 25.0}`), **before** the AWS publish is acknowledged and 145 ms **before** `shadow/update/accepted` arrives.
- The `Set led intensity` INFO log carries `{'led': {'ledEnable': True, 'ledIntensity': 25.0, 'ledMode': 1}}` — the shared-reference mutation described in the ticket is what wrote the 25.0 into `self.data[DATA_SECTION_LED]` and propagated it to the entity via the same-tick coordinator refresh (0.001 s, MainThread).
- The robot never receives the delta (offline). The eager 25 lingers for **~10 s** until the periodic `shadow/get/accepted` reads `reported.led.ledIntensity = 100` (unchanged) and reverts the entity. AWS then wipes `desired = {}` at T + 14 s (server-side timeout).
- If the broker were also down, no `shadow/get` refresh would fire and the phantom 25 would persist indefinitely until reconnect.

Reproduces the pattern for all three LED entities (`light.<robot>_led`, `number.<robot>_intensite_led`, `select.<robot>_mode_led`), since they share the same `_get_led_settings` code path (`aws_client.py:640, 646, 652`).

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

|            Δt | UTC              | Event                                                                               | Source                     |
| ------------: | ---------------- | ----------------------------------------------------------------------------------- | -------------------------- |
|             0 | 12:46:55.615     | `Set led intensity, Desired: {'led': {'ledEnable': True, 'ledIntensity': 25.0, …}}` | aws_client.mqtt.log:1      |
|        + 0 ms | 12:46:55.615     | Publish `$aws/things/N4720KMV/shadow/update` (MainThread)                           | aws_client.mqtt.log:2      |
|        + 1 ms | 12:46:55.616     | Coordinator refresh runs synchronously in the same MainThread frame                 | aws_client.mqtt.log:5      |
|    **+ 2 ms** | **12:46:55.617** | **`Data for number_n4720kmv3q_nono_2_intensite_led: {'state': 25.0}`**              | **aws_client.mqtt.log:6**  |
|        + 2 ms | 12:46:55.617     | Recorder ingests `state = 25.0`                                                     | state_history.tsv:3        |
|        + 5 ms | 12:46:55.620     | AWS SDK `packet_id: 67` confirmed                                                   | aws_client.mqtt.log:7      |
|      + 145 ms | 12:46:55.760     | `shadow/update/accepted` v2499 (`desired.led.ledIntensity=25.0`)                    | aws_client.mqtt.log:13-14  |
|      + 149 ms | 12:46:55.764     | `shadow/update/delta` (i.e. AWS acknowledges the delta is pending — robot offline)  | aws_client.mqtt.log:15     |
|     + 10.00 s | 12:47:05.619     | Coordinator refresh (MQTT-debounced)                                                | aws_client.mqtt.log:22     |
| **+ 10.00 s** | **12:47:05.620** | **`Data for number_n4720kmv3q_nono_2_intensite_led: {'state': 100}`**               | **aws_client.mqtt.log:23** |
|     + 10.00 s | 12:47:05.621     | Recorder ingests `state = 100` (rollback via shadow `reported`)                     | state_history.tsv:4        |
|     + 14.25 s | 12:47:09.869     | `shadow/update/accepted` v2500 (`desired = {}` — AWS clears the un-applied delta)   | aws_client.mqtt.log:27-28  |

Two things worth pinning:

1. The `Data for … {'state': 25.0}` log line at **T + 2 ms** proves the entity read the mutated value before _any_ cloud round-trip completed. Nothing else in the code path writes to `self.data[DATA_SECTION_LED]` in that window — the mutation can only come from `request_data[key] = value` at `aws_client.py:739` operating on the shared reference returned by `self.data.get(DATA_SECTION_LED, default_data)`.
2. The rollback at **T + 10 s** is **not** a shadow delta from the robot (the robot is offline throughout). It's a plain `shadow/get/accepted` refresh reading `reported.led.ledIntensity = 100` — the unchanged truth — and re-applying it to `self.data`. On a healthy robot the same rollback would come from the `reported` update the robot pushes _after_ it applies the delta; here the same mechanism happens to hide the bug by short-circuiting on the stale `reported`.

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
5. The 10 s rollback is the periodic shadow refresh reading `reported` (which is stale on the robot side, since the robot is offline). It does _not_ verify that the delta was applied; it only reflects whatever `reported` currently holds. On a fully-online robot the same rollback would arrive later, when the robot updates `reported` after applying the command — but during that window (network latency + robot processing time) the entity still lies.

## Impact on the three LED entities

`aws_client.py` calls `_get_led_settings` from three service methods:

- `_set_led_mode` (line 640) → mutates `self.data[LED][DATA_LED_MODE]`
- `_set_led_intensity` (line 646) → mutates `self.data[LED][DATA_LED_INTENSITY]`
- `_set_led_enable` (line 652) → mutates `self.data[LED][DATA_LED_ENABLE]`

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

or explicit `.copy()`. No test scaffold change beyond a regression that asserts `self.data[DATA_SECTION_LED]` is untouched immediately after `_set_led_intensity` returns and before any shadow ACK arrives.

## Ready-to-post reply for elad-bar on #24

> Live reproduction on my S2000 (offline, `robot_state=notConnected`, but AWS broker still up):
>
> - HA entity `state` jumps 100 → 25.0 at T + 2 ms after the service call — **before** `shadow/update/accepted` is received (T + 145 ms). The eager write can only come from mutating the shared reference returned by `self.data.get(DATA_SECTION_LED)` at `aws_client.py:738`; nothing else in the code path writes state before the ACK.
> - Robot is unreachable throughout, so `reported.led.ledIntensity` stays at 100. A follow-up `shadow/get/accepted` ~10 s later re-reads `reported` and reverts the entity to 100 — the "correction" is not a shadow delta from the robot, it's a poll masking the mutation.
> - If the broker is also degraded (real-world scenario: home network / cloud outage), no `shadow/get` refresh fires and the phantom value persists indefinitely until reconnect.
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
