"""store/: schema, migrations, and every query helper, against a temp DB."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import settings as hsettings
from hypothesis import strategies as st

from ingenaning.store import queries as q
from ingenaning.store.db import SCHEMA_VERSION, Database, connect

# --- helpers ---------------------------------------------------------------


def frow(
    path: str,
    tier: str = "cold",
    size: int = 100,
    mtime: int = 0,
    group: str | None = None,
    ordinal: int | None = None,
    pinned: str | None = None,
    until: int | None = None,
) -> dict:
    return {
        "path": path,
        "size": size,
        "tier": tier,
        "mtime": mtime,
        "group_key": group,
        "ordinal": ordinal,
        "pinned": pinned,
        "pinned_until": until,
    }


def seed_files(db: Database, rows: list[dict], seen: int = 1) -> None:
    with db.tx() as c:
        q.upsert_files(c, rows, seen=seen)


# --- db.py ------------------------------------------------------------------


def test_migrate_is_idempotent_and_records_version(settings):
    db = Database(settings.db_path)
    assert db.migrate() == SCHEMA_VERSION
    assert db.migrate() == SCHEMA_VERSION
    with db.connection() as c:
        assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {
            "files",
            "access",
            "signal_defs",
            "signals",
            "context",
            "proposals",
            "outcomes",
            "moves",
            "posteriors",
            "schedules",
            "sequences",
            "expectations",
            "pins",
            "migrations",
        } <= names
        assert c.execute("SELECT MAX(version) FROM migrations").fetchone()[0] == SCHEMA_VERSION
    assert db.version() == SCHEMA_VERSION
    db.close()


def test_connect_creates_parent_dir(tmp_path: Path):
    c = connect(tmp_path / "deep" / "er" / "x.db")
    c.execute("CREATE TABLE t(x)")
    c.close()
    assert (tmp_path / "deep" / "er" / "x.db").exists()


def test_tx_rolls_back_on_error(db: Database):
    with pytest.raises(RuntimeError), db.tx() as c:
        q.insert_access(c, [(1, "c", "open", "/x", 0, "hot")])
        raise RuntimeError("boom")
    with db.connection() as c:
        assert c.execute("SELECT COUNT(*) FROM access").fetchone()[0] == 0


def test_tx_propagates_the_real_error_when_sqlite_rolled_back_itself(settings):
    """On SQLITE_FULL, SQLite rolls the transaction back itself. An unconditional
    ROLLBACK then raises "cannot rollback - no transaction is active" and replaces
    the disk-full error the caller has to see."""
    db = Database(settings.db_path)
    db.migrate()
    with db.connection() as c:
        pages = int(c.execute("PRAGMA page_count").fetchone()[0])
        c.execute(f"PRAGMA max_page_count = {pages + 2}")  # a full disk, deterministically
    with pytest.raises(sqlite3.OperationalError) as excinfo, db.tx() as c:
        for i in range(2000):
            q.insert_access(c, [(i, "c", "open", "/p/" + "x" * 200 + str(i), 0, "cold")])
    assert "disk is full" in str(excinfo.value)
    db.close()


def test_concurrent_writers_do_not_lock(db: Database):
    errors: list[BaseException] = []

    def work(i: int) -> None:
        try:
            for j in range(50):
                with db.tx() as c:
                    q.insert_access(c, [(i * 1000 + j, f"c{i}", "open", f"/p{i}/{j}", 0, "cold")])
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    with db.connection() as c:
        assert c.execute("SELECT COUNT(*) FROM access").fetchone()[0] == 400


# --- files ------------------------------------------------------------------


def test_files_upsert_get_and_group_order(db: Database):
    seed_files(
        db,
        [
            frow("/g/b02", group="/g", ordinal=1),
            frow("/g/b10", group="/g", ordinal=2),
            frow("/g/b01", group="/g", ordinal=0, tier="hot"),
            frow("/h/x", group="/h", ordinal=0),
        ],
    )
    with db.connection() as c:
        f = q.get_file(c, "/g/b02")
        assert f and f.tier == "cold" and f.group_key == "/g" and f.ordinal == 1 and f.size == 100
        assert q.get_file(c, "/nope") is None
        assert [f.path for f in q.group_members(c, "/g")] == ["/g/b01", "/g/b02", "/g/b10"]
        assert [f.path for f in q.files_by_tier(c, "cold")] == ["/g/b02", "/g/b10", "/h/x"]
        assert [f.path for f in q.files_by_tier(c, "cold", glob="/g/*")] == ["/g/b02", "/g/b10"]
        assert [f.path for f in q.files_under(c, "/g")] == ["/g/b01", "/g/b02", "/g/b10"]
        assert q.tier_totals(c) == {
            "hot": {"files": 1, "bytes": 100},
            "cold": {"files": 3, "bytes": 300},
        }
    # upsert keeps last_open, updates the rest
    with db.tx() as c:
        q.insert_access(c, [(500, "c", "open", "/g/b02", 0, "cold")])
        q.upsert_files(c, [frow("/g/b02", size=999, tier="hot", group="/g", ordinal=5)], seen=2)
        q.set_tier(c, "/h/x", "hot")
    with db.connection() as c:
        f = q.get_file(c, "/g/b02")
        assert f and f.size == 999 and f.tier == "hot" and f.last_open == 500 and f.ordinal == 5
        assert q.get_file(c, "/h/x").tier == "hot"


def test_delete_unseen_files_is_per_tier(db: Database):
    """AGENTS.md rule 3: a partial cold walk must never prune cold rows, and a
    complete hot walk must never touch cold rows."""
    seed_files(db, [frow("/hot/a", tier="hot"), frow("/cold/a"), frow("/cold/b")], seen=1)
    with db.tx() as c:
        q.upsert_files(c, [frow("/hot/a", tier="hot"), frow("/cold/a")], seen=2)
        removed = q.delete_unseen_files(c, tier="hot", seen=2)
    assert removed == 0
    with db.connection() as c:
        assert q.get_file(c, "/cold/b") is not None  # cold untouched by the hot prune
    with db.tx() as c:
        removed = q.delete_unseen_files(c, tier="cold", seen=2)
    assert removed == 1
    with db.connection() as c:
        assert q.get_file(c, "/cold/b") is None and q.get_file(c, "/cold/a") is not None
    with pytest.raises(ValueError), db.tx() as c:
        q.delete_unseen_files(c, tier="", seen=2)  # tier is mandatory


def test_paths_are_normalised(db: Database):
    """The relay and the scanner both produce paths; one stray slash must not
    fork a file into two rows."""
    assert q.norm_path("//a//b/") == "/a/b" and q.norm_path("a/b") == "/a/b"
    assert q.norm_path("/") == "/" and q.norm_path("") == "/"
    with db.tx() as c:
        q.upsert_files(c, [frow("/a/b/"), frow("a/b", size=7), frow("//a/b", size=9)], seen=1)
        q.insert_access(c, [(50, "c", "open", "a/b/", 0, "cold")])
        q.set_pin(c, "a//b/", "hot", None, "ui")
    with db.connection() as c:
        assert c.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 1
        for variant in ("/a/b", "a/b", "/a/b/", "//a/b"):
            f = q.get_file(c, variant)
            assert f and f.path == "/a/b" and f.size == 9 and f.last_open == 50, variant
        assert [r["path"] for r in q.access_for_path(c, "a/b")] == ["/a/b"]
        assert [r["path"] for r in q.list_pins(c, now=0)] == ["/a/b"]
        assert q.last_access(c, "//a/b/") == 50


@hsettings(max_examples=40, deadline=None)
@given(
    rows=st.lists(
        st.tuples(
            st.text(st.characters(blacklist_categories=("Cs", "Cc")), min_size=1, max_size=40),
            st.integers(min_value=0, max_value=1 << 45),
            st.sampled_from(["hot", "cold"]),
        ),
        min_size=1,
        max_size=20,
        unique_by=lambda t: "/" + "/".join(x for x in t[0].split("/") if x),
    ).filter(lambda rows: all(p.strip("/") for p, _s, _t in rows))
)
def test_files_roundtrip_property(tmp_path_factory, rows):
    db = Database(tmp_path_factory.mktemp("h") / "p.db")
    db.migrate()
    with db.tx() as c:
        q.upsert_files(c, [frow("/" + p, tier=t, size=s) for p, s, t in rows], seen=1)
    with db.connection() as c:
        for p, s, t in rows:
            f = q.get_file(c, "/" + p)
            assert f and f.size == s and f.tier == t
    db.close()


# --- access -----------------------------------------------------------------


def test_access_insert_since_last_and_prune(db: Database):
    seed_files(db, [frow("/a"), frow("/b")])
    with db.tx() as c:
        n = q.insert_access(
            c,
            [
                (10, "tv", "open", "/a", 0, "cold"),
                (20, "tv", "write", "/b", 5, "hot"),
                (30, "pc", "open", "/b", 0, None),
            ],
        )
    assert n == 3
    with db.connection() as c:
        assert q.get_file(c, "/a").last_open == 10
        assert q.get_file(c, "/b").last_open == 30  # write does not count as an open
        rows = q.access_since(c, 15)
        assert [(r["ts"], r["op"], r["tier"]) for r in rows] == [
            (20, "write", "hot"),
            (30, "open", None),
        ]
        assert [r["ts"] for r in q.access_since(c, 0, glob="/a*")] == [10]
        assert [r["ts"] for r in q.access_for_path(c, "/b")] == [20, 30]
        assert [r["ts"] for r in q.access_for_path(c, "/b", since=25)] == [30]
        assert q.last_access(c, "/a") == 10 and q.last_access(c, "/zzz") is None
    with db.tx() as c:
        assert q.prune_access(c, before=20) == 1
    with db.connection() as c:
        assert c.execute("SELECT COUNT(*) FROM access").fetchone()[0] == 2


# --- signals / context ------------------------------------------------------


def test_signal_defs_and_latest_values(db: Database):
    with db.tx() as c:
        q.upsert_signal_def(
            c,
            {
                "name": "presence",
                "kind": "enum",
                "values_json": json.dumps(["home", "away"]),
                "ttl_s": 900,
                "bucket_role": "primary",
                "source": "ha",
                "created": 1,
            },
        )
        q.upsert_signal_def(
            c,
            {
                "name": "presence",
                "kind": "enum",
                "values_json": json.dumps(["home", "away", "near"]),
                "ttl_s": 600,
                "bucket_role": "primary",
                "source": "ha",
                "created": 2,
            },
        )
        q.upsert_signal_def(
            c,
            {
                "name": "temp",
                "kind": "number",
                "values_json": None,
                "ttl_s": 60,
                "bucket_role": "none",
                "source": "x",
                "created": 3,
            },
        )
        q.upsert_signal_def(
            c,
            {
                "name": "never",
                "kind": "text",
                "values_json": None,
                "ttl_s": 60,
                "bucket_role": "feature",
                "source": "x",
                "created": 3,
            },
        )
        q.insert_signals(
            c,
            [(1, "presence", "home", "ha"), (5, "presence", "away", "ha"), (3, "temp", "21", "x")],
        )
    with db.connection() as c:
        defs = {r["name"]: r for r in q.signal_defs(c)}
        assert defs["presence"]["ttl_s"] == 600 and json.loads(defs["presence"]["values_json"]) == [
            "home",
            "away",
            "near",
        ]
        assert (
            q.get_signal_def(c, "temp")["kind"] == "number" and q.get_signal_def(c, "nope") is None
        )
        latest = q.latest_signals(c)
        assert latest["presence"]["value"] == "away" and latest["presence"]["ts"] == 5
        assert latest["temp"]["value"] == "21"
        assert [r["value"] for r in q.signal_history(c, "presence", since=0)] == ["home", "away"]
        # TTL: a value older than ttl_s is unknown, not stale. presence: ts 5, ttl 600.
        fresh = q.latest_values(c, now=500)
        assert fresh["presence"]["value"] == "away" and fresh["presence"]["expired"] is False
        assert fresh["presence"]["age_s"] == 495
        stale = q.latest_values(c, now=700)
        assert stale["presence"]["value"] is None and stale["presence"]["expired"] is True
        assert stale["presence"]["age_s"] == 695 and stale["presence"]["ts"] == 5
        assert stale["temp"]["value"] is None  # ts 3, ttl 60
        assert q.latest_values(c, now=65)["temp"]["value"] is None  # 3 + 60 < 65: expired
        assert q.latest_values(c, now=63)["temp"]["value"] == "21"  # age == ttl is still fresh
        never = stale["never"]  # declared, never emitted
        assert never["value"] is None and never["ts"] is None and never["age_s"] is None
        assert never["expired"] is True and never["bucket_role"] == "feature"
        assert set(stale) == {"presence", "temp", "never"}  # one entry per declared signal
    with db.tx() as c:
        assert q.delete_signal_def(c, "temp") == 1
        assert q.prune_signals(c, before=4) == 2
    with db.connection() as c:
        assert q.get_signal_def(c, "temp") is None
        assert q.latest_signals(c)["presence"]["ts"] == 5


def test_context_insert_and_get(db: Database):
    with db.tx() as c:
        cid = q.insert_context(
            c, 100, {"presence": {"value": "home"}}, "presence=home|d=day", degraded=False
        )
        cid2 = q.insert_context(c, 200, {}, "unknown", degraded=True)
    with db.connection() as c:
        row = q.get_context(c, cid)
        assert (
            row
            and json.loads(row["vector_json"]) == {"presence": {"value": "home"}}
            and row["degraded"] == 0
        )
        assert q.latest_context(c)["id"] == cid2 and q.latest_context(c)["degraded"] == 1
        assert q.get_context(c, 999) is None


# --- proposals / outcomes ---------------------------------------------------


def prop(
    arm: str,
    path: str,
    ts: int = 100,
    run: str = "r1",
    ctx: int | None = None,
    size: int = 10,
    window_h: int = 6,
    accepted: int | None = None,
    features: dict | None = None,
) -> dict:
    return {
        "ts": ts,
        "run_id": run,
        "arm": arm,
        "path": path,
        "reason_class": "similar",
        "reason": "because",
        "window_h": window_h,
        "size": size,
        "context_id": ctx,
        "accepted": accepted,
        "moved_at": None,
        "features_json": json.dumps(features) if features else None,
        "reject_reason": None,
    }


def test_proposals_lifecycle_feed_and_backing(db: Database):
    with db.tx() as c:
        cid = q.insert_context(c, 90, {}, "b1", False)
        ids = q.insert_proposals(
            c,
            [
                prop("seq", "/a", ctx=cid, accepted=1, features={"f": 1.0}),
                prop("seq", "/b", ctx=cid, accepted=0),
                prop("p-x", "/a", ctx=cid, accepted=1, ts=101),
            ],
        )
        q.set_proposal_accepted(c, ids[1], False, "budget")
        q.mark_proposal_moved(c, ids[0], moved_at=110)
        q.mark_proposal_moved(c, ids[2], moved_at=111)
    with db.connection() as c:
        assert len(ids) == 3
        assert q.get_proposal(c, ids[1])["reject_reason"] == "budget"
        assert [r["id"] for r in q.open_proposals(c)] == [ids[0], ids[2]]
        backing = q.backing_arms(c, now=120)
        assert sorted(backing["/a"]) == [("p-x", "b1"), ("seq", "b1")] and "/b" not in backing
        assert q.backing_arms(c, now=110 + 6 * 3600 + 1) == {}
        feed = q.proposals_feed(c, since=0)
        assert [r["id"] for r in feed] == [ids[2], ids[0], ids[1]]  # newest first
        assert [r["id"] for r in q.proposals_feed(c, arm="seq")] == [ids[0], ids[1]]
        assert [r["id"] for r in q.proposals_feed(c, state="rejected")] == [ids[1]]
        assert [r["id"] for r in q.proposals_feed(c, state="open")] == [ids[2], ids[0]]
        assert [r["arm"] for r in q.proposals_for_path(c, "/a")] == ["p-x", "seq"]
        assert q.count_labelled(c) == 0
    with db.tx() as c:
        q.set_outcome(c, ids[0], opened_at=130, hit=1)
        q.set_outcome(c, ids[2], opened_at=None, hit=0)
        q.set_feedback(c, ids[0], -1)
        q.set_feedback(c, ids[1], 1)  # feedback before any outcome
    with db.connection() as c:
        assert q.open_proposals(c) == []
        p0 = q.get_proposal(c, ids[0])
        assert p0["hit"] == 1 and p0["feedback"] == -1 and p0["opened_at"] == 130
        assert (
            q.get_proposal(c, ids[1])["feedback"] == 1 and q.get_proposal(c, ids[1])["hit"] is None
        )
        assert [r["id"] for r in q.proposals_feed(c, state="hit")] == [ids[0]]
        assert [r["id"] for r in q.proposals_feed(c, state="miss")] == [ids[2]]
        recent = q.arm_recent_proposals(c, "seq", limit=50)
        assert [(r["path"], r["hit"], r["feedback"]) for r in recent] == [
            ("/a", 1, -1),
            ("/b", None, 1),
        ]
        lab = q.labelled_proposals(c)
        assert (
            len(lab) == 1
            and json.loads(lab[0]["features_json"]) == {"f": 1.0}
            and q.count_labelled(c) == 1
        )
    with db.tx() as c:
        q.set_outcome(c, ids[0], opened_at=140, hit=1)  # idempotent re-score keeps feedback
    with db.connection() as c:
        assert q.get_proposal(c, ids[0])["feedback"] == -1


# --- moves ------------------------------------------------------------------


def test_moves_insert_lookup_and_counts(db: Database):
    with db.tx() as c:
        pid = q.insert_proposals(c, [prop("seq", "/a")])[0]
        q.insert_move(
            c,
            {
                "ts": 10,
                "path": "/a",
                "src": "cold",
                "dst": "hot",
                "reason": "seq",
                "proposal_id": pid,
                "bytes": 5,
                "ms": 3,
                "ok": 1,
                "err": None,
            },
        )
        q.insert_move(
            c,
            {
                "ts": 20,
                "path": "/a",
                "src": "hot",
                "dst": "cold",
                "reason": "evict",
                "proposal_id": None,
                "bytes": 5,
                "ms": 3,
                "ok": 0,
                "err": "ENOSPC",
            },
        )
    with db.connection() as c:
        rows = q.moves_for_path(c, "/a")
        assert [(r["ts"], r["arm"]) for r in rows] == [(20, None), (10, "seq")]
        assert q.moves_count_since(c, 0, ok=True) == 1 and q.moves_count_since(c, 0, ok=False) == 1
        assert q.moves_count_since(c, 15, ok=None) == 1
        assert [r["ts"] for r in q.moves_since(c, 0)] == [10, 20]


# --- posteriors -------------------------------------------------------------


def test_posteriors(db: Database):
    with db.connection() as c:
        assert q.get_posterior(c, "seq", "b") is None
    with db.tx() as c:
        q.set_posterior(c, "seq", "b", 3.0, 1.5)
        q.set_posterior(c, "seq", "b", 4.0, 1.5)
        q.set_posterior(c, "p-x", "b", 2.0, 1.0)
        q.set_posterior(c, "p-x", "c", 2.0, 2.0)
    with db.connection() as c:
        assert q.get_posterior(c, "seq", "b") == (4.0, 1.5)
        assert [(r["arm"], r["bucket"]) for r in q.all_posteriors(c)] == [
            ("p-x", "b"),
            ("p-x", "c"),
            ("seq", "b"),
        ]
        assert {r["arm"] for r in q.posteriors_for_bucket(c, "b")} == {"seq", "p-x"}


# --- schedules / sequences / pins -------------------------------------------


def test_schedules_keyed_by_source_and_name(db: Database):
    s = {
        "name": "nightly",
        "cron": "30 3 * * *",
        "action": "promote",
        "arms": json.dumps(["all"]),
        "filter": None,
        "enabled": 1,
        "source": "yaml",
        "window_h": 6,
        "until": None,
    }
    with db.tx() as c:
        a = q.upsert_schedule(c, s)
        b = q.upsert_schedule(c, {**s, "cron": "0 4 * * *"})
        i = q.upsert_schedule(c, {**s, "source": "intent", "until": 999})
    assert a == b != i
    with db.connection() as c:
        assert q.get_schedule(c, a)["cron"] == "0 4 * * *"
        assert [r["id"] for r in q.list_schedules(c)] == [a, i]
        assert [r["id"] for r in q.list_schedules(c, source="intent")] == [i]
    with db.tx() as c:
        q.set_schedule_enabled(c, a, False)
        assert q.delete_schedules_by_source(c, "intent") == 1
        assert q.delete_schedule(c, 12345) == 0
    with db.connection() as c:
        assert q.get_schedule(c, a)["enabled"] == 0 and q.get_schedule(c, i) is None


def test_sequences_replace_and_lookup(db: Database):
    with db.tx() as c:
        assert q.replace_sequences(c, [("/a", "/b", 7, 0.7, 3600), ("/a", "/c", 5, 0.5, 3600)]) == 2
        assert q.replace_sequences(c, [("/x", "/y", 9, 0.9, 3600)]) == 1
    with db.connection() as c:
        assert [(r["a"], r["b"]) for r in q.sequence_rules(c)] == [("/x", "/y")]
        assert q.rules_for(c, "/a") == [] and q.rules_for(c, "/x")[0]["support"] == 9


def test_pins_expiry_and_sources(db: Database):
    with db.tx() as c:
        q.set_pin(c, "/keep", "hot", None, "ui")
        q.set_pin(c, "/soon", "hot", 100, "intent")
        q.set_pin(c, "/keep", "cold", None, "cli")  # replaces
    with db.connection() as c:
        pins = {r["path"]: r for r in q.list_pins(c, now=50)}
        assert (
            pins["/keep"]["tier"] == "cold" and pins["/keep"]["source"] == "cli" and "/soon" in pins
        )
        assert [r["path"] for r in q.list_pins(c, now=150)] == ["/keep"]
    with db.tx() as c:
        assert q.delete_pins_by_source(c, "intent") == 1
        assert q.delete_pin(c, "/keep") == 1 and q.delete_pin(c, "/keep") == 0


# --- expectations (executor/api only) ---------------------------------------


def test_expectations_lifecycle(db: Database):
    with db.tx() as c:
        e1 = q.insert_expectation(c, "/a/*", deadline=100, bucket="b1")
        e2 = q.insert_expectation(c, "/b", deadline=200, bucket=None)
        e3 = q.insert_expectation(c, "/c", deadline=300, bucket=None)
    with db.connection() as c:
        assert [r["id"] for r in q.open_expectations(c)] == [e1, e2, e3]
        assert (
            q.get_expectation(c, e1)["path_glob"] == "/a/*"
            and q.get_expectation(c, e1)["missed"] == 0
        )
    with db.tx() as c:
        q.mark_expectation_met(c, e1, met_by="seq", met_at=90)
        q.mark_expectation_missed(c, e2)
    with db.connection() as c:
        assert [r["id"] for r in q.open_expectations(c)] == [e3]
        e = q.get_expectation(c, e1)
        assert e["met_by"] == "seq" and e["met_at"] == 90
        assert q.get_expectation(c, e2)["missed"] == 1
        listed = q.list_expectations(c)
        assert listed[0]["id"] == e3  # pending first
    with db.tx() as c:
        assert q.delete_expectation(c, e3) == 1 and q.delete_expectation(c, e3) == 0


# --- runs / counters / arm_state / metrics ----------------------------------


def test_runs_counters_arm_state_metrics(db: Database):
    run = {
        "id": "r1",
        "ts": 1,
        "action": "promote",
        "arms": json.dumps(["seq"]),
        "trigger": "timer",
        "dry_run": 1,
        "context_id": None,
        "proposals": 0,
        "accepted": 0,
        "moved": 0,
        "bytes": 0,
        "evicted": 0,
        "finished": None,
        "err": None,
    }
    with db.tx() as c:
        q.upsert_run(c, run)
        q.upsert_run(c, {**run, "proposals": 3, "finished": 2})
        q.upsert_run(c, {**run, "id": "r2", "ts": 5})
        q.bump_counter(c, "dropped")
        q.bump_counter(c, "dropped", 4)
        q.set_counter(c, "last_scored_ts", 77)
        q.set_arm_state(c, "p-x", enabled=False)
        q.set_arm_state(c, "p-x", schema_errors_add=2, last_error="schema")
        q.set_arm_state(c, "seq", last_run=9)
        q.set_metric(c, "2026-09-08", "first_open_hot_rate", 0.5)
        q.set_metric(c, "2026-09-08", "first_open_hot_rate", 0.75)
        q.set_metric(c, "2026-09-07", "hot_utilisation", 0.9)
    with db.connection() as c:
        assert [r["id"] for r in q.recent_runs(c, limit=5)] == ["r2", "r1"]
        assert q.get_run(c, "r1")["proposals"] == 3 and q.get_run(c, "r1")["finished"] == 2
        assert q.get_counter(c, "dropped") == 5 and q.get_counter(c, "last_scored_ts") == 77
        assert q.get_counter(c, "nope") == 0
        st_ = q.arm_states(c)
        assert (
            st_["p-x"]["enabled"] == 0
            and st_["p-x"]["schema_errors"] == 2
            and st_["p-x"]["last_error"] == "schema"
        )
        assert st_["seq"]["enabled"] == 1 and st_["seq"]["last_run"] == 9
        rows = q.metrics_rows(c, since_day="2026-09-08")
        assert [(r["day"], r["name"], r["value"]) for r in rows] == [
            ("2026-09-08", "first_open_hot_rate", 0.75)
        ]
        assert len(q.metrics_rows(c, since_day="2026-09-01", name="hot_utilisation")) == 1


def test_row_factory_gives_named_columns(db: Database):
    with db.connection() as c:
        assert isinstance(c.execute("SELECT 1 AS one").fetchone(), sqlite3.Row)
