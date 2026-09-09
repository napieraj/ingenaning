"""Typed query helpers. Every module talks to SQLite through these so the schema
has one home (store/schema.sql). Times are epoch seconds. Paths are union-relative
and pass through `norm_path` on the way in and on lookup, so a stray slash never
forks one file into two rows. Rows come back as `sqlite3.Row`; `files` rows as
`FileRow`. Helpers take the connection from `Database.connection()` or
`Database.tx()`; writers must run inside `tx()`."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, TypedDict

TIERS: Final = ("hot", "cold")
OPEN_OPS: Final = frozenset({"open", "read"})  # access ops that count as an open


def norm_path(path: str) -> str:
    """Union-relative path with one leading slash and no doubled or trailing
    slashes. '' and '/' both mean the root."""
    return "/" + "/".join(part for part in path.split("/") if part)


def _one(
    conn: sqlite3.Connection, sql: str, args: Sequence[Any] | Mapping[str, Any] = ()
) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute(sql, args).fetchone()
    return row


def _all(
    conn: sqlite3.Connection, sql: str, args: Sequence[Any] | Mapping[str, Any] = ()
) -> list[sqlite3.Row]:
    rows: list[sqlite3.Row] = conn.execute(sql, args).fetchall()
    return rows


def _rowid(cur: sqlite3.Cursor) -> int:
    if cur.lastrowid is None:
        raise sqlite3.OperationalError("INSERT returned no rowid")
    return cur.lastrowid


def _now() -> int:
    return int(time.time())


# --- files ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FileRow:
    path: str
    size: int
    tier: str
    mtime: int
    last_open: int | None
    group_key: str | None
    ordinal: int | None
    pinned: str | None
    pinned_until: int | None
    seen: int | None

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> FileRow:
        return cls(
            path=r["path"],
            size=r["size"] or 0,
            tier=r["tier"],
            mtime=r["mtime"] or 0,
            last_open=r["last_open"],
            group_key=r["group_key"],
            ordinal=r["ordinal"],
            pinned=r["pinned"],
            pinned_until=r["pinned_until"],
            seen=r["seen"],
        )


_FILE_DEFAULTS: Final[dict[str, Any]] = {
    "group_key": None,
    "ordinal": None,
    "pinned": None,
    "pinned_until": None,
}


def upsert_files(conn: sqlite3.Connection, rows: Iterable[Mapping[str, Any]], seen: int) -> int:
    """The scanner's full-row write. Keys: path, size, tier, mtime, and optionally
    group_key, ordinal, pinned, pinned_until. On conflict every column except
    last_open is replaced (last_open belongs to the access stream)."""
    n = 0
    for r in rows:
        row = {**_FILE_DEFAULTS, **r, "path": norm_path(r["path"]), "seen": seen}
        conn.execute(
            """INSERT INTO files(path,size,tier,mtime,last_open,group_key,ordinal,
                                 pinned,pinned_until,seen)
               VALUES(:path,:size,:tier,:mtime,NULL,:group_key,:ordinal,:pinned,:pinned_until,:seen)
               ON CONFLICT(path) DO UPDATE SET
                 size=excluded.size, tier=excluded.tier, mtime=excluded.mtime,
                 group_key=excluded.group_key, ordinal=excluded.ordinal,
                 pinned=excluded.pinned, pinned_until=excluded.pinned_until,
                 seen=excluded.seen""",
            row,
        )
        n += 1
    return n


def delete_unseen_files(conn: sqlite3.Connection, tier: str, seen: int) -> int:
    """Drop rows of one tier that the walk stamped `seen` did not touch. The tier
    is mandatory (AGENTS.md rule 3): a walk of one branch says nothing about the
    other, and a walk that did not complete must not call this at all."""
    if tier not in TIERS:
        raise ValueError(f"delete_unseen_files needs a tier in {TIERS}, got {tier!r}")
    cur = conn.execute(
        "DELETE FROM files WHERE tier=? AND (seen IS NULL OR seen < ?)", (tier, seen)
    )
    return cur.rowcount


def get_file(conn: sqlite3.Connection, path: str) -> FileRow | None:
    r = _one(conn, "SELECT * FROM files WHERE path=?", (norm_path(path),))
    return FileRow.from_row(r) if r else None


def files_by_tier(conn: sqlite3.Connection, tier: str, glob: str | None = None) -> list[FileRow]:
    if glob is not None:
        rows = _all(
            conn, "SELECT * FROM files WHERE tier=? AND path GLOB ? ORDER BY path", (tier, glob)
        )
    else:
        rows = _all(conn, "SELECT * FROM files WHERE tier=? ORDER BY path", (tier,))
    return [FileRow.from_row(r) for r in rows]


def files_under(conn: sqlite3.Connection, prefix: str) -> list[FileRow]:
    """Files at or below a directory path. Uses a range scan on the primary key:
    '0' is the character after '/' so [prefix/, prefix0) is exactly the subtree."""
    p = norm_path(prefix)
    if p == "/":
        rows = _all(conn, "SELECT * FROM files ORDER BY path")
    else:
        rows = _all(
            conn,
            "SELECT * FROM files WHERE path=? OR (path>=? AND path<?) ORDER BY path",
            (p, p + "/", p + "0"),
        )
    return [FileRow.from_row(r) for r in rows]


def group_members(conn: sqlite3.Connection, group_key: str) -> list[FileRow]:
    """Every file sharing a group_key, in ordinal order (unknown ordinals last)."""
    rows = _all(
        conn,
        "SELECT * FROM files WHERE group_key=? ORDER BY ordinal IS NULL, ordinal, path",
        (group_key,),
    )
    return [FileRow.from_row(r) for r in rows]


def set_tier(conn: sqlite3.Connection, path: str, tier: str) -> None:
    conn.execute("UPDATE files SET tier=? WHERE path=?", (tier, norm_path(path)))


def tier_totals(conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
    out = {t: {"files": 0, "bytes": 0} for t in TIERS}
    rows = _all(
        conn, "SELECT tier, COUNT(*) AS n, COALESCE(SUM(size),0) AS b FROM files GROUP BY tier"
    )
    for r in rows:
        if r["tier"] in out:
            out[r["tier"]] = {"files": int(r["n"]), "bytes": int(r["b"])}
    return out


# --- access ---------------------------------------------------------------------

_ACCESS_COLS: Final = "rowid AS id, ts, client, op, path, bytes, tier"


def insert_access(
    conn: sqlite3.Connection, rows: Sequence[tuple[int, str, str, str, int, str | None]]
) -> int:
    """Rows are (ts, client, op, path, bytes, tier); tier is the branch the relay
    saw the event on, or None. Opens (op in OPEN_OPS) also advance files.last_open."""
    data = [
        (ts, client, op, norm_path(path), nbytes, tier)
        for ts, client, op, path, nbytes, tier in rows
    ]
    conn.executemany("INSERT INTO access(ts,client,op,path,bytes,tier) VALUES(?,?,?,?,?,?)", data)
    opens = [(ts, path) for ts, _c, op, path, _b, _t in data if op in OPEN_OPS]
    if opens:
        conn.executemany(
            "UPDATE files SET last_open=MAX(COALESCE(last_open,0), ?) WHERE path=?", opens
        )
    return len(data)


def access_since(
    conn: sqlite3.Connection, since: int, glob: str | None = None
) -> list[sqlite3.Row]:
    """Access rows with ts >= since, oldest first. `id` is the rowid, usable as a
    cursor when several rows share a second."""
    if glob is not None:
        return _all(
            conn,
            f"SELECT {_ACCESS_COLS} FROM access WHERE ts>=? AND path GLOB ? ORDER BY ts, rowid",
            (since, glob),
        )
    return _all(conn, f"SELECT {_ACCESS_COLS} FROM access WHERE ts>=? ORDER BY ts, rowid", (since,))


def access_for_path(conn: sqlite3.Connection, path: str, since: int = 0) -> list[sqlite3.Row]:
    return _all(
        conn,
        f"SELECT {_ACCESS_COLS} FROM access WHERE path=? AND ts>=? ORDER BY ts, rowid",
        (norm_path(path), since),
    )


def last_access(conn: sqlite3.Connection, path: str) -> int | None:
    r = _one(conn, "SELECT MAX(ts) AS t FROM access WHERE path=?", (norm_path(path),))
    return None if r is None or r["t"] is None else int(r["t"])


def prune_access(conn: sqlite3.Connection, before: int) -> int:
    return conn.execute("DELETE FROM access WHERE ts<?", (before,)).rowcount


# --- signals --------------------------------------------------------------------


def upsert_signal_def(conn: sqlite3.Connection, d: Mapping[str, Any]) -> None:
    """Keys: name, kind, values_json, ttl_s, bucket_role, source, created. A
    re-declaration replaces everything but `created`."""
    conn.execute(
        """INSERT INTO signal_defs(name,kind,values_json,ttl_s,bucket_role,source,created)
           VALUES(:name,:kind,:values_json,:ttl_s,:bucket_role,:source,:created)
           ON CONFLICT(name) DO UPDATE SET kind=excluded.kind, values_json=excluded.values_json,
             ttl_s=excluded.ttl_s, bucket_role=excluded.bucket_role, source=excluded.source""",
        d,
    )


def signal_defs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return _all(conn, "SELECT * FROM signal_defs ORDER BY name")


def get_signal_def(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return _one(conn, "SELECT * FROM signal_defs WHERE name=?", (name,))


def delete_signal_def(conn: sqlite3.Connection, name: str) -> int:
    return conn.execute("DELETE FROM signal_defs WHERE name=?", (name,)).rowcount


def insert_signals(conn: sqlite3.Connection, rows: Sequence[tuple[int, str, str, str]]) -> int:
    """Rows are (ts, name, value, source)."""
    conn.executemany("INSERT INTO signals(ts,name,value,source) VALUES(?,?,?,?)", rows)
    return len(rows)


# Newest emitted row per signal name. `ts` alone does not order the rows: a
# producer emitting twice in one second, or a batch carrying an explicit ts,
# leaves two rows with the same ts and MAX(ts) then picks either one. rowid
# breaks the tie in insertion order, so the last value emitted always wins.
_LATEST_SIGNAL_SQL: Final = """
    SELECT name, value, source, ts FROM (
      SELECT name, value, source, ts,
             ROW_NUMBER() OVER (PARTITION BY name ORDER BY ts DESC, rowid DESC) AS rn
      FROM signals) WHERE rn = 1"""


def latest_signals(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    """Newest emitted row per signal name, declared or not."""
    return {r["name"]: r for r in _all(conn, _LATEST_SIGNAL_SQL)}


class SignalValue(TypedDict):
    name: str
    kind: str | None
    bucket_role: str | None
    ttl_s: int | None
    value: str | None  # None once the value is older than ttl_s, or never emitted
    ts: int | None
    age_s: int | None
    expired: bool
    source: str | None


def latest_values(conn: sqlite3.Connection, now: int) -> dict[str, SignalValue]:
    """One entry per declared signal with its newest value, aged against its TTL.
    Expired or never-emitted signals report value None and expired True; age == ttl
    still counts as fresh. Undeclared names are not listed."""
    rows = _all(
        conn,
        f"""SELECT d.name, d.kind, d.bucket_role, d.ttl_s, s.value, s.ts, s.source
            FROM signal_defs d
            LEFT JOIN ({_LATEST_SIGNAL_SQL}) s ON s.name=d.name
            ORDER BY d.name""",
    )
    out: dict[str, SignalValue] = {}
    for r in rows:
        ts: int | None = r["ts"]
        ttl: int | None = r["ttl_s"]
        age = None if ts is None else now - ts
        expired = age is None or (ttl is not None and age > ttl)
        out[r["name"]] = SignalValue(
            name=r["name"],
            kind=r["kind"],
            bucket_role=r["bucket_role"],
            ttl_s=ttl,
            value=None if expired else r["value"],
            ts=ts,
            age_s=age,
            expired=expired,
            source=r["source"],
        )
    return out


def signal_history(conn: sqlite3.Connection, name: str, since: int) -> list[sqlite3.Row]:
    return _all(
        conn,
        "SELECT ts,value,source FROM signals WHERE name=? AND ts>=? ORDER BY ts, rowid",
        (name, since),
    )


def prune_signals(conn: sqlite3.Connection, before: int) -> int:
    return conn.execute("DELETE FROM signals WHERE ts<?", (before,)).rowcount


# --- context --------------------------------------------------------------------


def insert_context(
    conn: sqlite3.Connection, ts: int, vector: Mapping[str, Any], bucket: str, degraded: bool
) -> int:
    cur = conn.execute(
        "INSERT INTO context(ts,vector_json,bucket,degraded) VALUES(?,?,?,?)",
        (ts, json.dumps(vector, sort_keys=True), bucket, int(degraded)),
    )
    return _rowid(cur)


def get_context(conn: sqlite3.Connection, context_id: int) -> sqlite3.Row | None:
    return _one(conn, "SELECT * FROM context WHERE id=?", (context_id,))


def latest_context(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return _one(conn, "SELECT * FROM context ORDER BY id DESC LIMIT 1")


# --- proposals / outcomes -------------------------------------------------------

_PROPOSAL_DEFAULTS: Final[dict[str, Any]] = {
    "context_id": None,
    "accepted": None,
    "moved_at": None,
    "features_json": None,
    "reject_reason": None,
}
# A proposal joined with its outcome. Newest first; within one timestamp (one run)
# in insertion order, which is the order the arms ranked them.
_PROPOSAL_SELECT: Final = """SELECT p.*, o.opened_at, o.hit, o.feedback FROM proposals p
           LEFT JOIN outcomes o ON o.proposal_id=p.id"""
_FEED_STATES: Final[dict[str, str]] = {
    "accepted": "p.accepted=1",
    "rejected": "p.accepted=0",
    "moved": "p.moved_at IS NOT NULL",
    "open": "p.moved_at IS NOT NULL AND o.hit IS NULL",
    "hit": "o.hit=1",
    "miss": "o.hit=0",
}


def insert_proposals(conn: sqlite3.Connection, rows: Iterable[Mapping[str, Any]]) -> list[int]:
    """Keys: ts, run_id, arm, path, reason_class, reason, window_h, size, and
    optionally context_id, accepted, moved_at, features_json, reject_reason."""
    ids: list[int] = []
    for r in rows:
        row = {**_PROPOSAL_DEFAULTS, **r, "path": norm_path(r["path"])}
        cur = conn.execute(
            """INSERT INTO proposals(ts,run_id,arm,path,reason_class,reason,window_h,size,
                                     context_id,accepted,moved_at,features_json,reject_reason)
               VALUES(:ts,:run_id,:arm,:path,:reason_class,:reason,:window_h,:size,
                      :context_id,:accepted,:moved_at,:features_json,:reject_reason)""",
            row,
        )
        ids.append(_rowid(cur))
    return ids


def set_proposal_accepted(
    conn: sqlite3.Connection, proposal_id: int, accepted: bool, reject_reason: str | None = None
) -> None:
    """The executor gate's verdict. A rejection carries why ('budget', 'pinned',
    'opened-recently', ...), shown in the feed as 'rejected: <why>'."""
    conn.execute(
        "UPDATE proposals SET accepted=?, reject_reason=? WHERE id=?",
        (int(accepted), None if accepted else reject_reason, proposal_id),
    )


def mark_proposal_moved(conn: sqlite3.Connection, proposal_id: int, moved_at: int) -> None:
    conn.execute("UPDATE proposals SET accepted=1, moved_at=? WHERE id=?", (moved_at, proposal_id))


def get_proposal(conn: sqlite3.Connection, proposal_id: int) -> sqlite3.Row | None:
    return _one(conn, f"{_PROPOSAL_SELECT} WHERE p.id=?", (proposal_id,))


def open_proposals(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Moved proposals not yet scored (no hit/miss), oldest move first. A row that
    only carries UI feedback is still open."""
    return _all(
        conn,
        f"{_PROPOSAL_SELECT} WHERE p.moved_at IS NOT NULL AND o.hit IS NULL"
        " ORDER BY p.moved_at, p.id",
    )


def backing_arms(conn: sqlite3.Connection, now: int) -> dict[str, list[tuple[str, str]]]:
    """path -> [(arm, bucket)] for moved, unscored proposals whose window
    (moved_at + window_h hours) is still open at `now`. A hot file with no entry
    here has no arm backing it and is evicted first."""
    rows = _all(
        conn,
        """SELECT p.path, p.arm, c.bucket FROM proposals p
           LEFT JOIN context c ON c.id=p.context_id
           LEFT JOIN outcomes o ON o.proposal_id=p.id
           WHERE p.moved_at IS NOT NULL AND o.hit IS NULL
             AND p.moved_at + p.window_h*3600 > ?""",
        (now,),
    )
    out: dict[str, list[tuple[str, str]]] = {}
    for r in rows:
        out.setdefault(r["path"], []).append((r["arm"], r["bucket"] or "unknown"))
    return out


def proposals_feed(
    conn: sqlite3.Connection,
    since: int = 0,
    arm: str | None = None,
    state: str | None = None,
    limit: int = 500,
) -> list[sqlite3.Row]:
    """The UI/API feed. `state` is one of accepted, rejected, moved, open, hit, miss."""
    where = ["p.ts>=?"]
    args: list[Any] = [since]
    if arm is not None:
        where.append("p.arm=?")
        args.append(arm)
    if state is not None:
        if state not in _FEED_STATES:
            raise ValueError(f"unknown proposal state {state!r}; use one of {sorted(_FEED_STATES)}")
        where.append(_FEED_STATES[state])
    args.append(limit)
    return _all(
        conn,
        f"{_PROPOSAL_SELECT} WHERE {' AND '.join(where)} ORDER BY p.ts DESC, p.id LIMIT ?",
        args,
    )


def proposals_for_path(conn: sqlite3.Connection, path: str, limit: int = 100) -> list[sqlite3.Row]:
    return _all(
        conn,
        f"{_PROPOSAL_SELECT} WHERE p.path=? ORDER BY p.ts DESC, p.id LIMIT ?",
        (norm_path(path), limit),
    )


def arm_recent_proposals(conn: sqlite3.Connection, arm: str, limit: int = 50) -> list[sqlite3.Row]:
    """An arm's last proposals with hit and feedback: the planner feedback block."""
    return _all(
        conn,
        f"{_PROPOSAL_SELECT} WHERE p.arm=? ORDER BY p.ts DESC, p.id LIMIT ?",
        (arm, limit),
    )


def set_outcome(
    conn: sqlite3.Connection, proposal_id: int, opened_at: int | None, hit: int
) -> None:
    """Score a proposal. Re-scoring replaces opened_at/hit and keeps any feedback."""
    conn.execute(
        """INSERT INTO outcomes(proposal_id,opened_at,hit,feedback) VALUES(?,?,?,NULL)
           ON CONFLICT(proposal_id) DO UPDATE SET opened_at=excluded.opened_at, hit=excluded.hit""",
        (proposal_id, opened_at, hit),
    )


def set_feedback(conn: sqlite3.Connection, proposal_id: int, value: int) -> None:
    """UI feedback -1 / 0 / +1. Works before the proposal is scored."""
    if value not in (-1, 0, 1):
        raise ValueError(f"feedback must be -1, 0 or 1, got {value!r}")
    conn.execute(
        """INSERT INTO outcomes(proposal_id,opened_at,hit,feedback) VALUES(?,NULL,NULL,?)
           ON CONFLICT(proposal_id) DO UPDATE SET feedback=excluded.feedback""",
        (proposal_id, value),
    )


def labelled_proposals(conn: sqlite3.Connection, since: int = 0) -> list[sqlite3.Row]:
    """Proposals with a feature vector and a scored outcome: the scorer's training set."""
    return _all(
        conn,
        """SELECT p.id, p.arm, p.path, p.ts, p.features_json, o.hit, o.feedback
           FROM proposals p JOIN outcomes o ON o.proposal_id=p.id
           WHERE p.features_json IS NOT NULL AND o.hit IS NOT NULL AND p.ts>=?
           ORDER BY p.ts, p.id""",
        (since,),
    )


def count_labelled(conn: sqlite3.Connection) -> int:
    r = _one(
        conn,
        """SELECT COUNT(*) AS n FROM proposals p JOIN outcomes o ON o.proposal_id=p.id
           WHERE p.features_json IS NOT NULL AND o.hit IS NOT NULL""",
    )
    return int(r["n"]) if r else 0


# --- moves ----------------------------------------------------------------------


def insert_move(conn: sqlite3.Connection, m: Mapping[str, Any]) -> int:
    """Keys: ts, path, src, dst, reason, proposal_id, bytes, ms, ok, err."""
    cur = conn.execute(
        """INSERT INTO moves(ts,path,src,dst,reason,proposal_id,bytes,ms,ok,err)
           VALUES(:ts,:path,:src,:dst,:reason,:proposal_id,:bytes,:ms,:ok,:err)""",
        {**m, "path": norm_path(m["path"])},
    )
    return _rowid(cur)


def moves_for_path(conn: sqlite3.Connection, path: str) -> list[sqlite3.Row]:
    """Move history for `aning why`, newest first, with the backing proposal's arm."""
    return _all(
        conn,
        """SELECT m.rowid AS id, m.*, p.arm, p.reason_class, p.reason AS proposal_reason,
                  p.context_id
           FROM moves m LEFT JOIN proposals p ON p.id=m.proposal_id
           WHERE m.path=? ORDER BY m.ts DESC, m.rowid DESC""",
        (norm_path(path),),
    )


def moves_since(conn: sqlite3.Connection, since: int) -> list[sqlite3.Row]:
    return _all(conn, "SELECT rowid AS id, * FROM moves WHERE ts>=? ORDER BY ts, rowid", (since,))


def moves_count_since(conn: sqlite3.Connection, since: int, ok: bool | None = True) -> int:
    """Moves at or after `since`; ok=True counts successes, False failures, None both."""
    if ok is None:
        r = _one(conn, "SELECT COUNT(*) AS n FROM moves WHERE ts>=?", (since,))
    else:
        r = _one(conn, "SELECT COUNT(*) AS n FROM moves WHERE ts>=? AND ok=?", (since, int(ok)))
    return int(r["n"]) if r else 0


# --- posteriors -----------------------------------------------------------------


def get_posterior(conn: sqlite3.Connection, arm: str, bucket: str) -> tuple[float, float] | None:
    r = _one(conn, "SELECT alpha, beta FROM posteriors WHERE arm=? AND bucket=?", (arm, bucket))
    return (float(r["alpha"]), float(r["beta"])) if r else None


def set_posterior(
    conn: sqlite3.Connection,
    arm: str,
    bucket: str,
    alpha: float,
    beta: float,
    updated: int | None = None,
) -> None:
    conn.execute(
        """INSERT INTO posteriors(arm,bucket,alpha,beta,updated) VALUES(?,?,?,?,?)
           ON CONFLICT(arm,bucket) DO UPDATE SET alpha=excluded.alpha, beta=excluded.beta,
             updated=excluded.updated""",
        (arm, bucket, alpha, beta, _now() if updated is None else updated),
    )


def all_posteriors(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return _all(conn, "SELECT * FROM posteriors ORDER BY arm, bucket")


def posteriors_for_bucket(conn: sqlite3.Connection, bucket: str) -> list[sqlite3.Row]:
    return _all(conn, "SELECT * FROM posteriors WHERE bucket=? ORDER BY arm", (bucket,))


# --- schedules ------------------------------------------------------------------

_SCHEDULE_DEFAULTS: Final[dict[str, Any]] = {
    "filter": None,
    "enabled": 1,
    "window_h": None,
    "until": None,
}


def upsert_schedule(conn: sqlite3.Connection, s: Mapping[str, Any]) -> int:
    """Keyed by (source, name) so yaml, ui and intent entries with one name coexist.
    Keys: name, cron, action, arms (JSON list), source, and optionally filter,
    enabled, window_h, until. Returns the row id."""
    row = {**_SCHEDULE_DEFAULTS, **s}
    conn.execute(
        """INSERT INTO schedules(name,cron,action,arms,filter,enabled,source,window_h,until)
           VALUES(:name,:cron,:action,:arms,:filter,:enabled,:source,:window_h,:until)
           ON CONFLICT(source,name) DO UPDATE SET cron=excluded.cron, action=excluded.action,
             arms=excluded.arms, filter=excluded.filter, enabled=excluded.enabled,
             window_h=excluded.window_h, until=excluded.until""",
        row,
    )
    r = _one(
        conn, "SELECT id FROM schedules WHERE source=? AND name=?", (row["source"], row["name"])
    )
    if r is None:  # cannot happen: the upsert above just wrote it
        raise sqlite3.IntegrityError(f"schedule {row['source']}/{row['name']} missing after upsert")
    return int(r["id"])


def get_schedule(conn: sqlite3.Connection, schedule_id: int) -> sqlite3.Row | None:
    return _one(conn, "SELECT * FROM schedules WHERE id=?", (schedule_id,))


def list_schedules(conn: sqlite3.Connection, source: str | None = None) -> list[sqlite3.Row]:
    if source is not None:
        return _all(conn, "SELECT * FROM schedules WHERE source=? ORDER BY id", (source,))
    return _all(conn, "SELECT * FROM schedules ORDER BY id")


def set_schedule_enabled(conn: sqlite3.Connection, schedule_id: int, enabled: bool) -> None:
    conn.execute("UPDATE schedules SET enabled=? WHERE id=?", (int(enabled), schedule_id))


def delete_schedule(conn: sqlite3.Connection, schedule_id: int) -> int:
    return conn.execute("DELETE FROM schedules WHERE id=?", (schedule_id,)).rowcount


def delete_schedules_by_source(conn: sqlite3.Connection, source: str) -> int:
    return conn.execute("DELETE FROM schedules WHERE source=?", (source,)).rowcount


# --- sequences ------------------------------------------------------------------


def replace_sequences(
    conn: sqlite3.Connection, rules: Iterable[tuple[str, str, int, float, int]]
) -> int:
    """Swap the whole rule set for a fresh refit. Rules are (a, b, support,
    confidence, window_s). Call inside tx() so readers never see an empty table."""
    data = [(norm_path(a), norm_path(b), sup, conf, win) for a, b, sup, conf, win in rules]
    conn.execute("DELETE FROM sequences")
    conn.executemany(
        "INSERT INTO sequences(a,b,support,confidence,window_s) VALUES(?,?,?,?,?)", data
    )
    return len(data)


def sequence_rules(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return _all(conn, "SELECT * FROM sequences ORDER BY a, b")


def rules_for(conn: sqlite3.Connection, a: str) -> list[sqlite3.Row]:
    """Rules whose antecedent is `a`, most confident first."""
    return _all(
        conn,
        "SELECT * FROM sequences WHERE a=? ORDER BY confidence DESC, support DESC, b",
        (norm_path(a),),
    )


# --- pins (runtime: ui, cli, intent) --------------------------------------------


def set_pin(
    conn: sqlite3.Connection,
    path: str,
    tier: str,
    until: int | None,
    source: str,
    created: int | None = None,
) -> None:
    """Pin a path prefix to a tier. A later call for the same path replaces it."""
    conn.execute(
        "INSERT OR REPLACE INTO pins(path,tier,until,source,created) VALUES(?,?,?,?,?)",
        (norm_path(path), tier, until, source, _now() if created is None else created),
    )


def list_pins(conn: sqlite3.Connection, now: int | None = None) -> list[sqlite3.Row]:
    """Pins that have not expired at `now` (default: the current time)."""
    return _all(
        conn,
        "SELECT * FROM pins WHERE until IS NULL OR until>? ORDER BY path",
        (_now() if now is None else now,),
    )


def delete_pin(conn: sqlite3.Connection, path: str) -> int:
    return conn.execute("DELETE FROM pins WHERE path=?", (norm_path(path),)).rowcount


def delete_pins_by_source(conn: sqlite3.Connection, source: str) -> int:
    return conn.execute("DELETE FROM pins WHERE source=?", (source,)).rowcount


# --- runs / counters / arm state / metrics --------------------------------------


def upsert_run(conn: sqlite3.Connection, r: Mapping[str, Any]) -> None:
    """Keys: id, ts, action, arms, trigger, dry_run, context_id, proposals, accepted,
    moved, bytes, evicted, finished, err. A second call with the same id updates
    the progress columns."""
    conn.execute(
        """INSERT INTO runs(id,ts,action,arms,trigger,dry_run,context_id,proposals,accepted,
                            moved,bytes,evicted,finished,err)
           VALUES(:id,:ts,:action,:arms,:trigger,:dry_run,:context_id,:proposals,:accepted,
                  :moved,:bytes,:evicted,:finished,:err)
           ON CONFLICT(id) DO UPDATE SET context_id=excluded.context_id,
             proposals=excluded.proposals, accepted=excluded.accepted, moved=excluded.moved,
             bytes=excluded.bytes, evicted=excluded.evicted, finished=excluded.finished,
             err=excluded.err""",
        r,
    )


def get_run(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row | None:
    return _one(conn, "SELECT * FROM runs WHERE id=?", (run_id,))


def recent_runs(conn: sqlite3.Connection, limit: int = 10) -> list[sqlite3.Row]:
    return _all(conn, "SELECT * FROM runs ORDER BY ts DESC, rowid DESC LIMIT ?", (limit,))


def bump_counter(conn: sqlite3.Connection, name: str, by: int = 1) -> None:
    conn.execute(
        """INSERT INTO counters(name,value) VALUES(?,?)
           ON CONFLICT(name) DO UPDATE SET value=value+excluded.value""",
        (name, by),
    )


def set_counter(conn: sqlite3.Connection, name: str, value: int) -> None:
    conn.execute("INSERT OR REPLACE INTO counters(name,value) VALUES(?,?)", (name, value))


def get_counter(conn: sqlite3.Connection, name: str) -> int:
    r = _one(conn, "SELECT value FROM counters WHERE name=?", (name,))
    return int(r["value"]) if r and r["value"] is not None else 0


class _Unset:
    """Marker for 'leave this column alone' where None is a meaningful value."""


_UNSET: Final = _Unset()


def set_arm_state(
    conn: sqlite3.Connection,
    arm: str,
    *,
    enabled: bool | None = None,
    schema_errors_add: int = 0,
    last_run: int | None = None,
    last_error: str | _Unset | None = _UNSET,
) -> None:
    """Create the arm's row if missing (enabled, no errors) and update only the
    fields given. `last_error=None` clears the error; omitting it keeps it."""
    conn.execute("INSERT OR IGNORE INTO arm_state(arm,enabled,schema_errors) VALUES(?,1,0)", (arm,))
    if enabled is not None:
        conn.execute("UPDATE arm_state SET enabled=? WHERE arm=?", (int(enabled), arm))
    if schema_errors_add:
        conn.execute(
            "UPDATE arm_state SET schema_errors=schema_errors+? WHERE arm=?",
            (schema_errors_add, arm),
        )
    if last_run is not None:
        conn.execute("UPDATE arm_state SET last_run=? WHERE arm=?", (last_run, arm))
    if not isinstance(last_error, _Unset):
        conn.execute("UPDATE arm_state SET last_error=? WHERE arm=?", (last_error, arm))


def arm_states(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    return {r["arm"]: r for r in _all(conn, "SELECT * FROM arm_state ORDER BY arm")}


def set_metric(conn: sqlite3.Connection, day: str, name: str, value: float) -> None:
    """One value per (day, name); day is 'YYYY-MM-DD'. Rewriting a day replaces it."""
    conn.execute(
        "INSERT OR REPLACE INTO metrics_daily(day,name,value) VALUES(?,?,?)", (day, name, value)
    )


def metrics_rows(
    conn: sqlite3.Connection, since_day: str, name: str | None = None
) -> list[sqlite3.Row]:
    """Daily metric rows from `since_day` on, ordered by day then name."""
    if name is not None:
        return _all(
            conn,
            "SELECT day,name,value FROM metrics_daily WHERE day>=? AND name=? ORDER BY day,name",
            (since_day, name),
        )
    return _all(
        conn,
        "SELECT day,name,value FROM metrics_daily WHERE day>=? ORDER BY day,name",
        (since_day,),
    )


# --- expectations ---------------------------------------------------------------
# Held-out reward signal (D-004, AGENTS.md rule 5). Only executor/ and api/ may call
# these helpers. Nothing under arms/, the candidate generator, or prompt assembly may
# read this table; planners learn of a met expectation only through their feedback
# block, after met_at.


def insert_expectation(
    conn: sqlite3.Connection,
    path_glob: str,
    deadline: int,
    bucket: str | None,
    created: int | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO expectations(path_glob,deadline,bucket,created,missed) VALUES(?,?,?,?,0)",
        (path_glob, deadline, bucket, _now() if created is None else created),
    )
    return _rowid(cur)


def get_expectation(conn: sqlite3.Connection, expectation_id: int) -> sqlite3.Row | None:
    return _one(conn, "SELECT * FROM expectations WHERE id=?", (expectation_id,))


def open_expectations(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Neither met nor missed, earliest deadline first."""
    return _all(
        conn,
        "SELECT * FROM expectations WHERE missed=0 AND met_at IS NULL ORDER BY deadline, id",
    )


def list_expectations(conn: sqlite3.Connection, limit: int = 200) -> list[sqlite3.Row]:
    """For the UI: pending ones first, then the rest by deadline, newest first."""
    return _all(
        conn,
        """SELECT * FROM expectations
           ORDER BY (missed=0 AND met_at IS NULL) DESC, deadline DESC, id DESC LIMIT ?""",
        (limit,),
    )


def mark_expectation_met(
    conn: sqlite3.Connection, expectation_id: int, met_by: str, met_at: int
) -> None:
    conn.execute(
        "UPDATE expectations SET met_by=?, met_at=? WHERE id=?", (met_by, met_at, expectation_id)
    )


def mark_expectation_missed(conn: sqlite3.Connection, expectation_id: int) -> None:
    conn.execute("UPDATE expectations SET missed=1 WHERE id=?", (expectation_id,))


def delete_expectation(conn: sqlite3.Connection, expectation_id: int) -> int:
    return conn.execute("DELETE FROM expectations WHERE id=?", (expectation_id,)).rowcount
