"""Declared signals (build doc section 4.1).

A signal is a name, a kind, a TTL and a bucket role. The daemon attaches no
meaning to any name: `presence`, `vpn.phone` and `workstation` are opaque
strings that a producer chose (AGENTS.md rule 4). Names are the keys of the
bandit bucket, which is `name=value` pairs joined by `|`, so a name may not
contain whitespace, `|` or `=`.

Two signals are built in and always present: `clock.daypart` and
`clock.daytype`. They are derived from the wall clock, never stored, never
expire, and cannot be redeclared or removed.

Writers here take a connection from `Database.tx()`; readers take one from
`Database.connection()`."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Iterable
from datetime import datetime
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, field_validator

from ingenaning.store import queries as q

log = logging.getLogger(__name__)

Kind = Literal["enum", "number", "bool", "text"]
BucketRole = Literal["primary", "secondary", "feature", "none"]

DEFAULT_TTL_S: Final = 900
# The clock signals must outlive any run without being refreshed; they are
# recomputed from `now` on every vector build, so the TTL only has to be large.
_CLOCK_TTL_S: Final = 10**9

# The neutral truth vocabulary. Nothing in it names a place, a person or a
# device state the daemon would have to understand (AGENTS.md rule 4).
_TRUE_WORDS: Final = frozenset({"true", "on", "1", "yes", "connected"})
_FALSE_WORDS: Final = frozenset({"false", "off", "0", "no", "disconnected"})

# Local-time boundaries for clock.daypart, in hours.
_EVENING_FROM: Final = 17
_NIGHT_FROM: Final = 23
_DAY_FROM: Final = 7


class SignalDef(BaseModel):
    """One row of `signal_defs`. `values` is the allowed set for `enum` and an
    inclusive `[min, max]` for `number`; it is ignored for the other kinds."""

    model_config = ConfigDict(extra="forbid")

    name: str
    kind: Kind = "text"
    values: list[str] | None = None
    ttl_s: int = DEFAULT_TTL_S
    bucket_role: BucketRole = "none"
    source: str = ""

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip()
        if not v or any(c.isspace() for c in v) or "|" in v or "=" in v:
            raise ValueError("signal name must be non-empty and free of whitespace, '|' and '='")
        return v

    def to_row(self) -> dict[str, Any]:
        """Exactly the `signal_defs` columns. `created` is only used on insert;
        the upsert keeps the original value on a redeclaration."""
        return {
            "name": self.name,
            "kind": self.kind,
            "values_json": json.dumps(self.values) if self.values is not None else None,
            "ttl_s": self.ttl_s,
            "bucket_role": self.bucket_role,
            "source": self.source,
            "created": int(time.time()),
        }

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> SignalDef:
        return cls(
            name=r["name"],
            kind=r["kind"] or "text",
            values=json.loads(r["values_json"]) if r["values_json"] else None,
            ttl_s=r["ttl_s"] if r["ttl_s"] is not None else DEFAULT_TTL_S,
            bucket_role=r["bucket_role"] or "none",
            source=r["source"] or "",
        )

    def normalise(self, value: Any) -> str:
        """Validate an incoming value and return the canonical string stored in
        `signals`. Raises ValueError for anything the kind does not accept."""
        if self.kind == "bool":
            return self._normalise_bool(value)
        if self.kind == "number":
            return self._normalise_number(value)
        if self.kind == "enum":
            s = str(value).strip()
            if self.values and s not in self.values:
                raise ValueError(f"{self.name}: {s!r} is not one of {self.values}")
            return s
        return str(value)

    def _normalise_bool(self, value: Any) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        s = str(value).strip().lower()
        if s in _TRUE_WORDS:
            return "true"
        if s in _FALSE_WORDS:
            return "false"
        raise ValueError(
            f"{self.name}: {value!r} is not one of {sorted(_TRUE_WORDS | _FALSE_WORDS)}"
        )

    def _normalise_number(self, value: Any) -> str:
        try:
            f = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{self.name}: {value!r} is not a number") from exc
        if self.values and len(self.values) == 2:
            lo, hi = float(self.values[0]), float(self.values[1])
            if not lo <= f <= hi:
                raise ValueError(f"{self.name}: {f} is outside [{lo}, {hi}]")
        return str(int(f)) if f == int(f) else repr(f)


BUILTIN: Final[dict[str, SignalDef]] = {
    "clock.daypart": SignalDef(
        name="clock.daypart",
        kind="enum",
        values=["day", "evening", "night"],
        ttl_s=_CLOCK_TTL_S,
        bucket_role="primary",
        source="builtin",
    ),
    "clock.daytype": SignalDef(
        name="clock.daytype",
        kind="enum",
        values=["weekday", "weekend"],
        ttl_s=_CLOCK_TTL_S,
        bucket_role="primary",
        source="builtin",
    ),
}


def clock_values(ts: int | None = None) -> dict[str, str]:
    """The built-in signals at `ts` (epoch seconds), in local time: night before
    07:00 and from 23:00, evening from 17:00, day between; Sat/Sun are weekend."""
    dt = datetime.fromtimestamp(ts if ts is not None else time.time())
    h = dt.hour
    if h < _DAY_FROM or h >= _NIGHT_FROM:
        daypart = "night"
    elif h >= _EVENING_FROM:
        daypart = "evening"
    else:
        daypart = "day"
    return {
        "clock.daypart": daypart,
        "clock.daytype": "weekend" if dt.weekday() >= 5 else "weekday",
    }


def declare(conn: sqlite3.Connection, d: SignalDef) -> None:
    """Create or replace a definition. Redeclaring a name is how the operator
    widens an enum or changes a role. The clock signals are not redefinable."""
    if d.name in BUILTIN:
        raise ValueError(f"{d.name} is built in and cannot be redeclared")
    q.upsert_signal_def(conn, d.to_row())
    log.debug("declared signal %s (%s, role %s)", d.name, d.kind, d.bucket_role)


def declared(conn: sqlite3.Connection) -> dict[str, SignalDef]:
    """The built-ins plus every `signal_defs` row, keyed by name."""
    out = dict(BUILTIN)
    for r in q.signal_defs(conn):
        out[r["name"]] = SignalDef.from_row(r)
    return out


def ensure_declared(conn: sqlite3.Connection, name: str, source: str) -> SignalDef:
    """The definition for `name`, creating one with `bucket_role=none` if there
    is none (build doc 4.2): an undeclared name is stored but ignored for
    bucketing until an operator declares it."""
    defs = declared(conn)
    if name in defs:
        return defs[name]
    d = SignalDef(name=name, kind="text", ttl_s=DEFAULT_TTL_S, bucket_role="none", source=source)
    q.upsert_signal_def(conn, d.to_row())
    log.debug("auto-declared signal %s from %s with role none", d.name, source)
    return d


def ensure_primary(conn: sqlite3.Connection, names: Iterable[str]) -> list[str]:
    """Make every name in `names` a primary signal (policy `signals.primary`).

    A name with no definition is declared primary; a name that already has one
    keeps its kind, values, ttl_s and source and only has its role raised.
    Built-in names are already primary and are skipped. Returns the names that
    were written, so a second call with the same list writes nothing."""
    defs = declared(conn)
    written: list[str] = []
    for name in names:
        if name in BUILTIN:
            continue
        existing = defs.get(name)
        if existing is None:
            d = SignalDef(name=name, bucket_role="primary", source="policy")
        elif existing.bucket_role == "primary":
            continue
        else:
            d = existing.model_copy(update={"bucket_role": "primary"})
        q.upsert_signal_def(conn, d.to_row())
        defs[name] = d
        written.append(name)
    if written:
        log.info("%d signal definitions raised to primary", len(written))
    return written


def remove(conn: sqlite3.Connection, name: str) -> int:
    """Drop a definition; returns the number of rows deleted. The emitted values
    stay in `signals`. The clock signals cannot be removed."""
    if name in BUILTIN:
        raise ValueError(f"{name} is built in and cannot be removed")
    return q.delete_signal_def(conn, name)
