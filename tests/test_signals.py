"""signals/: registry, vector, ingest. Build doc section 4; offline, against the
`db` and `settings` fixtures. Signal names here ("presence", "vpn.phone",
"workstation") are opaque strings: the daemon knows nothing about homes or phones.

Every `now` is passed explicitly; nothing asserts on time.time()."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import pytest

from ingenaning.config import Settings
from ingenaning.signals.ingest import MqttIngest, SignalBatch, SignalIn, emit, parse_payload
from ingenaning.signals.registry import (
    BUILTIN,
    SignalDef,
    clock_values,
    declare,
    declared,
    ensure_declared,
    ensure_primary,
    remove,
)
from ingenaning.signals.vector import build_context, build_vector, primary_summary
from ingenaning.store import queries as q
from ingenaning.store.db import Database

NOW = 1_757_355_000  # the example ts in build doc section 4.2
PRESENCE = ["present", "absent", "approaching"]

# --- helpers ---------------------------------------------------------------


def _def(
    name: str,
    kind: str = "text",
    values: list[str] | None = None,
    ttl_s: int = 900,
    role: str = "none",
    source: str = "test",
) -> SignalDef:
    return SignalDef.model_validate(
        {
            "name": name,
            "kind": kind,
            "values": values,
            "ttl_s": ttl_s,
            "bucket_role": role,
            "source": source,
        }
    )


def _declare(db: Database, *defs: SignalDef) -> None:
    with db.tx() as c:
        for d in defs:
            declare(c, d)


def _insert(db: Database, rows: list[tuple[int, str, str, str]]) -> None:
    with db.tx() as c:
        q.insert_signals(c, rows)


def _vector(db: Database, now: int) -> tuple[dict[str, dict[str, Any]], str, bool]:
    with db.connection() as c:
        return build_vector(c, now)


def _clock_bucket(now: int) -> str:
    clk = clock_values(now)
    return f"clock.daypart={clk['clock.daypart']}|clock.daytype={clk['clock.daytype']}"


def _local_ts(year: int, month: int, day: int, hour: int, minute: int = 0) -> int:
    """A naive datetime is local time, so the round trip through clock_values is
    the same wall clock whatever TZ the test host runs in."""
    return int(datetime(year, month, day, hour, minute).timestamp())


class _Recorder:
    """Stands in for the event bus: records every published message."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    def __call__(self, msg: dict[str, Any]) -> None:
        self.messages.append(msg)


# --- registry: SignalDef model ----------------------------------------------


def test_signal_def_defaults() -> None:
    """4.1: a def needs only a name; kind text, ttl 900, bucket_role none."""
    d = SignalDef(name="workstation")
    assert (d.kind, d.values, d.ttl_s, d.bucket_role, d.source) == ("text", None, 900, "none", "")


@pytest.mark.parametrize("bad", ["", "a b", "a\tb", "a|b", "a=b", "a|b=c"])
def test_signal_def_name_rejects_whitespace_pipe_and_equals(bad: str) -> None:
    """Names are bucket keys (name=value joined by '|'), so those characters are banned."""
    with pytest.raises(ValueError):
        SignalDef(name=bad)


def test_signal_def_name_allows_dots_and_dashes() -> None:
    """4.4: names like vpn.phone and clock.daypart are valid identifiers."""
    assert SignalDef(name="vpn.phone").name == "vpn.phone"
    assert SignalDef(name="net-link_2").name == "net-link_2"


def test_signal_def_rejects_unknown_kind_and_role() -> None:
    """4.1 table: kind is one of enum/number/bool/text, bucket_role one of four roles."""
    with pytest.raises(ValueError):
        SignalDef.model_validate({"name": "x", "kind": "blob"})
    with pytest.raises(ValueError):
        SignalDef.model_validate({"name": "x", "bucket_role": "tertiary"})


def test_to_row_matches_signal_defs_columns() -> None:
    """to_row() produces exactly the signal_defs columns; values_json is JSON or NULL."""
    row = _def("presence", kind="enum", values=PRESENCE, role="primary", source="ha").to_row()
    assert set(row) == {"name", "kind", "values_json", "ttl_s", "bucket_role", "source", "created"}
    assert json.loads(row["values_json"]) == PRESENCE
    assert (row["kind"], row["ttl_s"], row["bucket_role"], row["source"]) == (
        "enum",
        900,
        "primary",
        "ha",
    )
    assert isinstance(row["created"], int)
    assert _def("temp", kind="number").to_row()["values_json"] is None


def test_to_row_from_row_roundtrip_through_store(db: Database) -> None:
    """A def survives upsert_signal_def -> get_signal_def -> from_row unchanged."""
    defs = [
        _def("presence", kind="enum", values=PRESENCE, ttl_s=600, role="primary", source="ha"),
        _def("temp", kind="number", values=["-10", "40"], ttl_s=60, role="feature"),
        _def("vpn.phone", kind="bool", role="secondary"),
        _def("note"),
    ]
    with db.tx() as c:
        for d in defs:
            q.upsert_signal_def(c, d.to_row())
    with db.connection() as c:
        for d in defs:
            row = q.get_signal_def(c, d.name)
            assert row is not None
            assert SignalDef.from_row(row) == d


# --- registry: normalise ------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (True, "true"),
        (False, "false"),
        ("true", "true"),
        ("false", "false"),
        ("on", "true"),
        ("off", "false"),
        ("1", "true"),
        ("0", "false"),
        (1, "true"),
        (0, "false"),
        ("yes", "true"),
        ("no", "false"),
        ("connected", "true"),
        ("disconnected", "false"),
        ("ON", "true"),
        (" Off ", "false"),
    ],
)
def test_normalise_bool_vocabulary(raw: Any, expected: str) -> None:
    """bool accepts true/false, on/off, 1/0, yes/no, connected/disconnected, any case."""
    assert _def("vpn.phone", kind="bool").normalise(raw) == expected


@pytest.mark.parametrize("raw", ["maybe", "2", "enabled", "tru"])
def test_normalise_bool_rejects_other_words(raw: Any) -> None:
    """bool rejects anything outside its vocabulary with ValueError."""
    with pytest.raises(ValueError):
        _def("vpn.phone", kind="bool").normalise(raw)


def test_normalise_number_formats_and_range() -> None:
    """number stores a canonical string; values=[min,max] is an inclusive range."""
    free = _def("temp", kind="number")
    assert free.normalise(21) == "21"
    assert free.normalise("21") == "21"
    assert free.normalise(3.0) == "3"
    assert free.normalise("21.5") == "21.5"
    assert free.normalise(-4) == "-4"
    with pytest.raises(ValueError):
        free.normalise("warm")
    ranged = _def("temp", kind="number", values=["0", "100"])
    assert ranged.normalise(0) == "0" and ranged.normalise(100) == "100"
    assert ranged.normalise(50.5) == "50.5"
    for outside in (-1, 101, "100.1"):
        with pytest.raises(ValueError):
            ranged.normalise(outside)


def test_normalise_enum_membership() -> None:
    """enum accepts only its declared values (surrounding whitespace ignored)."""
    d = _def("presence", kind="enum", values=PRESENCE)
    assert d.normalise("absent") == "absent"
    assert d.normalise(" approaching ") == "approaching"
    with pytest.raises(ValueError):
        d.normalise("elsewhere")
    with pytest.raises(ValueError):
        d.normalise("ABSENT")  # enum values are exact
    assert _def("mode", kind="enum").normalise("anything") == "anything"  # no values: open


def test_normalise_text_passthrough() -> None:
    """text stores str(value) untouched."""
    d = _def("note")
    assert d.normalise("a b c") == "a b c"
    assert d.normalise(42) == "42"
    assert d.normalise("") == ""


# --- registry: builtin clock ----------------------------------------------------


def test_builtin_clock_defs() -> None:
    """4.2: clock.daypart and clock.daytype are built in, enum, primary, source builtin."""
    assert set(BUILTIN) == {"clock.daypart", "clock.daytype"}
    dp, dt = BUILTIN["clock.daypart"], BUILTIN["clock.daytype"]
    assert dp.kind == "enum" and dp.values == ["day", "evening", "night"]
    assert dt.kind == "enum" and dt.values == ["weekday", "weekend"]
    assert dp.bucket_role == dt.bucket_role == "primary"
    assert dp.source == dt.source == "builtin"
    assert dp.ttl_s > 86400 * 365 and dt.ttl_s > 86400 * 365  # never expire


@pytest.mark.parametrize(
    ("hour", "minute", "daypart"),
    [
        (0, 0, "night"),
        (6, 59, "night"),
        (7, 0, "day"),
        (12, 0, "day"),
        (16, 59, "day"),
        (17, 0, "evening"),
        (22, 59, "evening"),
        (23, 0, "night"),
        (23, 59, "night"),
    ],
)
def test_clock_daypart_boundaries(hour: int, minute: int, daypart: str) -> None:
    """daypart: night before 07:00 and from 23:00, evening from 17:00, day between."""
    ts = _local_ts(2026, 9, 9, hour, minute)  # a Wednesday
    assert clock_values(ts)["clock.daypart"] == daypart


def test_clock_daytype_weekday_weekend() -> None:
    """daytype: Mon-Fri weekday, Sat/Sun weekend, in local time."""
    assert clock_values(_local_ts(2026, 9, 9, 12))["clock.daytype"] == "weekday"  # Wed
    assert clock_values(_local_ts(2026, 9, 11, 23, 30))["clock.daytype"] == "weekday"  # Fri
    assert clock_values(_local_ts(2026, 9, 12, 12))["clock.daytype"] == "weekend"  # Sat
    assert clock_values(_local_ts(2026, 9, 13, 0, 30))["clock.daytype"] == "weekend"  # Sun


def test_clock_values_are_valid_for_the_builtin_defs() -> None:
    """clock_values only ever produces values the builtin enum defs accept."""
    vals = clock_values(NOW)
    assert set(vals) == set(BUILTIN)
    for name, v in vals.items():
        assert BUILTIN[name].normalise(v) == v


# --- registry: declare / declared / remove ------------------------------------


def test_declare_and_declared_include_builtins(db: Database) -> None:
    """declared() is the builtins plus every signal_defs row, keyed by name."""
    with db.connection() as c:
        base = declared(c)
    assert set(base) == set(BUILTIN)
    assert base["clock.daypart"] == BUILTIN["clock.daypart"]
    d = _def("presence", kind="enum", values=PRESENCE, ttl_s=600, role="primary", source="ha")
    _declare(db, d, _def("temp", kind="number", role="feature"))
    with db.connection() as c:
        defs = declared(c)
    assert set(defs) == set(BUILTIN) | {"presence", "temp"}
    assert defs["presence"] == d
    assert defs["temp"].bucket_role == "feature"


def test_declare_overwrites_existing_def(db: Database) -> None:
    """Re-declaring a name replaces its def (the operator can widen values or change role)."""
    _declare(db, _def("presence", kind="enum", values=PRESENCE[:2], role="none"))
    _declare(db, _def("presence", kind="enum", values=PRESENCE, ttl_s=300, role="primary"))
    with db.connection() as c:
        d = declared(c)["presence"]
    assert (d.values, d.ttl_s, d.bucket_role) == (PRESENCE, 300, "primary")


def test_declare_builtin_name_rejected(db: Database) -> None:
    """The clock signals cannot be redefined by a producer or operator."""
    with db.tx() as c, pytest.raises(ValueError):
        declare(c, _def("clock.daypart", kind="text"))
    with db.connection() as c:
        assert declared(c)["clock.daypart"] == BUILTIN["clock.daypart"]


def test_remove_declared_and_builtin_protected(db: Database) -> None:
    """remove() drops a declared def; builtins raise ValueError and stay."""
    _declare(db, _def("presence", kind="enum", values=PRESENCE, role="primary"))
    with db.tx() as c:
        assert remove(c, "presence") == 1
        assert remove(c, "presence") == 0
    with db.connection() as c:
        assert "presence" not in declared(c)
    with db.tx() as c, pytest.raises(ValueError):
        remove(c, "clock.daytype")
    with db.connection() as c:
        assert "clock.daytype" in declared(c)


def test_ensure_declared_creates_none_role_and_keeps_existing(db: Database) -> None:
    """4.2: unknown names create a def with bucket_role=none; known names are returned as is."""
    with db.tx() as c:
        d = ensure_declared(c, "workstation", "mqtt")
        assert (d.name, d.kind, d.bucket_role, d.source) == ("workstation", "text", "none", "mqtt")
        row = q.get_signal_def(c, "workstation")
        assert row is not None and row["bucket_role"] == "none" and row["kind"] == "text"
        declare(c, _def("presence", kind="enum", values=PRESENCE, role="primary", source="ha"))
        again = ensure_declared(c, "presence", "mqtt")
        assert again.bucket_role == "primary" and again.source == "ha"  # not overwritten
        assert ensure_declared(c, "clock.daypart", "mqtt") == BUILTIN["clock.daypart"]


def test_ensure_primary_declares_missing_and_upgrades_existing(
    db: Database, settings: Settings
) -> None:
    """policy signals.primary: absent names are declared primary; present ones get role primary."""
    s = settings.model_copy(update={"signals_primary": ["presence", "workstation"]})
    _declare(db, _def("workstation", kind="bool", ttl_s=120, role="none", source="ha"))
    with db.tx() as c:
        ensure_primary(c, s.signals_primary)
    with db.connection() as c:
        defs = declared(c)
    assert defs["presence"].bucket_role == "primary"
    ws = defs["workstation"]
    assert ws.bucket_role == "primary"
    assert (ws.kind, ws.ttl_s, ws.source) == ("bool", 120, "ha")  # only the role changed
    with db.tx() as c:
        ensure_primary(c, s.signals_primary)  # idempotent
    with db.connection() as c:
        assert declared(c)["workstation"] == ws


# --- vector: build_vector -----------------------------------------------------


def test_build_vector_one_entry_per_declared_signal(db: Database) -> None:
    """4.3: one entry per declared signal, each {value, age_s, role, source, kind}."""
    _declare(
        db,
        _def("presence", kind="enum", values=PRESENCE, role="primary", source="ha"),
        _def("vpn.phone", kind="bool", role="secondary"),
        _def("temp", kind="number", role="feature"),
        _def("note", role="none"),
    )
    _insert(db, [(NOW - 100, "presence", "absent", "ha"), (NOW - 5, "stray", "x", "cron")])
    vector, _, _ = _vector(db, NOW)
    assert set(vector) == {
        "clock.daypart",
        "clock.daytype",
        "presence",
        "vpn.phone",
        "temp",
        "note",
    }  # undeclared "stray" rows are ignored
    for entry in vector.values():
        assert set(entry) == {"value", "age_s", "role", "source", "kind"}
    assert vector["presence"] == {
        "value": "absent",
        "age_s": 100,
        "role": "primary",
        "source": "ha",
        "kind": "enum",
    }
    assert vector["vpn.phone"]["role"] == "secondary" and vector["vpn.phone"]["kind"] == "bool"
    assert vector["temp"]["role"] == "feature" and vector["note"]["role"] == "none"


def test_build_vector_clock_entries(db: Database) -> None:
    """4.2: the clock signals are always present, fresh, primary, source builtin."""
    vector, _, _ = _vector(db, NOW)
    clk = clock_values(NOW)
    for name in BUILTIN:
        assert vector[name] == {
            "value": clk[name],
            "age_s": 0,
            "role": "primary",
            "source": "builtin",
            "kind": "enum",
        }


def test_build_vector_ttl_boundary(db: Database) -> None:
    """4.1: after ttl_s without an update the value is unknown; age == ttl is still fresh."""
    _declare(db, _def("presence", kind="enum", values=PRESENCE, ttl_s=600, role="primary"))
    _insert(db, [(NOW, "presence", "absent", "ha")])
    fresh = _vector(db, NOW + 600)[0]["presence"]
    assert fresh["value"] == "absent" and fresh["age_s"] == 600
    stale = _vector(db, NOW + 601)[0]["presence"]
    assert stale["value"] is None and stale["age_s"] == 601


def test_build_vector_uses_latest_value_per_signal(db: Database) -> None:
    """The vector carries the newest row of each signal, whatever the insert order."""
    _declare(db, _def("presence", kind="enum", values=PRESENCE, role="primary"))
    _insert(
        db,
        [
            (NOW - 10, "presence", "approaching", "ha"),
            (NOW - 300, "presence", "absent", "ha"),
            (NOW - 60, "presence", "present", "ha"),
        ],
    )
    entry = _vector(db, NOW)[0]["presence"]
    assert entry["value"] == "approaching" and entry["age_s"] == 10


def test_build_vector_never_emitted_signal_is_unknown(db: Database) -> None:
    """A declared signal with no rows is unknown (value None, age None), source from its def."""
    _declare(db, _def("presence", kind="enum", values=PRESENCE, role="primary", source="ha"))
    entry = _vector(db, NOW)[0]["presence"]
    assert entry["value"] is None and entry["age_s"] is None
    assert entry["source"] == "ha" and entry["role"] == "primary"


def test_bucket_is_sorted_primaries_with_clock(db: Database) -> None:
    """4.3: bucket = cross product of primary signals plus the clock, as name=value joined by |."""
    _declare(
        db,
        _def("workstation", kind="bool", role="primary"),
        _def("presence", kind="enum", values=PRESENCE, role="primary"),
    )
    _insert(db, [(NOW - 1, "presence", "absent", "ha"), (NOW - 2, "workstation", "true", "ha")])
    _, bucket, degraded = _vector(db, NOW)
    assert bucket == f"{_clock_bucket(NOW)}|presence=absent|workstation=true"
    assert degraded is False


def test_bucket_unknown_for_expired_primary_and_degraded(db: Database) -> None:
    """4.3: expired primaries contribute 'unknown'; all-unknown primaries flag degraded."""
    _declare(db, _def("presence", kind="enum", values=PRESENCE, ttl_s=900, role="primary"))
    _insert(db, [(NOW - 901, "presence", "absent", "ha")])
    vector, bucket, degraded = _vector(db, NOW)
    assert vector["presence"]["value"] is None
    assert bucket == f"{_clock_bucket(NOW)}|presence=unknown"
    assert degraded is True


def test_never_emitted_primary_is_degraded(db: Database) -> None:
    """A primary that was declared but never emitted is unknown and therefore degraded."""
    _declare(db, _def("presence", kind="enum", values=PRESENCE, role="primary"))
    _, bucket, degraded = _vector(db, NOW)
    assert bucket.endswith("|presence=unknown") and degraded is True


def test_not_degraded_without_non_clock_primary(db: Database) -> None:
    """With only the clock as primary the bucket is the clock alone and never degraded."""
    _declare(db, _def("temp", kind="number", role="feature"))
    _, bucket, degraded = _vector(db, NOW)
    assert bucket == _clock_bucket(NOW)
    assert degraded is False


def test_not_degraded_when_one_of_two_primaries_known(db: Database) -> None:
    """degraded only when every non-clock primary is unknown, not when one is."""
    _declare(
        db,
        _def("presence", kind="enum", values=PRESENCE, role="primary"),
        _def("workstation", kind="bool", role="primary"),
    )
    _insert(db, [(NOW, "workstation", "true", "ha")])
    _, bucket, degraded = _vector(db, NOW)
    assert bucket == f"{_clock_bucket(NOW)}|presence=unknown|workstation=true"
    assert degraded is False


def test_secondary_and_feature_do_not_split_bucket(db: Database) -> None:
    """4.3: secondary and feature signals are in the vector but never in the bucket."""
    _declare(
        db,
        _def("presence", kind="enum", values=PRESENCE, role="primary"),
        _def("vpn.phone", kind="bool", role="secondary"),
        _def("temp", kind="number", role="feature"),
        _def("note", role="none"),
    )
    _insert(
        db,
        [
            (NOW, "presence", "present", "ha"),
            (NOW, "vpn.phone", "true", "ha"),
            (NOW, "temp", "21", "cron"),
            (NOW, "note", "hello", "cron"),
        ],
    )
    vector, bucket, degraded = _vector(db, NOW)
    assert bucket == f"{_clock_bucket(NOW)}|presence=present"
    assert degraded is False
    assert vector["vpn.phone"]["value"] == "true" and vector["temp"]["value"] == "21"
    assert vector["note"]["value"] == "hello"
    # an expired secondary does not make the run degraded either
    _declare(db, _def("vpn.phone", kind="bool", ttl_s=1, role="secondary"))
    vector, _, degraded = _vector(db, NOW + 5)
    assert vector["vpn.phone"]["value"] is None and degraded is False


# --- vector: build_context / primary_summary -----------------------------------


def test_build_context_persists_row(db: Database) -> None:
    """build_context stores ts, vector, bucket, degraded in `context` and returns its id."""
    # build_context's Context object (arms/base.py) is covered once arms/base.py lands;
    # here only its id and the persisted row are checked against the build_vector tuple.
    _declare(db, _def("presence", kind="enum", values=PRESENCE, role="primary", source="ha"))
    _insert(db, [(NOW - 30, "presence", "approaching", "ha")])
    with db.tx() as c:
        vector, bucket, degraded = build_vector(c, NOW)
        ctx = build_context(c, NOW, persist=True)
    assert ctx.id is not None
    with db.connection() as c:
        row = q.get_context(c, ctx.id)
        assert row is not None
        assert row["ts"] == NOW and row["bucket"] == bucket
        assert bool(row["degraded"]) is degraded is False
        assert json.loads(row["vector_json"]) == vector
        assert q.latest_context(c)["id"] == ctx.id


def test_build_context_without_persist_writes_nothing(db: Database) -> None:
    """persist=False returns the context with id None and leaves the table empty."""
    with db.tx() as c:
        ctx = build_context(c, NOW, persist=False)
    assert ctx.id is None
    with db.connection() as c:
        assert q.latest_context(c) is None


def test_primary_summary_only_primaries_sorted(db: Database) -> None:
    """primary_summary lists {name, value, age_s} for primary signals only, by name."""
    _declare(
        db,
        _def("workstation", kind="bool", role="primary"),
        _def("presence", kind="enum", values=PRESENCE, role="primary"),
        _def("vpn.phone", kind="bool", role="secondary"),
        _def("temp", kind="number", role="feature"),
    )
    _insert(db, [(NOW - 7, "presence", "absent", "ha"), (NOW, "vpn.phone", "true", "ha")])
    vector, _, _ = _vector(db, NOW)
    summary = primary_summary(vector)
    clk = clock_values(NOW)
    assert summary == [
        {"name": "clock.daypart", "value": clk["clock.daypart"], "age_s": 0},
        {"name": "clock.daytype", "value": clk["clock.daytype"], "age_s": 0},
        {"name": "presence", "value": "absent", "age_s": 7},
        {"name": "workstation", "value": None, "age_s": None},
    ]


# --- ingest: models -------------------------------------------------------------


def test_signal_in_and_batch_models() -> None:
    """4.2: an emitted signal is {name, value[, ts, source]}; a batch is {signals: [...]}."""
    s = SignalIn(name="presence", value="approaching")
    assert (s.ts, s.source) == (None, None)
    s2 = SignalIn.model_validate({"name": "temp", "value": 21.5, "ts": NOW, "source": "cron"})
    assert (s2.value, s2.ts, s2.source) == (21.5, NOW, "cron")
    assert SignalBatch().signals == []
    batch = SignalBatch.model_validate({"signals": [{"name": "a", "value": 1}]})
    assert [x.name for x in batch.signals] == ["a"]
    with pytest.raises(ValueError):
        SignalIn.model_validate({"name": "presence"})  # value is required


# --- ingest: emit -----------------------------------------------------------------


def test_emit_stores_rows_with_default_and_item_source(db: Database) -> None:
    """emit stores one signals row per accepted item; source is the item's, else the caller's."""
    _declare(db, _def("presence", kind="enum", values=PRESENCE, role="primary"))
    out = emit(
        db,
        [
            SignalIn(name="presence", value="absent"),
            SignalIn(name="temp", value=21, source="cron"),
        ],
    )
    assert out == {"accepted": 2, "rejected": [], "changed": ["presence"]}
    with db.connection() as c:
        latest = q.latest_signals(c)
    assert latest["presence"]["value"] == "absent" and latest["presence"]["source"] == "http"
    assert latest["temp"]["value"] == "21" and latest["temp"]["source"] == "cron"
    out = emit(db, [SignalIn(name="temp", value=22)], source="mqtt")
    assert out["accepted"] == 1
    with db.connection() as c:
        assert q.latest_signals(c)["temp"]["source"] == "mqtt"


def test_emit_normalises_values_per_def(db: Database) -> None:
    """Stored values are the def's canonical form (bool 'on' -> 'true', number 3.0 -> '3')."""
    _declare(db, _def("vpn.phone", kind="bool", role="secondary"), _def("temp", kind="number"))
    emit(db, [SignalIn(name="vpn.phone", value="on"), SignalIn(name="temp", value=3.0)])
    with db.connection() as c:
        latest = q.latest_signals(c)
    assert latest["vpn.phone"]["value"] == "true" and latest["temp"]["value"] == "3"


def test_emit_rejects_invalid_without_failing_batch(db: Database) -> None:
    """An invalid value is reported in rejected [{name, error}]; the rest of the batch lands."""
    _declare(
        db,
        _def("presence", kind="enum", values=PRESENCE, role="primary"),
        _def("temp", kind="number", values=["0", "100"], role="feature"),
    )
    out = emit(
        db,
        [
            SignalIn(name="presence", value="elsewhere"),
            SignalIn(name="temp", value=250),
            SignalIn(name="temp", value=21),
        ],
    )
    assert out["accepted"] == 1
    assert [r["name"] for r in out["rejected"]] == ["presence", "temp"]
    assert all(isinstance(r["error"], str) and r["error"] for r in out["rejected"])
    assert out["changed"] == []
    with db.connection() as c:
        latest = q.latest_signals(c)
    assert "presence" not in latest and latest["temp"]["value"] == "21"


def test_emit_auto_declares_unknown_name_with_role_none(db: Database) -> None:
    """4.2: unknown names create a signal_defs row with bucket_role=none and are stored."""
    out = emit(db, [SignalIn(name="workstation", value="on")], source="mqtt")
    assert out["accepted"] == 1 and out["changed"] == []
    with db.connection() as c:
        row = q.get_signal_def(c, "workstation")
        assert row is not None
        assert row["bucket_role"] == "none" and row["kind"] == "text" and row["source"] == "mqtt"
        assert q.latest_signals(c)["workstation"]["value"] == "on"
        vector, bucket, _ = build_vector(c, NOW)
    assert vector["workstation"]["role"] == "none" and "workstation" not in bucket


def test_emit_honours_explicit_ts(db: Database) -> None:
    """4.2: an item's ts is stored as given; items without ts get the intake time."""
    _declare(db, _def("presence", kind="enum", values=PRESENCE, role="primary"))
    emit(db, [SignalIn(name="presence", value="approaching", ts=NOW)])
    with db.connection() as c:
        latest = q.latest_signals(c)
        assert latest["presence"]["ts"] == NOW
        assert build_vector(c, NOW + 10)[0]["presence"]["age_s"] == 10
    emit(db, [SignalIn(name="temp", value=1)])
    with db.connection() as c:
        ts = q.latest_signals(c)["temp"]["ts"]
    assert isinstance(ts, int) and ts > NOW  # intake time, not the epoch


def test_emit_publishes_context_on_primary_change(db: Database) -> None:
    """A primary changing value publishes exactly one {"type": "context", ...} message."""
    _declare(db, _def("presence", kind="enum", values=PRESENCE, role="primary"))
    rec = _Recorder()
    out = emit(db, [SignalIn(name="presence", value="absent")], publish=rec)
    assert out["changed"] == ["presence"]
    assert len(rec.messages) == 1
    msg = rec.messages[0]
    assert msg["type"] == "context" and msg["changed"] == ["presence"]
    assert "presence=absent" in msg["bucket"] and msg["degraded"] is False
    assert msg["vector"]["presence"]["value"] == "absent"
    assert msg["vector"]["presence"]["role"] == "primary"


def test_emit_no_publish_on_repeat_value(db: Database) -> None:
    """Re-emitting the same value is stored but is not a change: no message, changed empty."""
    _declare(db, _def("presence", kind="enum", values=PRESENCE, role="primary"))
    rec = _Recorder()
    emit(db, [SignalIn(name="presence", value="absent")], publish=rec)
    out = emit(db, [SignalIn(name="presence", value="absent")], publish=rec)
    assert out == {"accepted": 1, "rejected": [], "changed": []}
    assert len(rec.messages) == 1
    out = emit(db, [SignalIn(name="presence", value="present")], publish=rec)
    assert out["changed"] == ["presence"] and len(rec.messages) == 2
    assert "presence=present" in rec.messages[1]["bucket"]
    with db.connection() as c:
        history = [r["value"] for r in q.signal_history(c, "presence", since=0)]
    assert history == ["absent", "absent", "present"]  # every emission is stored


def test_emit_publishes_on_secondary_change_not_feature_or_none(db: Database) -> None:
    """Only primary and secondary changes publish; feature and none signals never do."""
    _declare(
        db,
        _def("vpn.phone", kind="bool", role="secondary"),
        _def("temp", kind="number", role="feature"),
    )
    rec = _Recorder()
    out = emit(
        db,
        [
            SignalIn(name="vpn.phone", value="on"),
            SignalIn(name="temp", value=21),
            SignalIn(name="workstation", value="on"),  # undeclared -> none
        ],
        publish=rec,
    )
    assert out["accepted"] == 3 and out["changed"] == ["vpn.phone"]
    assert len(rec.messages) == 1 and rec.messages[0]["changed"] == ["vpn.phone"]
    out = emit(
        db,
        [SignalIn(name="temp", value=22), SignalIn(name="workstation", value="off")],
        publish=rec,
    )
    assert out["accepted"] == 2 and out["changed"] == []
    assert len(rec.messages) == 1


def test_emit_one_message_for_a_batch_with_several_changes(db: Database) -> None:
    """A batch changing two bucket signals publishes once, naming both in changed."""
    _declare(
        db,
        _def("presence", kind="enum", values=PRESENCE, role="primary"),
        _def("vpn.phone", kind="bool", role="secondary"),
    )
    rec = _Recorder()
    out = emit(
        db,
        [SignalIn(name="presence", value="absent"), SignalIn(name="vpn.phone", value="off")],
        publish=rec,
    )
    assert sorted(out["changed"]) == ["presence", "vpn.phone"]
    assert len(rec.messages) == 1
    assert sorted(rec.messages[0]["changed"]) == ["presence", "vpn.phone"]


def test_emit_rejected_primary_is_not_a_change(db: Database) -> None:
    """A rejected value for a primary neither publishes nor appears in changed."""
    _declare(db, _def("presence", kind="enum", values=PRESENCE, role="primary"))
    rec = _Recorder()
    out = emit(db, [SignalIn(name="presence", value="elsewhere")], publish=rec)
    assert out["accepted"] == 0 and out["changed"] == []
    assert [r["name"] for r in out["rejected"]] == ["presence"]
    assert rec.messages == []


def test_emit_without_publish_callable(db: Database) -> None:
    """publish is optional: a change with no bus is stored and reported, nothing raised."""
    _declare(db, _def("presence", kind="enum", values=PRESENCE, role="primary"))
    out = emit(db, [SignalIn(name="presence", value="absent")])
    assert out["changed"] == ["presence"] and out["accepted"] == 1
    out = emit(db, [SignalIn(name="presence", value="absent")], publish=None)
    assert out["changed"] == []


def test_emit_empty_batch(db: Database) -> None:
    """An empty batch is accepted as nothing: no rows, no rejects, no message."""
    rec = _Recorder()
    assert emit(db, [], publish=rec) == {"accepted": 0, "rejected": [], "changed": []}
    assert rec.messages == []
    with db.connection() as c:
        assert q.latest_signals(c) == {}


# --- ingest: MQTT payloads ------------------------------------------------------


def test_parse_payload_plain_value_on_named_topic() -> None:
    """4.2: ingenaning/signal/<name> with the bare value as payload."""
    items = parse_payload("approaching", "presence")
    assert [(i.name, i.value, i.ts) for i in items] == [("presence", "approaching", None)]
    items = parse_payload(b"on", "vpn.phone")
    assert [(i.name, i.value) for i in items] == [("vpn.phone", "on")]
    items = parse_payload(b"  21.5\n", "temp")
    assert items[0].name == "temp" and float(items[0].value) == 21.5


def test_parse_payload_json_object_on_named_topic() -> None:
    """A named topic also accepts {"value": .., "ts": .., "source": ..}."""
    items = parse_payload(json.dumps({"value": "absent", "ts": NOW}), "presence")
    assert len(items) == 1
    assert (items[0].name, items[0].value, items[0].ts) == ("presence", "absent", NOW)
    items = parse_payload(b'{"value": true, "source": "ha"}', "vpn.phone")
    assert (items[0].value, items[0].source) == (True, "ha")


def test_parse_payload_batch_on_bare_topic() -> None:
    """The bare topic takes a JSON list, a {"signals": [...]} batch, or one object."""
    raw = [{"name": "presence", "value": "absent"}, {"name": "temp", "value": 21, "ts": NOW}]
    items = parse_payload(json.dumps(raw))
    assert [(i.name, i.value, i.ts) for i in items] == [
        ("presence", "absent", None),
        ("temp", 21, NOW),
    ]
    items = parse_payload(json.dumps({"signals": raw}).encode())
    assert [i.name for i in items] == ["presence", "temp"]
    items = parse_payload(json.dumps({"name": "workstation", "value": "on"}), None)
    assert [(i.name, i.value) for i in items] == [("workstation", "on")]


def test_parse_payload_bad_bare_payload_raises() -> None:
    """Unparseable or nameless payloads on the bare topic raise ValueError, never store."""
    with pytest.raises(ValueError):
        parse_payload("not json")
    with pytest.raises(ValueError):
        parse_payload(json.dumps({"value": "absent"}))  # no name on the bare topic
    with pytest.raises(ValueError):
        parse_payload(json.dumps([{"name": "presence"}]))  # value missing


def test_parsed_payload_feeds_emit(db: Database) -> None:
    """MQTT and HTTP land in the same emit(): a parsed payload is stored with source mqtt."""
    _declare(db, _def("presence", kind="enum", values=PRESENCE, role="primary"))
    rec = _Recorder()
    out = emit(db, parse_payload(b"approaching", "presence"), source="mqtt", publish=rec)
    assert out["accepted"] == 1 and out["changed"] == ["presence"]
    with db.connection() as c:
        row = q.latest_signals(c)["presence"]
    assert row["value"] == "approaching" and row["source"] == "mqtt"
    assert len(rec.messages) == 1


def test_mqtt_ingest_start_false_without_config(settings: Settings, db: Database) -> None:
    """With settings.mqtt None the intake is disabled: start() is False, nothing connects."""
    assert settings.mqtt is None
    ingest = MqttIngest(settings=settings, db=db)
    assert ingest.start() is False
    ingest.stop()  # safe when never started
