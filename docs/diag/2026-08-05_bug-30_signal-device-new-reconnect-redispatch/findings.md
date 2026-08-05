# BUG-30 — reconnection re-fires `SIGNAL_DEVICE_NEW`, re-adding all entities (`v1.0.26b3-raoul.*`)

## TL;DR

Live confirmation of [BUG-30 (#152)](https://github.com/raouldekezel/dolphin-robot/issues/152).
Every time the **REST/API side** of the integration drops and reconnects, the
integration re-runs its "new device discovered" pipeline and re-instantiates
**all 29 entities** with their existing `unique_id`s. Home Assistant's
`entity_platform` refuses the duplicates and logs one `ERROR` per entity:

```
Platform mydolphin_plus does not generate unique IDs.
ID number_<serial>_nono_2_intensite_led already exists - ignoring number.nono_2_intensite_led
```

- **Root cause.** `RestAPI._device_loaded` is a one-shot "device already
  discovered" latch. `_set_status()` resets it to `False` on any transition
  to a disconnected status (`rest_api.py:681`, via
  `ConnectivityStatus.is_disconnected()` — includes `FAILED`, `EXPIRED_TOKEN`,
  `NOT_CONNECTED`, …). On the next reconnect, `RestAPI.update()`
  (`rest_api.py:379-396`) — the **sole** dispatcher of `SIGNAL_DEVICE_NEW` —
  finds the latch clear again and re-dispatches it. The per-platform
  `_async_device_new` listeners are registered for the **whole config-entry
  lifetime** (`entry.async_on_unload(async_dispatcher_connect(...))`, e.g.
  `number.py:34-36`) and are **not idempotent**: each rebuilds its full entity
  set unconditionally (`common/base_entity.py:async_setup_entities` →
  `async_add_entities`). `SIGNAL_DEVICE_NEW` ("new device") is thus reused as a
  reconnect signal.
- **Discriminator (proven).** In the 8-day `home-assistant.log` window the
  **REST API** transitioned `Connected → Failed` exactly **twice** (2026-07-30
  20:54:36, 2026-08-04 14:07:52) — and there were **exactly two** dedup bursts
  (2026-07-30 20:56:39, 2026-08-04 15:25:01), 29 `ERROR`s each. The **15**
  AWS/MQTT-only drops in the same window (API stays `CONNECTED`, so
  `_device_loaded` is never touched) produced **zero** bursts. A third burst
  sits in the rotated log (2026-07-27 01:10, 29 `ERROR`s).
- **Impact: cosmetic.** HA keeps the originally-registered entities and drops
  the re-instantiated copies — the entities keep working. This is log noise
  (at `ERROR` level), not a functional or state regression. No orphan/duplicate
  entities appear in the registry.

## Context

- Fork: `raouldekezel/dolphin-robot`, `deploy` branch, HACS install on a single
  S2000 (`nono_2`, 29 entities across 7 platforms: 17 sensor, 5 number,
  2 select, 2 binary_sensor, 1 light, 1 vacuum, 1 remote).
- Source: `home-assistant.log` (2026-07-29 09:20 → 2026-08-05 19:47) and the
  rotated `home-assistant.log.1`. Both bursts occurred with
  `custom_components.mydolphin_plus` at its default (INFO) level — DEBUG was
  briefly on only at 2026-07-29 09:20–09:24.
- The two REST-side drops have distinct proximate causes, both of which flow
  through a disconnected status and reset the latch:
  - 2026-07-30 20:54:36 — `authenticate-user` HTTP POST, `[Timeout while
contacting DNS servers]` (`rest_api.py` line 517), `Connected → Failed`.
  - 2026-08-04 14:07:52 — `refresh transient failure (Cognito InitiateAuth
network failure)`, `Connected → Failed` (WARNING-level status change).

## Mechanism (call graph)

`async_add_entities` for the full entity set is reachable by **exactly one**
path (verified by exhaustive grep — there is no other caller):

```
RestAPI.update()                          rest_api.py:379   (guard: status == CONNECTED and not _device_loaded)
  └─ _async_dispatcher_send(SIGNAL_DEVICE_NEW)   rest_api.py:396   (SOLE dispatcher of this signal)
       └─ <platform>._async_device_new()   e.g. number.py:23     (@callback, lifetime-scoped, NOT idempotent)
            └─ async_setup_entities(...)    common/base_entity.py:20
                 └─ async_add_entities(entities, True)   common/base_entity.py:42
                      └─ entity_platform: unique_id already registered → "does not generate unique IDs"
```

The one-shot latch that is supposed to make the dispatch fire only on genuine
first discovery:

```python
# rest_api.py — update()
async def update(self):
    if self._status != ConnectivityStatus.CONNECTED:   # line 380
        return
    if self._device_loaded:                            # line 383  ← guard
        return
    ...
    self._device_loaded = True                         # line 394
    self._async_dispatcher_send(SIGNAL_DEVICE_NEW, ...) # line 396

# rest_api.py — _set_status()
if status.is_disconnected():                            # line 680
    self._device_loaded = False                        # line 681  ← latch defeated on every REST drop
```

Because the platform listeners persist across reconnects and are never
de-duplicated, re-firing `SIGNAL_DEVICE_NEW` re-adds the entire catalogue.

## Timeline (Burst 1, 2026-07-30)

|         Δt | Time (CEST)  | Event                                                                                               | Source                        |
| ---------: | ------------ | --------------------------------------------------------------------------------------------------- | ----------------------------- |
|     −188 s | 20:53:31.012 | `aws_client` `Connected → Failed` (`AWS_ERROR_MQTT_TIMEOUT`) — API still `CONNECTED`                | `reconnect_redispatch.log:21` |
|     −123 s | 20:54:33.761 | coordinator `Firing reconnection attempt #1` (`_api.initialize()`)                                  | `:23`                         |
| **−123 s** | 20:54:36.484 | **`rest_api` `Connected → Failed` (DNS timeout on `authenticate-user`)** → `_device_loaded = False` | `:24`                         |
|       −3 s | 20:56:36.761 | coordinator `Firing reconnection attempt #2`                                                        | `:26`                         |
|          0 | 20:56:39.479 | **first of 29 `does not generate unique IDs` ERRORs** (whole catalogue re-added)                    | `:28`                         |
|     +22 ms | 20:56:39.501 | last of the 29 ERRORs (`remote.nono_2`)                                                             | `:56`                         |

Burst 2 (2026-08-04) is identical in shape: REST `Connected → Failed` at
14:07:52 (Cognito), then the burst at 15:25:01 on the first REST reconnect that
completed (attempt #8), 29 ERRORs in ~7 ms.

## Evidence

- `reconnect_redispatch.log`
  - **Section A** — the discriminator: 2 REST `Connected → Failed` vs 2 dedup
    bursts vs 15 AWS-only drops.
  - **Section B / C** — full per-burst context (both drops and both 29-ERROR
    bursts).
  - **Section D** — the third burst in the rotated log (2026-07-27).
  - **Section E** — per-entity dedup distribution: every one of the 29 entities
    is ignored exactly twice ⇒ exactly two re-discovery events in this log.

## Open item (needs a DEBUG capture)

The entire re-add path is DEBUG-level — `update()` logs
`"Connected. Refresh details"` (`rest_api.py:386`) and `async_setup_entities`
logs `"Setting up <platform> entities"` (`base_entity.py`), both suppressed at
the default level. With INFO logging, the re-dispatch is therefore invisible
**except** via the HA-core dedup ERRORs it produces, and no INFO-level
`… → Connected to the API` recovery transition is emitted inside the immediate
burst window. The causal chain above is nonetheless fully determined by the
call graph (single dispatcher, single guard) and by the discriminator. A
capture of the next REST reconnect with
`custom_components.mydolphin_plus` at DEBUG would timestamp the exact
`update()` invocation and the intermediate `CONNECTED` status for the record.

## Fix direction

Not implemented here (design options are enumerated on the BUG-30 issue
thread). The robust, low-blast-radius fix is to make entity creation
idempotent — either filter already-registered `unique_id`s inside
`async_setup_entities` before `async_add_entities`, or gate the dispatch on a
never-reset "entities dispatched" latch decoupled from the connection-state
`_device_loaded` flag.
