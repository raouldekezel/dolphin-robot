#!/usr/bin/env python3
"""Assert that the ``## Sessions`` table in ``docs/diag/README.md`` matches
the actual subdirectory list under ``docs/diag/``.

Fails (exit 1) and prints a unified diff if either side is missing a row.
Run before opening a session PR; see ``docs/diag/README.md``.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


DIAG_DIR = Path(__file__).resolve().parent.parent / "docs" / "diag"
README = DIAG_DIR / "README.md"

# Subdirectory name shape: YYYY-MM-DD_<bug-id>_<topic>
SUBDIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_[A-Za-z0-9-]+_[A-Za-z0-9-]+$")

# Markdown link in the Link column: [text](path) — we pull `path` and grep
# the YYYY-MM-DD_<bug>_<topic> token out of it.
LINK_TOKEN_RE = re.compile(r"\d{4}-\d{2}-\d{2}_[A-Za-z0-9-]+_[A-Za-z0-9-]+")


def list_subdirs() -> set[str]:
    return {
        p.name
        for p in DIAG_DIR.iterdir()
        if p.is_dir() and SUBDIR_RE.match(p.name)
    }


def list_table_rows() -> set[str]:
    """Extract subdir tokens from the ``## Sessions`` table.

    A row contributes its first matching ``YYYY-MM-DD_<bug>_<topic>`` token,
    typically found in the Link column. Rows that say ``_none yet_`` or
    similar placeholders contribute nothing.
    """
    text = README.read_text(encoding="utf-8")
    sessions_header = "## Sessions"
    next_header_re = re.compile(r"^## ", re.MULTILINE)

    start = text.find(sessions_header)
    if start == -1:
        sys.exit(f"missing `## Sessions` heading in {README}")

    after_header = start + len(sessions_header)
    match = next_header_re.search(text, after_header)
    end = match.start() if match else len(text)
    section = text[after_header:end]

    rows = set()
    for line in section.splitlines():
        line = line.strip()
        if not line.startswith("|") or line.startswith("|--") or line.startswith("|---"):
            continue
        # Header row: cells contain words like "Date", "Bug", etc.
        if "Date" in line and "Bug" in line:
            continue
        tokens = LINK_TOKEN_RE.findall(line)
        rows.update(tokens)
    return rows


def main() -> int:
    if not DIAG_DIR.is_dir():
        sys.exit(f"diag dir missing: {DIAG_DIR}")
    if not README.is_file():
        sys.exit(f"README missing: {README}")

    subdirs = list_subdirs()
    table = list_table_rows()

    missing_in_table = subdirs - table
    missing_on_disk = table - subdirs

    if not missing_in_table and not missing_on_disk:
        print(f"OK — {len(subdirs)} session(s), all rows match.")
        return 0

    if missing_in_table:
        print("Subdirectories present on disk but NOT listed in ## Sessions:")
        for name in sorted(missing_in_table):
            print(f"  + {name}")
    if missing_on_disk:
        print("Rows in ## Sessions pointing to a NON-EXISTENT subdirectory:")
        for name in sorted(missing_on_disk):
            print(f"  - {name}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
