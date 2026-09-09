"""Audit chain rules that can be checked without the host."""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "ingenaning"


def _line(seq: int, prev: str, event: str, data: dict) -> dict:
    body = {
        "seq": seq,
        "ts": 1_757_356_000 + seq,
        "prev": prev,
        "event": event,
        "actor": "test",
        "data": data,
    }
    body["hash"] = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return body


def test_chain_verifies_and_detects_edit():
    from ingenaning.telemetry.relay import verify_chain  # stdlib-only host code

    lines = []
    prev = "0" * 64
    for i in range(1, 6):
        row = _line(i, prev, "move", {"path": f"/f{i}"})
        lines.append(row)
        prev = row["hash"]
    assert verify_chain(lines) == (True, None)
    lines[2]["data"]["path"] = "/tampered"
    ok, at = verify_chain(lines)
    assert not ok and at == 3
    del lines[2]
    ok, at = verify_chain(lines)
    assert not ok and at == 3  # seq gap


def test_only_relay_touches_audit_file():
    for p in ROOT.rglob("*.py"):
        if p.name == "relay.py":
            continue
        assert "audit.jsonl" not in p.read_text(), f"{p}: references the audit file"


def test_state_changes_emit_audit_first():
    """Every function in executor/ and api/ that calls a store mutation must
    call audit.emit earlier in the same function body."""
    mutators = {"insert_", "set_", "delete_", "mark_", "upsert_"}
    subs = [ROOT / s for s in ("executor", "api") if (ROOT / s).is_dir()]
    if not subs:
        pytest.skip("executor/ and api/ are not written yet")
    for sub in subs:
        for p in sub.rglob("*.py"):
            tree = ast.parse(p.read_text())
            for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
                calls = [
                    n
                    for n in ast.walk(fn)
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                ]
                first_mut = next(
                    (c.lineno for c in calls if any(c.func.attr.startswith(m) for m in mutators)),
                    None,
                )
                if first_mut is None:
                    continue
                emits = [
                    c.lineno
                    for c in calls
                    if c.func.attr == "emit" and getattr(c.func.value, "id", "") == "audit"
                ]
                assert emits and min(emits) < first_mut, (
                    f"{p}:{fn.name}: mutation before audit.emit()"
                )
