"""Container side of access telemetry.

Reads the JSON lines that `telemetry/relay.py` produces on the Proxmox host
(D-001) from the unix socket bind-mounted into the container at
`settings.socket_path`, coalesces repeats, and batch-inserts into `access`.
Nothing here spawns fatrace: the container cannot use fanotify at all.

What the stream is allowed to do to us, and what we do about it:

* arrive out of order across branches — rows are keyed by (path, client, op)
  and carry the newest stamp seen; nothing assumes a monotonic clock;
* carry `{"type": "dropped"}` markers when the relay's buffer overflowed while
  we were down — counted, never fatal;
* carry rubbish — counted at DEBUG and skipped;
* stop — the reader reconnects with backoff and keeps the batcher alive;
* go quiet — the select timeout still fires, so a buffered open lands within a
  second or two and `skip_if_opened_within` sees it.

Coalescing: fanotify fires per syscall, so a burst of opens on one file from
one client collapses to a single row inside `coalesce_s`."""

from __future__ import annotations

import json
import logging
import select
import socket
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from ingenaning.config import Settings
from ingenaning.store import queries as q
from ingenaning.store.db import Database

log = logging.getLogger(__name__)

MAX_ROWS = 1000
MAX_WAIT_S = 5.0
COALESCE_S = 5.0
POLL_S = 1.0
RECV_BYTES = 65536
_OPEN_CAP = 50_000


@dataclass(slots=True)
class AccessEvent:
    """One row on its way to `access`."""

    ts: int
    client: str
    op: str  # open | write
    path: str  # union-relative
    bytes: int = 0
    tier: str | None = None  # branch the relay saw it on, None when unknown


@dataclass(slots=True)
class LineCounts:
    """What a stretch of stream contained. Counts only — PRIVACY.md rule 11
    keeps paths out of anything logged above DEBUG."""

    events: int = 0
    dropped: int = 0
    malformed: int = 0


def parse_json_lines(
    lines: Iterable[str | bytes], counts: LineCounts | None = None
) -> Iterator[AccessEvent]:
    """Relay lines -> events. Dropped markers and unparseable lines are counted
    and skipped; a bad line never ends the stream."""
    counts = counts if counts is not None else LineCounts()
    for raw in lines:
        text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        text = text.strip()
        if not text:
            continue
        try:
            obj = json.loads(text)
            if not isinstance(obj, dict):
                raise TypeError("line is not an object")
            if obj.get("type") == "dropped":
                counts.dropped += int(obj.get("count") or 0)
                continue
            event = AccessEvent(
                ts=int(obj["ts"]),
                client=str(obj.get("client") or "unknown"),
                op=str(obj["op"]),
                path=q.norm_path(str(obj["path"])),
                bytes=int(obj.get("bytes") or 0),
                tier=obj.get("tier"),
            )
        except (KeyError, TypeError, ValueError):
            counts.malformed += 1
            log.debug("unusable relay line: %r", text[:200])
            continue
        counts.events += 1
        yield event


class Batcher:
    """Coalesce, then insert in batches. Thread-safe: the reader thread adds and
    the same thread ticks, but `flush` may also be called from a timer."""

    def __init__(
        self,
        db: Database,
        max_rows: int = MAX_ROWS,
        max_wait_s: float = MAX_WAIT_S,
        coalesce_s: float = COALESCE_S,
        now_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self.db = db
        self.max_rows = max_rows
        self.max_wait_s = max_wait_s
        self.coalesce_s = coalesce_s
        self._now = now_fn
        self._buf: list[AccessEvent] = []
        # coalescing window: key -> (when it opened, the row it opened)
        self._open: dict[tuple[str, str, str], tuple[float, AccessEvent]] = {}
        self._last = self._now()
        self._lock = threading.Lock()
        self.flushed = 0
        self.coalesced = 0
        self.failed = 0

    def add(self, event: AccessEvent) -> None:
        """Take one event. Inside the coalescing window it folds into the row
        that opened the window; outside it, it starts a new row."""
        key = (event.path, event.client, event.op)
        with self._lock:
            mono = self._now()
            held = self._open.get(key)
            if held is not None and mono - held[0] < self.coalesce_s:
                self.coalesced += 1
                # Lines can arrive out of order across branches: keep the newest
                # stamp, not the last one to turn up. If the row it folds into
                # has already been written, the fold is simply the row we did
                # not insert.
                held[1].ts = max(held[1].ts, event.ts)
                held[1].tier = event.tier or held[1].tier
                return
            self._open[key] = (mono, event)
            self._buf.append(event)
            due = len(self._buf) >= self.max_rows or mono - self._last >= self.max_wait_s
        if due:
            self.flush()

    def tick(self) -> None:
        """Timer entry: flush anything that has waited longer than max_wait_s,
        so an idle stream still lands. Also trims the coalescing window."""
        with self._lock:
            mono = self._now()
            due = bool(self._buf) and mono - self._last >= self.max_wait_s
            if len(self._open) > _OPEN_CAP:
                cutoff = mono - self.coalesce_s
                self._open = {k: v for k, v in self._open.items() if v[0] >= cutoff}
        if due:
            self.flush()

    def flush(self) -> int:
        """Write what is buffered. A store that will not take the rows is logged
        and the batch is dropped: telemetry is a signal source, and the daemon
        does not stop because one is unavailable."""
        with self._lock:
            self._last = self._now()
            if not self._buf:
                return 0
            events = self._buf
            self._buf = []
        rows = [(e.ts, e.client, e.op, e.path, e.bytes, e.tier) for e in events]
        try:
            with self.db.tx() as conn:
                q.insert_access(conn, rows)
        except (sqlite3.Error, OSError):
            self.failed += 1
            log.warning("dropped a batch of %d access rows", len(rows), exc_info=True)
            return 0
        self.flushed += len(rows)
        return len(rows)


class SocketReader:
    """Consume the relay socket. Reconnects with backoff so a host reboot, a
    container failover, or a relay restart is a pause and not an outage."""

    def __init__(
        self,
        sock_path: Path | str,
        batcher: Batcher,
        stop: threading.Event | None = None,
        poll_s: float = POLL_S,
        backoff_s: float = 1.0,
        max_backoff_s: float = 30.0,
    ) -> None:
        self.sock_path = str(sock_path)
        self.batcher = batcher
        self.stop = stop or threading.Event()
        self.poll_s = poll_s
        self.backoff_s = backoff_s
        self.max_backoff_s = max_backoff_s
        self.counts = LineCounts()
        self.connects = 0
        self.failures = 0

    def _connect(self) -> socket.socket | None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.settimeout(self.poll_s)  # a connect to a wedged peer must not hang
            s.connect(self.sock_path)
            s.setblocking(False)
        except OSError as exc:
            s.close()
            self.failures += 1
            log.warning("relay socket unavailable: %s", exc)
            return None
        return s

    def run(self) -> None:
        backoff = self.backoff_s
        while not self.stop.is_set():
            s = self._connect()
            if s is None:
                self.stop.wait(backoff)
                backoff = min(backoff * 2, self.max_backoff_s)
                continue
            backoff = self.backoff_s
            self.connects += 1
            log.info("connected to the relay")
            try:
                self._read_loop(s)
            finally:
                s.close()
        self.batcher.flush()

    def _read_loop(self, s: socket.socket) -> None:
        pending = b""
        while not self.stop.is_set():
            ready, _, _ = select.select([s], [], [], self.poll_s)
            if not ready:
                self.batcher.tick()  # idle: flush what is waiting anyway
                continue
            try:
                chunk = s.recv(RECV_BYTES)
            except OSError:
                log.warning("relay socket read failed", exc_info=True)
                break
            if not chunk:
                log.warning("the relay closed the socket")
                break
            pending += chunk
            *lines, pending = pending.split(b"\n")
            before = self.counts.dropped
            for event in parse_json_lines(lines, self.counts):
                self.batcher.add(event)
            if self.counts.dropped > before:
                log.warning(
                    "the relay dropped %d lines while nothing was consuming",
                    self.counts.dropped - before,
                )
            self.batcher.tick()


def run_socket(
    settings: Settings,
    db: Database,
    stop: threading.Event | None = None,
    sock_path: Path | str | None = None,
) -> SocketReader:
    """Run the consumer until `stop` is set. Returns the reader so a caller can
    read its counters."""
    reader = SocketReader(sock_path or settings.socket_path, Batcher(db), stop)
    reader.run()
    return reader
