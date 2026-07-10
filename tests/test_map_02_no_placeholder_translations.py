"""Regression tests for MAP-02.

Custom integrations installed via HACS do not go through the HA Core build
step that resolves ``[%key:common::...%]`` placeholders. Any such placeholder
that survives into ``strings.json`` or ``translations/*.json`` is shown raw
to the user (see issue #30 — ``[%key:common::config_flow::abort::reauth_successful%]``
was displayed literally after a successful FR reauth).

The check below scans every leaf string of every shipped translation file
and fails if any value contains a Core placeholder. Enforced uniformly so a
future contributor cannot re-introduce the pattern in any locale.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

COMPONENT_ROOT = (
    Path(__file__).resolve().parent.parent / "custom_components" / "mydolphin_plus"
)

TRANSLATION_FILES = (
    COMPONENT_ROOT / "strings.json",
    COMPONENT_ROOT / "translations" / "en.json",
    COMPONENT_ROOT / "translations" / "fr.json",
    COMPONENT_ROOT / "translations" / "it.json",
)

PLACEHOLDER_RE = re.compile(r"\[%key:[^%]+%\]")


def _load(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def _walk_leaves(node, path=""):
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _walk_leaves(v, f"{path}.{k}" if path else k)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk_leaves(v, f"{path}[{i}]")
    elif isinstance(node, str):
        yield path, node


@pytest.mark.parametrize("path", TRANSLATION_FILES, ids=lambda p: p.name)
def test_no_core_key_placeholder_leaks_to_users(path: Path) -> None:
    data = _load(path)
    offenders = [
        (key, value)
        for key, value in _walk_leaves(data)
        if PLACEHOLDER_RE.search(value)
    ]
    assert not offenders, (
        f"{path.name} still contains unresolved [%key:...%] placeholders "
        f"(custom integrations do not resolve them at build time): {offenders}"
    )
