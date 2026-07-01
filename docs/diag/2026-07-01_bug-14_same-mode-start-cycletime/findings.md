# BUG-14 — same-mode `vacuum.start` writes the configured `cycle_time_<mode>` on `v1.0.26b3-raoul.19`

## TL;DR

Live re-check of [BUG-14 (#48)](https://github.com/raouldekezel/dolphin-robot/issues/48) on the S2000 after the write-on-commit / HARD-12 refactors. **The regression is gone**: an HA-initiated Run whose mode is already the firmware's current mode now writes both `desired.cleaningMode.mode` and, via the BUG-08 chain, `desired.cycleInfo.cycleTime` from `number.<robot>_duree_du_cycle_<mode>`. The distinctive test — app pushed `cleaningModes.all = 120`, then HA same-mode Run with `duree_du_cycle_complet = 75` — flipped the firmware catalog to `all = 75` and MyDolphin displays 1 h 15 for the running cycle, not 2 h.

- **Same-mode start writes `cleaningMode.mode`**: yes, unconditionally (raoul.19, `coordinator.py:1149`).
- **BUG-08 chain fires on same-mode start**: yes, `Set cycle time` published 1.15 s after `Set cleaning mode`.
- **Firmware honors HA's cycleTime, not the pre-existing catalog value**: yes — catalog `all` goes 120 (from app) → 75 (from HA) inside 1.1 s, `cycleInfo.cleaningMode.cycleTime` = 75, MyDolphin displays 1 h 15.
- **Root cause of the fix**: incidental, from [BUG-13 write-on-commit (#100)](https://github.com/raouldekezel/dolphin-robot/pull/100) + [HARD-12 (#105)](https://github.com/raouldekezel/dolphin-robot/pull/105). The UI select no longer writes to AWS at all; the only path to the firmware is `_vacuum_start`, which always writes `set_cleaning_mode(mode)` regardless of the current firmware mode, so the shadow accepts, `_event_is_ours` is true, and `_set_cycle_time` is chained.
- **Mode-agnostic**: the same protocol on `stairs` produces the same result — `cycleInfo.cleaningMode.cycleTime` goes 120 (app) → 75 (HA same-mode). Side observation: `cleaningModes.stairs` stays immutable at 150 in the catalog while the run-active value tracks the writes correctly.

## Context

- **Date:** 2026-07-01
- **Tag installed via HACS:** `v1.0.26b3-raoul.19`. No `custom_components/` change vs. `raoul.17` — the two intervening tags shipped CI (CHORE-03, [PR #113](https://github.com/raouldekezel/dolphin-robot/pull/113)) and test fixes (BUG-22, [PR #115](https://github.com/raouldekezel/dolphin-robot/pull/115)). Runtime code exercised is the same one that landed BUG-13 / HARD-11 / HARD-12 / HARD-13.
- **Robot:** Maytronics Dolphin S2000 ("Nono 2"). Firmware reports `robotType="S4"`.
- **HA:** 2026.1.3 (container `hass` on intel-nuc, `network_mode: host`).
- **HA config for this test:** `number.nono_2_duree_du_cycle_complet = 75`, `number.nono_2_duree_du_cycle_sol = 120`.
- **Capture:** `docker logs -f --tail 0 --timestamps hass` filtered on `Set cleaning mode | Set cycle time | Start vacuum | Pause vacuum | shadow reported`. Slice + PII redaction in [`coordinator.mqtt.log`](coordinator.mqtt.log). All times local (Europe/[REDACTED], UTC+2).

## Protocol

Four actions in sequence, no dashboard picker interaction between them (mode stays `all` in `_desired_clean_mode` throughout actions 2–4):

| # | Trigger | Mode written | HA number | Expected shadow (post-echo) |
| - | ------- | ------------ | --------- | --------------------------- |
| 1 | HA Run "Sol" (via dashboard select then Run) | `floor` | 120 | catalog `floor: 120`, cycle time 120 |
| 2 | HA Stop, then Run "Complet" | `all`   | 75  | catalog `all: 75`, cycle time 75 |
| 3 | Stop from HA, Run "Complet" from **MyDolphin app** (user picks 2 h preset) | `all` (app-initiated) | 75 (ignored — token not ours) | catalog `all: 120`, cycle time 120 |
| 4 | Stop from app, **Run "Complet" from HA** — the same-mode start | `all` | 75 | catalog `all: 75`, cycle time 75 |

Action 4 is the load-bearing test: firmware mode `all` unchanged since action 3, catalog seeded to 120 by the app, HA `number = 75`. If BUG-14 persists, the firmware sees `desired.mode = all` (no delta) and either drops the update or fails to chain `cycleTime`; the app-pushed 120 stays in place. If BUG-14 is fixed, HA re-writes `cycleTime = 75` and the catalog + `cycleInfo.cleaningMode.cycleTime` flip to 75.

## Observations

### Action 1 — HA "Sol" (mode change `all → floor`)

```
15:14:35.009 INFO Set cleaning mode, Desired: {'cleaningMode': {'mode': 'floor'}}
15:14:36.073 INFO Set cycle time,    Desired: {'cycleInfo':   {'cycleTime': 120}}
```

Δ = 1.064 s (BUG-08 sleep + shadow round-trip). Post-echo shadow (v2111):

```
"cycleInfo":{"cleaningMode":{"mode":"floor","cycleTime":120}, …}
"cleaningModes":{"all":120,"short":60,"floor":120, …}
```

Catalog `floor` bumped to 120 (HA value). Nominal mode-change path, no surprise.

### Action 2 — HA "Complet" (mode change `floor → all`)

```
15:15:06.260 INFO Set cleaning mode, Desired: {'cleaningMode': {'mode': 'all'}}
15:15:07.322 INFO Set cycle time,    Desired: {'cycleInfo':   {'cycleTime': 75}}
```

Δ = 1.062 s. Post-echo shadow (v2117):

```
"cycleInfo":{"cleaningMode":{"mode":"all","cycleTime":75}, …}
"cleaningModes":{"all":75, "short":60,"floor":75, …}
```

Catalog `all` bumped to 75 (HA value). Note that `floor` in the catalog also carries 75 now — the firmware caps some sibling entries when they were previously equal, an artifact irrelevant to this test.

### Action 3 — MyDolphin app "Complet 2h" (app-initiated, not HA)

**No `Set cleaning mode` or `Set cycle time` line from `custom_components.mydolphin_plus.managers.aws_client`.** This is the expected asymmetry — `_on_update_accepted` chains `Set cycle time` only when `_event_is_ours(payload_data)` is true; app writes carry a foreign client token, so the integration stays out.

Post-app-echo, catalog snapshot advanced to `all: 120` (visible in the shadow payload immediately preceding action 4 in `coordinator.mqtt.log`). Firmware `cycleInfo.cleaningMode.cycleTime = 120`. MyDolphin displays 2 h. This is the state entering the load-bearing test.

### Action 4 — HA "Complet" (**same-mode** start, `all` unchanged)

```
15:20:00.806 INFO Set cleaning mode, Desired: {'cleaningMode': {'mode': 'all'}}
15:20:01.958 INFO Set cycle time,    Desired: {'cycleInfo':   {'cycleTime': 75}}
```

Δ = 1.152 s. **The chain fires even though the mode `all` matches the current firmware value.** Post-echo shadow (v2143):

```
"cycleInfo":{"cleaningMode":{"mode":"all","cycleTime":75}, …}
"cleaningModes":{"all":75, "short":60, "floor":75, "water":75, "ultra":75, …}
```

Catalog `all` flipped **120 → 75** in the same ~1.1 s window. MyDolphin, checked live by the operator: displays **1 h 15**, not 2 h. **Distinctive proof: BUG-14 is fixed.**

### Sub-test — mode `stairs` (same protocol, generalises the verdict)

Replayed the same three-step protocol on `stairs` a few minutes later (15:44 → 15:47) to confirm the fix is not `all`-specific. `number.nono_2_cycle_time_stairs` was left at 75 for the test (default is 150 per FEAT-01).

| # | Trigger | `Set cleaning mode` | `Set cycle time` | Δ | Post-echo `cycleInfo.cycleTime` |
| - | ------- | ------------------- | ---------------- | - | ------------------------------ |
| 1 | HA "Couverture complète" (mode-change) | 15:44:50.703 `stairs` | 15:44:51.766 `75` | 1.063 s | 75 (shadow v2160) |
| 2 | MyDolphin app "Couverture complète 2 h" | ø | ø | — | 120 (shadow v2171, app-pushed) |
| 3 | **HA "Couverture complète" — same-mode** | 15:46:50.352 `stairs` | 15:46:51.450 `75` | 1.098 s | **75** (shadow v2184) |

`cycleInfo.cleaningMode.cycleTime` goes **120 → 75** on the same-mode HA Run — same shape as the `all` result, same conclusion: BUG-14 fixed for `stairs` too. Operator reports MyDolphin displays 1 h 15, matching the shadow.

**Side observation — catalog immutability on `stairs`.** Unlike `all`, `cleaningModes.stairs` **stays at 150 throughout** — every one of the three shadow snapshots reports `"stairs": 150`, whether after HA writes 75 or after the app pushes 120. The **run-active** value (`cycleInfo.cleaningMode.cycleTime`) tracks the writes correctly (75 / 120 / 75), so the visible behaviour is unaffected. Best explanation given the data: the firmware treats `stairs`' catalog entry as a hard-default (150 min = 2 h 30, matching the FEAT-01 default and the middle preset in the app's "Couverture complète" picker) and refuses to update it in place, while still honouring writes to `cycleInfo.cycleTime` for the running cycle. This is a small but real divergence from `all`, which has a mutable catalog entry that mirrors the last write; worth noting for future MAP-* work but not a bug — MyDolphin reads `cycleInfo.cleaningMode.cycleTime`, not the catalog, for the running-cycle display.

## Why the regression is gone (code walk on `raoul.19`)

Two design changes since the [2026-06-13 session that pinned BUG-14](https://github.com/raouldekezel/dolphin-robot/issues/48) remove every path that could skip the `Set cycle time` chain on same-mode start:

1. **BUG-13 write-on-commit ([PR #100](https://github.com/raouldekezel/dolphin-robot/pull/100))** — picking a mode from the HA select no longer writes to AWS. The pick only stages `_desired_clean_mode` in coordinator memory. That closed the historical *"picker wrote the mode; Run then wrote power only, and the chain didn't refire"* path.
2. **HARD-12 ([PR #105](https://github.com/raouldekezel/dolphin-robot/pull/105))** — even the running-path `_set_cleaning_mode` no longer writes to AWS. That closed the fallback where a repeat pick during a cycle would touch the shadow.

After both, the **only** producer of `desired.cleaningMode.mode` is `_vacuum_start` (`coordinator.py:1149`, invoked on every `vacuum.start`):

```
self._aws_client.set_cleaning_mode(mode)  # unconditional — no `if current != mode`
self.async_update_listeners()
```

Any HA Run — mode-change or same-mode — triggers the same shadow-write. The shadow ack (`_on_update_accepted`) sees `desired.cleaningMode.mode` present + `_event_is_ours = True`, sleeps 1 s, publishes `_set_cycle_time(mode)`. The firmware records `cleaningModes[mode] = HA_configured` and starts the cycle at that duration. BUG-14 disappears as a side-effect of the write-on-commit pivot.

The chain sequence itself (`aws_client.py:482–499`) is unchanged from what the [BUG-08 SPIKE-02 session](https://github.com/raouldekezel/dolphin-robot/issues/17) established — `sleep(1)` between the two writes (v2143 – v2141 delta ≈ 1.15 s in this session, matching the +1.06/+1.15 s seen in the [2026-06-12/13 sessions](https://github.com/raouldekezel/dolphin-robot/pull/41)). Nothing about BUG-08 was fixed; BUG-14 was a *distinct* symptom rooted in a picker-side write path that no longer exists.

## Verdict

- **BUG-14 (#48)**: resolved by BUG-13 + HARD-12, not by a direct fix. No regression seen in vivo on `raoul.19`. Recommend closing #48 with a pointer to this session.
- **BUG-08 (#17)**: unchanged behaviour and unchanged root cause (`sleep(1)` in the awscrt callback still there — see `aws_client.py:498`). Still open. This session incidentally re-confirms the 1.06–1.15 s chain delta; nothing new to add there.
- **Related tickets to review whether the wording is still accurate now that BUG-13 landed**:
  - [BUG-13 (#47)](https://github.com/raouldekezel/dolphin-robot/issues/47) — was closed by [PR #100](https://github.com/raouldekezel/dolphin-robot/pull/100); the "picker starts a cycle" symptom is gone by construction.
- **No follow-up work required in production code** for BUG-14.
