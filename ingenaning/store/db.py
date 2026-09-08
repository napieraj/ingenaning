"""SQLite access for the store: one file, WAL, a connection per thread, versioned
migrations. Rows come back as `sqlite3.Row` (column access by name). No ORM; every
query lives in store/queries.py.

The database file is on the container rootfs, never on a branch, so nothing here
can block on the cold mount. The only wait is SQLite's own busy timeout."""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")
_BUSY_TIMEOUT_S = 30.0
# The migration runner's own table. Created before any migration runs.
_BOOTSTRAP = "CREATE TABLE IF NOT EXISTS migrations (version INTEGER PRIMARY KEY, applied INTEGER);"


@dataclass(frozen=True, slots=True)
class Migration:
    """One schema step. `sql` runs inside a single transaction together with the
    row that records it in `migrations`, so a failed step leaves no trace."""

    version: int
    sql: str


MIGRATIONS: tuple[Migration, ...] = (Migration(1, _SCHEMA_PATH.read_text()),)


def connect(path: Path | str) -> sqlite3.Connection:
    """Open `path`, creating parent directories. Autocommit mode (transactions are
    explicit `BEGIN`s in `Database.tx`), WAL journal, `synchronous=NORMAL`, and a
    busy timeout so a second process waits instead of failing at once."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(p), timeout=_BUSY_TIMEOUT_S, isolation_level=None, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(f"PRAGMA busy_timeout={int(_BUSY_TIMEOUT_S * 1000)}")
    conn.execute("PRAGMA temp_store=MEMORY")
    return conn


class Database:
    """One SQLite file shared by every thread of the daemon.

    Each thread gets its own connection (`connection()`); writes go through `tx()`,
    which serialises in-process writers with a lock so they queue on the lock
    instead of spinning on SQLITE_BUSY against each other."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._local = threading.local()
        self._write_lock = threading.RLock()
        self._registry_lock = threading.Lock()
        self._conns: list[sqlite3.Connection] = []
        self._generation = 0  # bumped by close(); stale thread-local connections reopen

    def _conn(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None or getattr(self._local, "generation", -1) != self._generation:
            conn = connect(self.path)
            self._local.conn = conn
            self._local.generation = self._generation
            with self._registry_lock:
                self._conns.append(conn)
        return conn

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """This thread's connection. Fine for reads and for single autocommit
        statements; use `tx()` for anything that must land together."""
        yield self._conn()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """A write transaction: `BEGIN IMMEDIATE`, then `COMMIT`, or `ROLLBACK` if
        the body raises. Serialised across threads of this process."""
        with self._write_lock:
            conn = self._conn()
            if conn.in_transaction:
                raise RuntimeError("tx() called inside an open transaction")
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                # SQLite rolls the transaction back itself on SQLITE_FULL, IOERR,
                # NOMEM, BUSY and INTERRUPT. An unconditional ROLLBACK then raises
                # "cannot rollback - no transaction is active" and that replaces the
                # disk-full or busy error the caller has to see. The cleanup must
                # never shadow the original failure.
                try:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                except sqlite3.Error:
                    log.warning("ROLLBACK after a failed transaction failed", exc_info=True)
                raise
            conn.execute("COMMIT")

    def migrate(self) -> int:
        """Apply every migration not yet recorded in `migrations`. Idempotent;
        safe to call at every start. Returns SCHEMA_VERSION."""
        with self._write_lock:
            conn = self._conn()
            conn.executescript(_BOOTSTRAP)
            applied = {int(r["version"]) for r in conn.execute("SELECT version FROM migrations")}
            for m in MIGRATIONS:
                if m.version in applied:
                    continue
                # One script so the DDL and its version row commit or roll back together.
                # Values are ints, not user input, so they can be formatted into the SQL.
                script = (
                    "BEGIN IMMEDIATE;\n"
                    f"{m.sql}\n"
                    "INSERT OR IGNORE INTO migrations(version, applied) "
                    f"VALUES ({m.version}, {int(time.time())});\n"
                    "COMMIT;"
                )
                try:
                    conn.executescript(script)
                except sqlite3.Error:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise
                log.info("applied schema migration %d to %s", m.version, self.path)
        return SCHEMA_VERSION

    def version(self) -> int:
        """Highest migration recorded; 0 for a database that was never migrated."""
        try:
            row = self._conn().execute("SELECT MAX(version) AS v FROM migrations").fetchone()
        except sqlite3.OperationalError:  # no migrations table yet
            return 0
        return int(row["v"] or 0)

    def close(self) -> None:
        """Close every connection this Database opened, on any thread. A thread that
        uses the Database again afterwards transparently gets a new connection.

        Takes the write lock, so a writer inside `tx()` finishes and commits first:
        closing its connection under it would abort that transaction with a
        ProgrammingError and discard writes that were already made."""
        with self._write_lock:
            with self._registry_lock:
                conns, self._conns = self._conns, []
                self._generation += 1
            for conn in conns:
                conn.close()
