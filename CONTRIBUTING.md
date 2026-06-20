# Contributing

Thanks for helping improve MyDolphin Plus. Contributions are welcome as bug reports, documentation updates, translations, tests, and code changes.

## Before opening a pull request

- Check existing issues and pull requests to avoid duplicate work.
- Keep changes focused on one fix or feature at a time.
- Update `README.md`, `CHANGELOG.md`, or files in `docs/` when behavior or user-facing setup changes.
- For Home Assistant changes, follow the existing integration structure under `custom_components/mydolphin_plus/`.

## Development setup

1. Create and activate a Python virtual environment.
2. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Run checks that are relevant to your change before opening a pull request.

This project uses `pyproject.toml` configuration for formatting, import sorting, linting, and tests.

## Pull request guidelines

- Branch new development work from `develop` and open pull requests back into `develop`.
- Use `develop` for pre-release changes before they are promoted to a stable release.
- Explain the user-visible change and why it is needed.
- Include test details, even when the test is manual.
- Keep secrets, tokens, account emails, and robot identifiers out of issues, logs, and pull requests.
- Prefer small, reviewable pull requests over broad refactors.

## Commit message conventions

This project does **not** use [Conventional Commits](https://www.conventionalcommits.org/) for ticketed work. Instead, commit subjects lead with the issue ID, uppercase, followed by a colon and a short description:

```
<TICKET-ID>: <short description> (#<PR>)
```

Examples (taken from the actual history of the `deploy` branch):

- `FEAT-04: expose next_scheduled_mode + next_scheduled_cycle_time sensors (#84)`
- `FEAT-01: add `stairs` (Full Coverage) cleaning mode (#50)`
- `CHORE-02: remove source-level tests (#80)`
- `MAP-03: investigate unknown cleaning modes (cove/spot/wall/ticTac/custom) (#46)`
- `BUG-03 + BUG-05: strip INITIAL_TOKENS_KEY transactionally (#13)`
- `SEC-01/02/03: redact secrets in debug logs (#4)`

The ticket-ID families currently in use are: `SEC`, `BUG`, `HARD`, `FEAT`, `MAP`, `SPIKE`, `CHORE`, `QUIRK`. New families are fine when a category genuinely needs one — keep them short and uppercase.

The single **exception** to "no Conventional Commits" is diagnostic session commits, which use `docs(diag):` to mark them as documentation-only changes living under `docs/diag/`:

```
docs(diag): <session topic> (<YYYY-MM-DD>)
```

For example: `docs(diag): FEAT-04 cleaningModes[mode] source confirmation (2026-06-20) (#83)`.

Squash-merging a PR? The PR title should already follow this convention; pass it as `--subject` to `gh pr merge --squash` so the commit landing on `deploy` matches.

## Tests

Tests that inspect a module's source code as text are forbidden, for the reasons detailed in [#77 (CHORE-02)](https://github.com/raouldekezel/dolphin-robot/issues/77). Inspecting logs, translation JSON files, and other data is fine.

## Contributors

Thanks to everyone who has contributed to this project:

- [Elad Bar](https://github.com/elad-bar)
- Dan Wheaton
- [sh00t2kill](https://github.com/sh00t2kill)
- [tigers75](https://github.com/tigers75)
- [Loïc](https://github.com/zoic21)
- Gil Peeters
- [devilismyfriend](https://github.com/devilismyfriend)
- [yumlevi](https://github.com/yumlevi)
- [grillp](https://github.com/grillp)
- [lordlala](https://github.com/lordlala)

If you contributed and are missing from this list, please open a pull request to add your name.
