"""Every rule in AGENTS.md that can be checked mechanically is checked here."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "ingenaning"


def _sources(sub: str) -> list[Path]:
    return list((ROOT / sub).rglob("*.py"))


def test_no_media_semantics_in_core():
    banned = re.compile(r"episode|season|movie|series|album|plex|roon|\.mkv|\.flac", re.I)
    for p in ROOT.rglob("*.py"):
        if "prompts" in p.parts or p.name.startswith("test"):
            continue
        for i, line in enumerate(p.read_text().splitlines(), 1):
            assert not banned.search(line), f"{p}:{i}: domain semantics in core: {line.strip()}"


def test_expectations_not_read_by_arms():
    for p in _sources("arms"):
        src = p.read_text()
        assert "expectations" not in src, f"{p} references expectations"


def test_no_print():
    for p in ROOT.rglob("*.py"):
        if p.name == "relay.py":  # host script, stdlib, prints to stderr on purpose
            continue
        tree = ast.parse(p.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "print":
                raise AssertionError(f"{p}: print() in daemon code")


def test_relay_is_stdlib_only():
    if not (ROOT / "telemetry" / "relay.py").exists():
        pytest.skip("relay.py not ported yet")
    src = (ROOT / "telemetry" / "relay.py").read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mod = (node.module if isinstance(node, ast.ImportFrom) else node.names[0].name) or ""
            assert not mod.startswith("ingenaning"), "relay.py must not import the package"
