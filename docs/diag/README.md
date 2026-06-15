# Diagnostic experiments

Raw artifacts of timed diagnostic runs against a real Maytronics Dolphin
robot. Each session is self-contained and immutable once merged; later
sessions supersede rather than rewrite. Not installed by HACS.

## Sessions

One row per session, kept in sync with the subdirectory list by the
`scripts/check_diag_index.py` test (see _Drift-proof index_ below). The
PR that adds the session updates this table in the same commit.

| Date       | Bug                                                               | Question                                                                                                                                                                       | Answer (TL;DR)                                                                                                                                                                                                                                                                                                                                                                                                                                                                             | Link                                                                                                       |
| ---------- | ----------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------- |
| 2026-06-12 | MAP-01                                                            | Is `stairs` a transient phase of `REGULAR` or a first-class user-selectable mode?                                                                                              | First-class mode — operator-facing `« Couverture complète »` / Full Coverage. HA-initiated mode write is honored end-to-end, app reflects it.                                                                                                                                                                                                                                                                                                                                              | [2026-06-12_map-01_stairs-validation](2026-06-12_map-01_stairs-validation/findings.md)                     |
| 2026-06-12 | MAP-01                                                            | When the Maytronics app initiates a mode change, does the integration still enforce its locally configured `cycle_time_<mode>`?                                                | Yes — the integration emits a `Set cycle time` write ~1 s after every observed mode delta (BUG-08 sleep), whether the trigger was HA or the app.                                                                                                                                                                                                                                                                                                                                           | [2026-06-12_map-01_app-driven-mode-transitions](2026-06-12_map-01_app-driven-mode-transitions/findings.md) |
| 2026-06-12 | [BUG-08](https://github.com/raouldekezel/dolphin-robot/issues/17) | Does the integration's per-mode cycle time write reach the firmware?                                                                                                           | Yes when triggered from HA. When triggered from the Maytronics app, the app's own picker-selected duration (default 2 h 30 = 150 min) overwrites the integration's value ~1.4 s later via last-write-wins on AWS IoT Shadow. Operator decision: keep this "launcher picks the duration" semantics.                                                                                                                                                                                         | [2026-06-12_bug-08_cycle-time](2026-06-12_bug-08_cycle-time/findings.md)                                   |
| 2026-06-13 | MAP-03                                                            | Are the 5 firmware catalog modes unknown to the enum (`cove`, `spot`, `wall`, `ticTac`, `custom`) first-class pilotable cleaning modes worth adding to `CleanModes`?           | `cove`/`spot`/`wall` are firmware-pilotable but intentionally not surfaced by Maytronics (i18n placeholders unresolved in MyDolphin Plus); `ticTac` is a DolphinTech Plus technician/diagnostic mode; `custom` requires a payload the integration can't transport. Decision: skip all five — enum stays at 7 with `stairs` from PR #35.                                                                                                                                                    | [2026-06-13_map-03_unknown-modes-validation](2026-06-13_map-03_unknown-modes-validation/findings.md)       |
| 2026-06-15 | FEAT-01                                                           | After PR #50 re-merge (tag `v1.0.26b3-raoul.2`), does `stairs` behave end-to-end on S2000 — enum exposed, default cycle time 150, label localized, no regression on BUG-13/14? | Yes — `fan_speed_list` has 7 entries with `stairs` last, default 150 verified empirically after storage purge, "Couverture complète" rendered in dashboard tile and vacuum picker. BUG-13/14 reconfirmed in vivo, and PR #50 missed the `entity.number.cycle_time_stairs.name` translation (filed as BUG-15 #53).                                                                                                                                                                          | [2026-06-15_feat-01_stairs-validation](2026-06-15_feat-01_stairs-validation/findings.md)                   |
| 2026-06-15 | MAP-04                                                            | On S2000, are `water` and `pickup` actually firmware-pilotable, and how do they relate to what the Maytronics app exposes?                                                     | Both pilotable. `water` is a real cleaning mode with a non-wall-follow "spot-waterline" pattern, intentionally not surfaced in the S2000 app picker (4 cards only: `all`/`stairs`/`short`/`floor`); app does localize the running-mode label as "Ligne d'eau". `pickup` is a first-class retrieval cycle with a dedicated app screen ("Récupérez-moi"), firmware-fixed `cycleTime = 12` (the HA-side number entity is silently ignored), and firmware auto-ends ~72 s in. Enum stays at 7. | [2026-06-15_map-04_water-pickup-behaviour](2026-06-15_map-04_water-pickup-behaviour/findings.md)           |

## Layout

```
docs/diag/
├── README.md                                # this file (= the index)
└── YYYY-MM-DD_<bug-id>_<short-topic>/
    ├── findings.md
    ├── NN_<action>.mqtt.log                 # raw HA log slice, ANSI stripped
    └── NN_<action>.sensors.tsv              # periodic HA entity poll
```

- Subdirectory name uses an action-anchored topic (e.g.
  `2026-06-12_bug-08_cycle-time`), never a finding-anchored one.
- `NN_` numeric prefix gives chronological order; the slug describes the
  **action taken**, never the **outcome** (findings get revised; actions
  don't).
- Two file flavours per action: `.mqtt.log` for raw log lines,
  `.sensors.tsv` for periodic polls of `sensor.<robot>_*` entities.

## findings.md

Required structure, in this order:

1. **TL;DR** — one sentence stating the answer to the session's question.
2. **Context** — date, robot model, firmware version, **fork tag or
   commit SHA** of the integration running during the experiment (e.g.
   `v1.0.26b3-raoul.1` or `0d5d63e`), HA version, relevant pre-experiment
   state.
3. **Actions taken** — numbered list matching the `NN_…` prefixes.
4. **Timeline** — key timestamps with what happened at each. Evidence
   layer; survives even if the conclusions are later revised.
5. **Findings** — bullet list. Each conclusion cites a specific line or
   timestamp from the included files.
6. **Open questions** — what the next session would need to answer.
7. **Refs** — issues, PRs, previous or follow-up sessions.

## .sensors.tsv format

First non-comment line is a tab-separated header. The very first line is a
header comment carrying the polling interval and timezone offset. Example:

```
# interval=6s tz=+02:00
timestamp	cycle_time	time_left	mode	robot_state
12:03:18	150	8529.42	all	scanning
```

The cadence and offset comment is mandatory: these files are routinely
pasted detached from `findings.md` into upstream issues, and bare TSV with
neither column names nor sampling rate is unreadable evidence.

## PII to redact

| Real value                                                            | Redacted form                |
| --------------------------------------------------------------------- | ---------------------------- |
| Motor unit serial (~8 chars)                                          | `REDACTED-MUSN`              |
| Robot serial (~10 chars)                                              | `REDACTED-ROBOT-SERIAL`      |
| Wi-Fi SSID                                                            | `REDACTED-WIFI-SSID`         |
| Wi-Fi BSSID / MAC addresses                                           | `REDACTED-MAC`               |
| Timezone **name** (e.g. `Europe/Brussels`)                            | `Europe/[REDACTED]`          |
| AWS IoT endpoint hostname (`<prefix>-ats.iot.<region>.amazonaws.com`) | `REDACTED-IOT-ENDPOINT`      |
| MQTT client id (= HA config entry id, hex UUID)                       | `REDACTED-MQTT-CLIENT-ID`    |
| Cognito tokens (`eyJ…`)                                               | `REDACTED-JWT`               |
| AWS access key id (`AKIA…`)                                           | `REDACTED-AWS-KEY`           |
| AWS secret access key                                                 | `REDACTED-AWS-SECRET`        |
| AWS session token                                                     | `REDACTED-AWS-SESSION-TOKEN` |
| Email address                                                         | `REDACTED-EMAIL`             |
| Account / device UUIDs                                                | `REDACTED-UUID-<purpose>`    |

**Keep** numeric UTC offsets (`+02:00` is shared by ~40 countries, not
PII), timestamps, log levels, thread names, module names, generic robot
model identifiers (`S4`, `M700`, ...), firmware version strings, and AWS
Shadow document version numbers — they carry no PII and are necessary for
analysis.

When in doubt, redact.

## Drift-proof index

The `## Sessions` table above is the index. To prevent it from drifting
out of sync with the actual subdirectory list, `scripts/check_diag_index.py`
fails if any `docs/diag/<date>_<topic>/` subdirectory is missing from the
table, or if any table row points to a non-existent directory.

This is enforced by the **Check diag index** GitHub Actions workflow
(`.github/workflows/check-diag-index.yaml`), which runs on every push and
pull request that touches `docs/diag/` or the check script itself. A PR
that adds a session without updating the table (or vice versa) fails CI.

Run it locally first to fail fast:

```
python3 scripts/check_diag_index.py
```

The session PR is expected to add one row to the table **in the same
commit** that adds the subdirectory.

## Adding a new session

1. Branch from `deploy`: `git checkout -b patches/docs-diag-<bug-id>-<date>`.
2. Run the experiment; redact PII on the copies in `/tmp` first.
3. Create `docs/diag/<date>_<bug-id>_<topic>/` and populate.
4. Write `findings.md` last, in front of the raw files.
5. Add the row to the `## Sessions` table here.
6. Run `python3 scripts/check_diag_index.py`.
7. Open one PR per session targeting `deploy`.
