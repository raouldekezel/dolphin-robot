"""Tests for MAP-02 — no unresolved [%key:...%] placeholders in translation files.

The ``[%key:common::...::...%]`` syntax is a HA Core build-time substitution
mechanism. It is resolved by the official HA Core build pipeline against the
``common`` translation namespace, but the substitution does not run for
custom (HACS) integrations: the file is shipped as-is. Any such placeholder
left in ``strings.json`` or in a locale file is therefore displayed raw to
the user.

The fix replaces the placeholders with literal strings. These tests assert
no placeholder is left anywhere in the file set.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest


def _integration_dir() -> Path:
    import custom_components.mydolphin_plus as pkg

    return Path(pkg.__file__).parent


def _translation_files() -> list[Path]:
    root = _integration_dir()
    files = [root / "strings.json"]
    files.extend(sorted((root / "translations").glob("*.json")))
    return files


PLACEHOLDER_RE = re.compile(r"\[%key:[^%]+%]")


@pytest.mark.parametrize(
    "path",
    _translation_files(),
    ids=lambda p: p.name,
)
def test_no_unresolved_translation_placeholders(path):
    """No [%key:...%] left in shipped translation files."""
    text = path.read_text(encoding="utf-8")
    matches = PLACEHOLDER_RE.findall(text)
    assert not matches, (
        f"{path.name} still contains unresolved translation placeholders that "
        f"HA's build-time resolver does not run on for custom integrations: "
        f"{matches}"
    )


@pytest.mark.parametrize(
    "path",
    _translation_files(),
    ids=lambda p: p.name,
)
def test_translation_file_is_valid_json(path):
    """All translation files must remain valid JSON after the placeholder replacement."""
    json.loads(path.read_text(encoding="utf-8"))


def test_reauth_confirm_title_is_translated():
    """The reauth_confirm.title must be a real string, not a placeholder.

    Specifically targets the FR locale because that's the one the user reported.
    """
    root = _integration_dir()
    fr = json.loads((root / "translations" / "fr.json").read_text(encoding="utf-8"))
    title = fr["config"]["step"]["reauth_confirm"]["title"]
    assert "[%key" not in title, "reauth_confirm.title still contains a placeholder"
    assert title.strip() != "", "reauth_confirm.title is empty"


def test_reauth_successful_abort_is_translated():
    """The abort.reauth_successful must be a real string in every shipped locale."""
    root = _integration_dir()
    for path in (root / "strings.json", *(root / "translations").glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        text = data.get("config", {}).get("abort", {}).get("reauth_successful")
        if text is None:
            continue  # locale doesn't override this key
        assert "[%key" not in text, (
            f"{path.name}: abort.reauth_successful still contains a placeholder"
        )
        assert text.strip() != "", f"{path.name}: abort.reauth_successful is empty"
