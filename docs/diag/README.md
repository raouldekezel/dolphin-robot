# Diagnostic experiments

This directory stores the raw artifacts of timed diagnostic runs against a
real Maytronics Dolphin robot integrated with Home Assistant. Each run is
self-contained in its own subdirectory and produced by a deliberate
sequence of user actions on the robot or HA. The goal is to keep evidence
attached to the codebase so that:

- bug analyses can cite a fixed point in time rather than re-running an
  experiment from scratch,
- future contributors can diff a *post-fix* run against the *pre-fix* run
  shipped here,
- a reviewer (human or LLM) can audit the conclusions in a `findings.md`
  against the raw logs without having to trust the author.

This is **not** an installation artifact. HACS reads only
`custom_components/mydolphin_plus/` and ignores everything under `docs/`.

## Directory layout

```
docs/diag/
├── README.md                                # this file
└── YYYY-MM-DD_<bug-id>_<short-topic>/        # one subdirectory per session
    ├── findings.md                          # synthesis of this session only
    ├── NN_<action>.mqtt.log                 # HA debug log slice (raw, ANSI-stripped)
    └── NN_<action>.sensors.tsv              # periodic poll of HA entities
```

There is no global index file. The subdirectory names sort chronologically
because they begin with the date in ISO order, so a plain directory listing
is the index.

## Subdirectory naming

- `YYYY-MM-DD_<bug-id>_<short-topic>`
- `bug-id` follows the issue tracker (`bug-08`, `bug-02`, `sec-01`, ...).
  Use `unknown` if the experiment is exploratory and not tied to a known
  bug yet.
- `short-topic` is a 2-4 word slug summarising the question the session
  tries to answer. Avoid putting a finding in the name (findings can be
  proven wrong later, the action that was taken cannot).

Examples:

- `2026-06-12_bug-08_cycle-time/`
- `2026-06-15_bug-08_no-sleep/`
- `2026-07-04_bug-02_reauth-recurrence/`

## File naming inside a session

Files are prefixed `NN_` (two-digit, leading zeros) so they sort in the
order the actions were taken. The slug describes the **action**, not the
**outcome**.

Examples (good):

- `01_resume-mode-stairs-on-dolphin-app.mqtt.log`
- `02_start-complete-on-dolphin-app.mqtt.log`
- `03_start-complete-on-ha.mqtt.log`

Examples (bad — embed a finding that can be revisited later):

- `02_app-overrides-our-60.mqtt.log`
- `03_ha-start-our-60-holds.mqtt.log`

Each action typically produces two files:

| Extension | Content |
|---|---|
| `.mqtt.log` | Raw HA log slice. Use `docker logs hass --since … --until …`, strip ANSI codes, redact PII (see below). One line per log event, no reformatting. |
| `.sensors.tsv` | Periodic poll of the relevant entities (`sensor.<robot>_cycle_time`, `…_temps_restant_du_cycle`, `…_mode_de_nettoyage`, `…_etat_du_robot`). One line per tick, tab-separated. The polling interval is chosen for the session and documented in `findings.md`. |

If a session has actions that don't involve a sensor poll (e.g. a
config_entry reload trace), the `.sensors.tsv` file is simply absent for
that step.

## findings.md

One per session. Lives in the session's subdirectory. It is **scoped to
this session only** and is considered immutable once the PR that
introduces it merges — if a later experiment revises the conclusions, that
later experiment writes its own `findings.md` in its own subdirectory
rather than rewriting history here.

A `findings.md` should contain, in this order:

1. **Context** — date, robot model, firmware version, integration version,
   relevant HA version, what was set up before the experiment ran (HA
   config, user-visible state).
2. **Actions taken** — a numbered list matching the `NN_…` prefixes of the
   files. One sentence per action, in plain English.
3. **Timeline** — a table or list of key timestamps extracted from the
   logs, with what happened at each. This is the only synthesis layer; if
   the conclusion is wrong, the timeline still stands as raw evidence.
4. **Findings** — short bullet list of what the data showed. Each
   conclusion should cite a specific line or timestamp from the included
   files.
5. **Open questions** — what would the next experiment need to answer in
   order to make progress.
6. **Refs** — links to relevant issues, PRs, previous sessions on the same
   topic.

## PII to redact

Always replace the following before committing:

| Real value | Redacted form |
|---|---|
| Motor unit serial number (typically 8 chars) | `REDACTED-MUSN` |
| Robot serial number (typically 10 chars) | `REDACTED-ROBOT-SERIAL` |
| Wi-Fi SSID | `REDACTED-WIFI-SSID` |
| Time zone name | `Europe/[REDACTED]` (or the appropriate continent) |
| Cognito tokens (any `eyJ…` JWT) | `REDACTED-JWT` |
| AWS access key id (`AKIA…`) | `REDACTED-AWS-KEY` |
| AWS secret access key | `REDACTED-AWS-SECRET` |
| AWS session token | `REDACTED-AWS-SESSION-TOKEN` |
| Email address | `REDACTED-EMAIL` |
| Account UUIDs / device UUIDs | `REDACTED-UUID-<purpose>` |

It is acceptable to leave timestamps, log levels, thread names, module
names, generic robot model identifiers (`S4`, `M700`, ...), generic
firmware version strings, and Shadow document version numbers as-is —
they carry no PII and they are necessary for analysis.

When in doubt, redact.

## Adding a new session

1. Branch off `deploy`: `git checkout -b patches/docs-diag-<bug-id>-<date>`.
2. Run the experiment. Capture both the HA debug log slice and the sensor
   poll into `/tmp` first; do all redaction on those copies.
3. Create `docs/diag/<date>_<bug-id>_<topic>/` and populate it.
4. Write `findings.md` last, once you have the raw files in front of you.
5. Open one PR per session targeting `deploy`. The PR title should match
   the subdirectory name. Reference the underlying issue in the PR body so
   the issue tracker has a back-link.

There is no need to update an index — the directory listing is the index.

## Why no index file

A separate index introduces a second source of truth that drifts away from
the actual subdirectory list as soon as someone forgets to update it. The
directory listing is always correct by construction, and GitHub's tree
view sorts it for free. If the volume of sessions becomes high enough that
discovery is a real problem, add an index then — not preemptively.
