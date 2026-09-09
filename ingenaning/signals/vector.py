"""Context vector and bandit bucket (build doc section 4.3).

Every run builds the vector from the latest non-expired value of each declared
signal. The bucket is the cross product of the `primary` signals — the two
clock signals included — as `name=value` pairs sorted by name and joined by
`|`. `secondary` and `feature` signals ride along in the vector for the
planners but never split the bucket.

Expired or never-emitted signals contribute `unknown`. When every non-clock
primary is unknown the run is flagged `degraded`; timers carry on as normal."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

from ingenaning.signals.registry import clock_values, declared
from ingenaning.store import queries as q

UNKNOWN = "unknown"

# The shared Context type arrives with arms/base.py; until it does, this is the
# local definition build_context returns. It carries exactly what a `context`
# row holds plus the row id, so swapping it for the shared one is a rename.


@dataclass(slots=True)
class Context:
    """One evaluation of the signal state: the vector, the bucket it maps to,
    and whether every non-clock primary was unknown. `id` is the `context` row
    when the context was persisted, None when it was not."""

    ts: int
    bucket: str
    degraded: bool
    vector: dict[str, dict[str, Any]] = field(default_factory=dict)
    id: int | None = None


def build_vector(
    conn: sqlite3.Connection, now: int | None = None
) -> tuple[dict[str, dict[str, Any]], str, bool]:
    """(vector, bucket, degraded) at `now`.

    The vector has one entry per declared signal — built-ins first-class among
    them — each `{value, age_s, role, source, kind}`. `value` is None and the
    signal reads as unknown when nothing was ever emitted for it or the newest
    row is older than its `ttl_s`; an age exactly equal to the TTL is still
    fresh. Values emitted under a name nobody declared are ignored here: they
    stay in `signals` and appear once someone declares the name."""
    now = int(time.time()) if now is None else now
    defs = declared(conn)
    latest = q.latest_signals(conn)
    clock = clock_values(now)

    vector: dict[str, dict[str, Any]] = {}
    primary_total = 0
    primary_known = 0
    for name, d in sorted(defs.items()):
        if name in clock:
            value: str | None = clock[name]
            age: int | None = 0
            source = d.source
        else:
            row = latest.get(name)
            if row is None:
                value, age, source = None, None, d.source
            else:
                age = max(0, now - int(row["ts"]))
                value = row["value"] if age <= d.ttl_s else None
                source = row["source"] or d.source
            if d.bucket_role == "primary":
                primary_total += 1
                if value is not None:
                    primary_known += 1
        vector[name] = {
            "value": value,
            "age_s": age,
            "role": d.bucket_role,
            "source": source,
            "kind": d.kind,
        }

    bucket = "|".join(
        f"{name}={entry['value'] if entry['value'] is not None else UNKNOWN}"
        for name, entry in vector.items()
        if entry["role"] == "primary"
    )
    degraded = primary_total > 0 and primary_known == 0
    return vector, bucket, degraded


def build_context(
    conn: sqlite3.Connection, now: int | None = None, persist: bool = True
) -> Context:
    """Build the vector and wrap it in a `Context`. With `persist` the context is
    written to the `context` table and the row id is set on the result; the
    caller must already hold a write transaction in that case."""
    now = int(time.time()) if now is None else now
    vector, bucket, degraded = build_vector(conn, now)
    ctx = Context(ts=now, bucket=bucket, degraded=degraded, vector=vector)
    if persist:
        ctx.id = q.insert_context(conn, now, vector, bucket, degraded)
    return ctx


def primary_summary(vector: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """`{name, value, age_s}` for the primary signals only, sorted by name. This
    is what the feed and the planner prompts show as "the context"."""
    return [
        {"name": name, "value": entry["value"], "age_s": entry["age_s"]}
        for name, entry in sorted(vector.items())
        if entry["role"] == "primary"
    ]
