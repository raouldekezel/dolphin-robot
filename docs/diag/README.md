# Diagnostic experiments

Raw artifacts of timed diagnostic runs against a real Maytronics Dolphin
robot. Each session is self-contained and immutable once merged; later
sessions supersede rather than rewrite. Not installed by HACS.

## Sessions

One row per session, kept in sync with the subdirectory list by the
`scripts/check_diag_index.py` test (see _Drift-proof index_ below). The
PR that adds the session updates this table in the same commit.

| Date       | Bug    | Question                                                                                                                                                             | Answer (TL;DR)                                                                                                                                                                                                                                                                                                                          | Link                                                                                                 |
| ---------- | ------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| 2026-06-13 | MAP-03 | Are the 5 firmware catalog modes unknown to the enum (`cove`, `spot`, `wall`, `ticTac`, `custom`) first-class pilotable cleaning modes worth adding to `CleanModes`? | `cove`/`spot`/`wall` are firmware-pilotable but intentionally not surfaced by Maytronics (i18n placeholders unresolved in MyDolphin Plus); `ticTac` is a DolphinTech Plus technician/diagnostic mode; `custom` requires a payload the integration can't transport. Decision: skip all five — enum stays at 7 with `stairs` from PR #35. | [2026-06-13_map-03_unknown-modes-validation](2026-06-13_map-03_unknown-modes-validation/findings.md) |

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
