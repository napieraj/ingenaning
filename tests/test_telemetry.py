"""Telemetry: keys, the host relay, the container reader, the branch scanner.

Everything here runs offline. There is no fatrace, no NFS, no network: the relay
parser is fed strings, the socket tests serve a unix socket in-process, and the
scanner walks tmp_path directories."""

from __future__ import annotations

import json
import os
import socket
import stat
import threading
import time
from collections.abc import Callable
from pathlib import Path

from conftest import access_line, touch

from ingenaning.config import PinRule, Settings
from ingenaning.store import queries as q
from ingenaning.store.db import Database
from ingenaning.telemetry import fatrace, keys, relay, scanner


def wait_for(pred: Callable[[], bool], timeout: float = 5.0) -> bool:
    """Poll `pred` at 10 ms. Used instead of a sleep so the suite stays fast."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


# --- keys -------------------------------------------------------------------------


def test_group_key_is_the_parent_directory():
    assert keys.group_key("/a/b/c.txt") == "/a/b"
    assert keys.group_key("/a//b///c.txt") == "/a/b"
    assert keys.group_key("/top.txt") == "/"
    assert keys.group_key("/") == "/"


def test_ordinal_sorts_naturally_within_the_group():
    group = ["/a/b10.txt", "/a/b2.txt", "/a/b1.txt"]
    assert keys.ordinal("/a/b1.txt", group) == 0
    assert keys.ordinal("/a/b2.txt", group) == 1
    assert keys.ordinal("/a/b10.txt", group) == 2
    assert keys.ordinal("/a/missing.txt", group) is None


def test_assign_ordinals_numbers_each_group_on_its_own():
    paths = ["/a/b10", "/a/b2", "/z/q1", "/z/q10", "/z/q9"]
    got = keys.assign_ordinals(paths)
    assert got["/a/b2"] == 0 and got["/a/b10"] == 1
    assert got["/z/q1"] == 0 and got["/z/q9"] == 1 and got["/z/q10"] == 2


def test_assign_ordinals_is_stable_for_names_that_only_differ_in_padding():
    got = keys.assign_ordinals(["/a/b02", "/a/b2", "/a/b1"])
    assert got["/a/b1"] == 0
    assert sorted([got["/a/b02"], got["/a/b2"]]) == [1, 2]


# --- relay: parsing ---------------------------------------------------------------


def test_fatrace_command_has_no_path_argument_and_no_read_filter():
    # D-005: the build doc's `-t -f RWO /srv/nas` is wrong. -c watches the mount
    # of the cwd, so the branch is the cwd, never an argument.
    assert relay.FATRACE_CMD == ("fatrace", "-c", "-tt", "-f", "CWO")
    assert "R" not in relay.FATRACE_FILTER
    assert not any(arg.startswith("/") for arg in relay.FATRACE_CMD)


def test_pump_runs_fatrace_with_the_branch_as_cwd():
    seen: dict[str, object] = {}

    class FakeProc:
        stdout = iter(())

        def wait(self, timeout: float | None = None) -> int:
            return 0

        def poll(self) -> int:
            return 0

    def fake_popen(cmd: list[str], **kw: object) -> FakeProc:
        seen["cmd"] = list(cmd)
        seen["cwd"] = kw.get("cwd")
        return FakeProc()

    r = relay.Relay({"hot": "/tank/hot"}, "/dev/null", relay.ClientResolver(None))

    def spawn(cmd: list[str], **kw: object) -> FakeProc:
        r.stop.set()  # one pass through the restart loop, then out
        return fake_popen(cmd, **kw)

    r.popen = spawn  # type: ignore[assignment]
    r._pump("hot", "/tank/hot")
    assert seen["cmd"] == list(relay.FATRACE_CMD)
    assert seen["cwd"] == "/tank/hot"


def test_parse_line_reads_epoch_timestamps_as_float():
    line = "1757355000.123456 smbd(4242): O /tank/hot/a/b.bin\n"
    parsed = relay.parse_fatrace_line(line, "/tank/hot", now=999.0)
    assert parsed == (1757355000, "smbd", 4242, "open", "/a/b.bin")


def test_parse_line_without_a_timestamp_uses_the_receiver_clock():
    parsed = relay.parse_fatrace_line("nfsd(7): O /tank/hot/x", "/tank/hot", now=1_700_000_000.9)
    assert parsed is not None and parsed[0] == 1_700_000_000


def test_parse_line_classifies_ops_and_drops_closes_without_writes():
    def op(ops: str) -> str | None:
        got = relay.parse_fatrace_line(f"1.0 p(1): {ops} /r/f", "/r", now=0.0)
        return None if got is None else got[3]

    assert op("O") == "open"
    assert op("W") == "write"
    assert op("CW") == "write"
    assert op("C") is None


def test_parse_line_ignores_other_mounts_and_deleted_files():
    assert relay.parse_fatrace_line("1.0 p(1): O /other/f", "/r", now=0.0) is None
    assert relay.parse_fatrace_line("1.0 p(1): O /rogue/f", "/r", now=0.0) is None
    assert relay.parse_fatrace_line("1.0 p(1): O /r/f (deleted)", "/r", now=0.0) is None
    assert relay.parse_fatrace_line("not a fatrace line", "/r", now=0.0) is None


def test_resolver_does_not_leak_process_names_as_clients():
    # PRIVACY.md principle 2: `access` stores the client, never process names.
    res = relay.ClientResolver(None, runner=lambda cmd: "")
    assert res.resolve("ffmpeg", 1) == "local"
    assert res.resolve("nfsd", 1) == "nfs"


def test_resolver_names_the_only_nfs_client(tmp_path: Path):
    res = relay.ClientResolver(None, runner=lambda cmd: "")
    res._nfs = ["10.20.0.31"]
    res._at = time.monotonic()
    assert res.resolve("nfsd", 1) == "10.20.0.31"
    res._nfs = ["10.20.0.31", "10.20.0.32"]
    assert res.resolve("nfsd", 1) == "nfs"


# --- relay: buffer and socket -----------------------------------------------------


def test_emit_buffers_while_no_consumer_and_counts_what_it_drops():
    r = relay.Relay({}, "/dev/null", relay.ClientResolver(None), buffer_lines=2)
    for i in range(4):
        r.emit(f'{{"n":{i}}}\n'.encode())
    assert [line.strip() for line in r.buf] == [b'{"n":2}', b'{"n":3}']
    assert r.dropped == 2


def test_serve_replays_the_buffer_and_reports_drops_first(tmp_path: Path):
    sock = tmp_path / "access.sock"
    r = relay.Relay({}, str(sock), relay.ClientResolver(None), buffer_lines=1)
    r.emit(b'{"n":1}\n')
    r.emit(b'{"n":2}\n')  # drops the first
    t = threading.Thread(target=r._serve, daemon=True)
    t.start()
    try:
        assert wait_for(sock.exists)
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.connect(str(sock))
        c.settimeout(5.0)
        data = b""
        while data.count(b"\n") < 2:
            chunk = c.recv(4096)
            assert chunk, "relay closed before replaying the buffer"
            data += chunk
        first, second = data.decode().splitlines()[:2]
        assert json.loads(first) == {"type": "dropped", "count": 1}
        assert json.loads(second) == {"n": 2}
        assert wait_for(lambda: len(r.clients) == 1)
        r.emit(b'{"n":3}\n')  # live now: goes to the consumer, not the buffer
        while data.count(b"\n") < 3:
            data += c.recv(4096)
        assert json.loads(data.decode().splitlines()[2]) == {"n": 3}
        assert not r.buf
        c.close()
    finally:
        r.stop.set()
        t.join(5)


# --- relay: audit chain -----------------------------------------------------------


def _chain(n: int) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    prev = relay.GENESIS
    for i in range(1, n + 1):
        row: dict[str, object] = {
            "seq": i,
            "ts": 1_757_356_000 + i,
            "prev": prev,
            "event": "move",
            "actor": "test",
            "data": {"path": f"/f{i}"},
        }
        row["hash"] = relay.line_hash(row)
        out.append(row)
        prev = str(row["hash"])
    return out


def test_verify_chain_accepts_json_text_lines():
    lines = [json.dumps(row, sort_keys=True) for row in _chain(3)]
    assert relay.verify_chain(lines) == (True, None)
    assert relay.verify_chain([]) == (True, None)


def test_verify_chain_reports_a_broken_link():
    lines = _chain(4)
    lines[2]["prev"] = relay.GENESIS
    assert relay.verify_chain(lines) == (False, 3)


def test_verify_chain_reports_a_malformed_line():
    ok, at = relay.verify_chain(["{not json"])
    assert not ok and at is None


# --- container reader: parsing ----------------------------------------------------


def test_parse_json_lines_counts_dropped_markers_and_rubbish():
    counts = fatrace.LineCounts()
    lines = [
        access_line(10, "10.20.0.31", "open", "/a/x", "cold"),
        '{"type":"dropped","count":7}\n',
        "not json\n",
        '{"ts":11,"op":"open"}\n',  # no path
        "\n",
        access_line(12, "10.20.0.31", "write", "/a/y", "hot"),
    ]
    got = list(fatrace.parse_json_lines(lines, counts))
    assert [e.path for e in got] == ["/a/x", "/a/y"]
    assert got[0].tier == "cold" and got[1].tier == "hot"
    assert counts.events == 2
    assert counts.dropped == 7
    assert counts.malformed == 2


def test_parse_json_lines_takes_bytes_and_out_of_order_stamps():
    got = list(fatrace.parse_json_lines([access_line(50, "c", "open", "//a//x").encode()]))
    assert got[0].path == "/a/x" and got[0].ts == 50


# --- container reader: batching ---------------------------------------------------


def _ev(ts: int, path: str = "/a/x", op: str = "open", client: str = "c") -> fatrace.AccessEvent:
    return fatrace.AccessEvent(ts=ts, client=client, op=op, path=path, tier="hot")


def test_batcher_coalesces_repeats_within_the_window(db: Database):
    clock = [100.0]
    b = fatrace.Batcher(db, max_rows=10, max_wait_s=60.0, coalesce_s=5.0, now_fn=lambda: clock[0])
    for ts in (10, 11, 12):
        b.add(_ev(ts))
    clock[0] += 10.0
    b.add(_ev(20))
    assert b.flush() == 2
    assert b.coalesced == 2
    with db.connection() as conn:
        rows = q.access_since(conn, 0)
    assert [(r["ts"], r["path"], r["tier"]) for r in rows] == [
        (12, "/a/x", "hot"),
        (20, "/a/x", "hot"),
    ]


def test_batcher_keeps_the_newest_stamp_when_lines_arrive_out_of_order(db: Database):
    b = fatrace.Batcher(db, max_rows=10, max_wait_s=60.0, now_fn=lambda: 0.0)
    b.add(_ev(30))
    b.add(_ev(20))
    b.flush()
    with db.connection() as conn:
        assert [r["ts"] for r in q.access_since(conn, 0)] == [30]


def test_batcher_flushes_at_max_rows_and_advances_last_open(db: Database):
    with db.tx() as conn:
        q.upsert_files(conn, [{"path": "/a/x", "size": 5, "tier": "hot", "mtime": 1}], seen=1)
    b = fatrace.Batcher(db, max_rows=2, max_wait_s=60.0, now_fn=lambda: 0.0)
    b.add(_ev(10, "/a/x"))
    b.add(_ev(10, "/a/y"))
    assert b.flushed == 2
    with db.connection() as conn:
        row = q.get_file(conn, "/a/x")
    assert row is not None and row.last_open == 10


def test_batcher_tick_flushes_an_idle_buffer(db: Database):
    clock = [0.0]
    b = fatrace.Batcher(db, max_rows=1000, max_wait_s=1.0, now_fn=lambda: clock[0])
    b.add(_ev(10))
    b.tick()
    assert b.flushed == 0
    clock[0] += 2.0
    b.tick()
    assert b.flushed == 1


def test_batcher_survives_a_store_error(db: Database, tmp_path: Path):
    b = fatrace.Batcher(db, max_rows=1000, max_wait_s=60.0, now_fn=lambda: 0.0)
    b.add(_ev(10))
    db.close()
    db.path = tmp_path  # a directory: every connection from here on fails
    assert b.flush() == 0
    assert b.failed == 1


# --- container reader: socket -----------------------------------------------------


class _FakeRelay:
    """A unix socket that hands a consumer some lines and then holds the
    connection open, like the real relay does."""

    def __init__(self, path: Path, payload: bytes):
        self.path = path
        self.payload = payload
        self.stop = threading.Event()
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(str(path))
        self.srv.listen(1)
        self.srv.settimeout(0.05)
        self.conns: list[socket.socket] = []
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self.stop.is_set():
            try:
                conn, _ = self.srv.accept()
            except (TimeoutError, OSError):
                continue
            self.conns.append(conn)
            conn.sendall(self.payload)
        for c in self.conns:
            c.close()
        self.srv.close()

    def __enter__(self) -> _FakeRelay:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop.set()
        self.thread.join(5)


def test_socket_reader_inserts_what_the_relay_sends(settings: Settings, db: Database):
    payload = (
        access_line(10, "10.20.0.31", "open", "/a/x", "cold")
        + '{"type":"dropped","count":3}\n'
        + access_line(11, "10.20.0.31", "open", "/a/y", "hot")
    ).encode()
    b = fatrace.Batcher(db, max_rows=1, max_wait_s=60.0, now_fn=lambda: 0.0)
    stop = threading.Event()
    reader = fatrace.SocketReader(settings.socket_path, b, stop, poll_s=0.05)
    with _FakeRelay(settings.socket_path, payload):
        t = threading.Thread(target=reader.run, daemon=True)
        t.start()
        try:
            assert wait_for(lambda: b.flushed >= 2)
        finally:
            stop.set()
            t.join(5)
    with db.connection() as conn:
        rows = q.access_since(conn, 0)
    assert [(r["path"], r["tier"]) for r in rows] == [("/a/x", "cold"), ("/a/y", "hot")]
    assert reader.counts.dropped == 3


def test_socket_reader_flushes_on_the_poll_timeout(settings: Settings, db: Database):
    payload = access_line(10, "c", "open", "/a/x").encode()
    b = fatrace.Batcher(db, max_rows=1000, max_wait_s=0.0, now_fn=time.monotonic)
    stop = threading.Event()
    reader = fatrace.SocketReader(settings.socket_path, b, stop, poll_s=0.02)
    with _FakeRelay(settings.socket_path, payload):
        t = threading.Thread(target=reader.run, daemon=True)
        t.start()
        try:
            assert wait_for(lambda: b.flushed == 1)
        finally:
            stop.set()
            t.join(5)


def test_socket_reader_backs_off_when_the_relay_is_absent(settings: Settings, db: Database):
    b = fatrace.Batcher(db, now_fn=lambda: 0.0)
    stop = threading.Event()
    reader = fatrace.SocketReader(
        settings.socket_path, b, stop, poll_s=0.02, backoff_s=0.01, max_backoff_s=0.02
    )
    t = threading.Thread(target=reader.run, daemon=True)
    t.start()
    try:
        assert wait_for(lambda: reader.failures >= 2)
    finally:
        stop.set()
        t.join(5)
    assert reader.connects == 0


def test_run_socket_reads_the_configured_socket_path(settings: Settings, db: Database):
    # D-008 puts the socket in Settings; it is not derived from state_dir.
    stop = threading.Event()
    stop.set()
    reader = fatrace.run_socket(settings, db, stop)
    assert reader.sock_path == str(settings.socket_path)
    assert reader.connects == 0


# --- scanner ----------------------------------------------------------------------


def test_scan_records_both_branches_with_tier_group_and_ordinal(
    settings: Settings, db: Database, branches: tuple[Path, Path, Path]
):
    hot, cold, _ = branches
    touch(hot, "/g/b2.bin", 10, 1000)
    touch(hot, "/g/b10.bin", 20, 1001)
    touch(cold, "/g/b1.bin", 30, 1002)
    result = scanner.scan(settings, db, now=500)
    assert (result.hot, result.cold, result.dual) == (2, 1, 0)
    assert result.hot_complete and result.cold_complete
    with db.connection() as conn:
        rows = {r.path: r for r in q.files_by_tier(conn, "hot") + q.files_by_tier(conn, "cold")}
    assert rows["/g/b2.bin"].tier == "hot" and rows["/g/b1.bin"].tier == "cold"
    assert rows["/g/b2.bin"].size == 10 and rows["/g/b2.bin"].mtime == 1000
    assert {r.group_key for r in rows.values()} == {"/g"}
    assert [rows[p].ordinal for p in ("/g/b1.bin", "/g/b2.bin", "/g/b10.bin")] == [0, 1, 2]


def test_scan_treats_a_file_on_both_branches_as_hot(
    settings: Settings, db: Database, branches: tuple[Path, Path, Path]
):
    hot, cold, _ = branches
    touch(hot, "/g/x.bin", 10)
    touch(cold, "/g/x.bin", 99)
    result = scanner.scan(settings, db, now=500)
    assert result.dual == 1 and result.hot == 1 and result.cold == 0
    with db.connection() as conn:
        row = q.get_file(conn, "/g/x.bin")
    assert row is not None and row.tier == "hot" and row.size == 10


def test_scan_skips_symlinks_specials_and_skip_dirs(
    settings: Settings, db: Database, branches: tuple[Path, Path, Path]
):
    hot, _cold, _ = branches
    touch(hot, "/g/real.bin", 8)
    os.symlink(str(hot / "g/real.bin"), str(hot / "g/link.bin"))
    touch(hot, "/.snapshots/old.bin", 8)
    os.mkfifo(str(hot / "g/pipe"))
    scanner.scan(settings, db, now=500)
    with db.connection() as conn:
        assert [r.path for r in q.files_by_tier(conn, "hot")] == ["/g/real.bin"]


def test_scan_prunes_only_the_branch_whose_walk_completed(
    settings: Settings, db: Database, branches: tuple[Path, Path, Path], monkeypatch
):
    _hot, _cold, _ = branches
    with db.tx() as conn:
        q.upsert_files(
            conn,
            [
                {"path": "/gone-hot.bin", "size": 1, "tier": "hot", "mtime": 1},
                {"path": "/gone-cold.bin", "size": 1, "tier": "cold", "mtime": 1},
            ],
            seen=1,
        )
    real = scanner.walk_branch

    def half(tier: str, root: Path, timeout_s: float = 1.0) -> scanner.BranchWalk:
        walk = real(tier, root, timeout_s)
        if tier == "cold":
            walk.complete = False
            walk.error = "timed out"
        return walk

    monkeypatch.setattr(scanner, "walk_branch", half)
    result = scanner.scan(settings, db, now=500)
    assert not result.cold_complete and result.removed == 1
    with db.connection() as conn:
        paths = {r.path for r in q.files_by_tier(conn, "hot") + q.files_by_tier(conn, "cold")}
    assert paths == {"/gone-cold.bin"}


def test_scan_skips_the_cold_walk_when_the_probe_fails(
    settings: Settings, db: Database, branches: tuple[Path, Path, Path], monkeypatch
):
    hot, cold, _ = branches
    touch(hot, "/a.bin", 1)
    touch(cold, "/b.bin", 1)
    with db.tx() as conn:
        q.upsert_files(conn, [{"path": "/b.bin", "size": 1, "tier": "cold", "mtime": 1}], seen=1)
    monkeypatch.setattr(scanner, "cold_ok", lambda s, timeout_s=2.0: False)
    called: list[str] = []
    real = scanner.walk_branch

    def note(tier: str, root: Path, timeout_s: float = 1.0) -> scanner.BranchWalk:
        called.append(tier)
        return real(tier, root, timeout_s)

    monkeypatch.setattr(scanner, "walk_branch", note)
    result = scanner.scan(settings, db, now=500)
    assert called == ["hot"]
    assert not result.cold_complete and result.cold_error
    with db.connection() as conn:  # the cold row survives a skipped walk
        assert q.get_file(conn, "/b.bin") is not None


def test_scan_stamps_pins_with_the_longest_prefix(
    settings: Settings, db: Database, branches: tuple[Path, Path, Path]
):
    hot, _cold, _ = branches
    touch(hot, "/pinned/deep/f.bin", 1)
    touch(hot, "/loose/f.bin", 1)
    pinned = settings.model_copy(update={"pins": [PinRule(path="/pinned", tier="hot")]})
    with db.tx() as conn:
        q.set_pin(conn, "/pinned/deep", "cold", until=None, source="ui")
    scanner.scan(pinned, db, now=500)
    with db.connection() as conn:
        deep = q.get_file(conn, "/pinned/deep/f.bin")
        loose = q.get_file(conn, "/loose/f.bin")
    assert deep is not None and deep.pinned == "cold"
    assert loose is not None and loose.pinned is None


def test_walk_branch_returns_partial_results_when_it_runs_long(
    branches: tuple[Path, Path, Path], monkeypatch
):
    hot, _cold, _ = branches
    release = threading.Event()

    def slow(root: Path):
        yield "/one.bin", 1, 1
        release.wait(5)
        yield "/two.bin", 1, 1

    monkeypatch.setattr(scanner, "_walk_branch", slow)
    monkeypatch.setattr(scanner, "_CHUNK", 1)
    walk = scanner.walk_branch("hot", hot, timeout_s=0.2)
    release.set()
    assert not walk.complete and walk.error
    assert walk.files == {"/one.bin": (1, 1)}


def test_cold_ok_is_false_for_a_missing_branch(settings: Settings, tmp_path: Path):
    assert scanner.cold_ok(settings) is True
    gone = settings.model_copy(update={"cold_root": tmp_path / "not-mounted"})
    assert scanner.cold_ok(gone) is False


def test_cold_ok_gives_up_on_a_probe_that_blocks(settings: Settings, monkeypatch):
    monkeypatch.setattr(scanner.os, "statvfs", lambda p: time.sleep(5))
    assert scanner.cold_ok(settings, timeout_s=0.1) is False


def test_branch_free_and_usage_degrade_to_zero(settings: Settings, tmp_path: Path):
    used, total = scanner.branch_usage(settings.hot_root)
    assert total > 0 and used >= 0
    assert scanner.branch_free(settings.hot_root) > 0
    assert scanner.branch_usage(tmp_path / "gone") == (0, 0)
    assert scanner.branch_free(tmp_path / "gone") == 0


def test_tier_of_prefers_the_hot_copy(settings: Settings, branches: tuple[Path, Path, Path]):
    hot, cold, _ = branches
    touch(hot, "/both.bin", 1)
    touch(cold, "/both.bin", 1)
    touch(cold, "/only-cold.bin", 1)
    assert scanner.tier_of(settings, "/both.bin") == "hot"
    assert scanner.tier_of(settings, "/only-cold.bin") == "cold"
    assert scanner.tier_of(settings, "/nowhere.bin") is None


def test_scan_never_writes_a_path_above_debug(
    settings: Settings, db: Database, branches: tuple[Path, Path, Path], caplog
):
    hot, cold, _ = branches
    touch(hot, "/secret-name.bin", 1)
    touch(cold, "/secret-name.bin", 1)  # dual, the noisiest path there is
    with caplog.at_level("INFO"):
        scanner.scan(settings, db, now=500)
    assert not any("secret-name" in r.getMessage() for r in caplog.records)


def test_walk_uses_one_lstat_per_file(branches: tuple[Path, Path, Path], monkeypatch):
    hot, _cold, _ = branches
    for i in range(3):
        touch(hot, f"/g/f{i}.bin", 1)
    calls: list[str] = []
    real_lstat = os.lstat

    def counting(path, *a, **kw):
        calls.append(str(path))
        return real_lstat(path, *a, **kw)

    monkeypatch.setattr(scanner.os, "lstat", counting)
    walk = scanner.walk_branch("hot", hot, timeout_s=5.0)
    monkeypatch.undo()
    assert walk.complete and len(walk.files) == 3
    # One lstat per file and no more: the mode bits decide, not isfile+islink.
    on_files = [c for c in calls if c.endswith(".bin")]
    assert len(on_files) == 3 and len(set(on_files)) == 3
    assert stat.S_ISREG(os.lstat(hot / "g/f0.bin").st_mode)
