"""Periodic walk of both branches, keeping the `files` table current.

Walking the branches rather than the union gives the tier for free — the branch
that holds the path is the tier — and avoids the FUSE round trip the build
doc's `getfattr -n user.mergerfs.relpath` would need per file.

The cold branch is NFS 4.1 mounted `hard` (AGENTS.md): every call on it can
block forever when the UNAS is unreachable, and no flag on our side changes
that. Three rules follow, and they are the whole reason this module is shaped
the way it is:

1. probe first — `cold_ok()` decides whether the cold walk is attempted at all,
   and the probe itself runs under a timeout;
2. walk under a timeout — each branch walk runs in a daemon thread that is
   abandoned, not waited on, when it overruns, so a hung mount can never keep
   the daemon or the interpreter alive;
3. prune nothing from a partial walk — `delete_unseen_files` is called only for
   a branch whose walk actually completed, and only for that branch's tier
   (AGENTS.md rule 3, D-007a).

A file present on both branches is a move that was interrupted. mergerfs `ff`
serves the hot copy, so that is what the row says; the count is logged and the
executor reconciles it later."""

from __future__ import annotations

import logging
import os
import sqlite3
import stat
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from ingenaning.config import Settings
from ingenaning.store import queries as q
from ingenaning.store.db import Database
from ingenaning.telemetry import keys

log = logging.getLogger(__name__)

SKIP_NAMES = frozenset({".snapshots", ".zfs", "lost+found", ".Trash", ".Trash-1000"})
WALK_TIMEOUT_S = 600.0
PROBE_TIMEOUT_S = 2.0
_CHUNK = 1000  # files published from the walk thread at a time
_LOG_SAMPLE = 20


def _with_timeout[T](work: Callable[[], T], timeout_s: float, default: T, what: str) -> T:
    """Run `work` in a daemon thread and give up on it after `timeout_s`.

    The thread is abandoned rather than cancelled — there is no way to interrupt
    a blocked NFS syscall — so it must be a daemon: a `concurrent.futures`
    worker is not, and one stuck on the cold mount would hold the interpreter
    open at shutdown."""
    box: list[T] = []

    def run() -> None:
        try:
            box.append(work())
        except OSError:
            log.debug("%s failed", what, exc_info=True)

    t = threading.Thread(target=run, daemon=True, name=what)
    t.start()
    t.join(timeout_s)
    if t.is_alive():
        log.warning("%s did not answer within %.1fs; giving up on it", what, timeout_s)
        return default
    return box[0] if box else default


# --- walking ------------------------------------------------------------------


def _on_walk_error(exc: OSError) -> None:
    log.debug("walk error: %s", exc)


def _walk_branch(root: Path) -> Iterator[tuple[str, int, int]]:
    """Yield (union-relative path, size, mtime) for the regular files under
    `root`. One lstat per file: the mode bits already say whether it is a
    regular file, so there is no second isfile/islink pass, and symlinks are
    never followed out of the branch."""
    if not root.is_dir():
        return
    root_s = str(root)
    for dirpath, dirnames, filenames in os.walk(root_s, onerror=_on_walk_error, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in SKIP_NAMES]
        for name in filenames:
            full = os.path.join(dirpath, name)
            try:
                st = os.lstat(full)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            rel = full[len(root_s) :]
            yield keys.norm(rel), st.st_size, int(st.st_mtime)


class _Collector:
    """Somewhere for a walk thread to put results that the caller can read even
    if the thread is still running.

    The worker fills a private dict and publishes it in chunks under the lock;
    the reader only ever merges chunks that are finished with. Nothing the
    reader touches is mutated afterwards, so an abandoned thread cannot make a
    snapshot change size while it is being iterated."""

    def __init__(self, chunk: int | None = None) -> None:
        self._chunk = _CHUNK if chunk is None else chunk
        self._lock = threading.Lock()
        self._chunks: list[dict[str, tuple[int, int]]] = []
        self._pending: dict[str, tuple[int, int]] = {}
        self.done = False

    def add(self, rel: str, size: int, mtime: int) -> None:
        self._pending[rel] = (size, mtime)
        if len(self._pending) >= self._chunk:
            self.publish()

    def publish(self) -> None:
        if not self._pending:
            return
        with self._lock:
            self._chunks.append(self._pending)
        self._pending = {}

    def snapshot(self) -> dict[str, tuple[int, int]]:
        with self._lock:
            chunks = list(self._chunks)
        out: dict[str, tuple[int, int]] = {}
        for c in chunks:
            out.update(c)
        return out


@dataclass(slots=True)
class BranchWalk:
    """What one branch walk saw. `complete` is the only thing that licenses a
    prune of that tier."""

    tier: str
    files: dict[str, tuple[int, int]] = field(default_factory=dict)
    complete: bool = False
    error: str | None = None
    seconds: float = 0.0


def walk_branch(tier: str, root: Path, timeout_s: float = WALK_TIMEOUT_S) -> BranchWalk:
    """Walk one branch under a timeout. On overrun the partial result comes back
    with complete=False, which stops it being used to delete anything."""
    col = _Collector()
    failures: list[str] = []

    def work() -> None:
        try:
            for rel, size, mtime in _walk_branch(root):
                col.add(rel, size, mtime)
        except OSError as exc:  # os.walk swallows most, a stat storm may not
            failures.append(str(exc))
        finally:
            col.publish()
            col.done = True

    t0 = time.monotonic()
    t = threading.Thread(target=work, daemon=True, name=f"walk-{tier}")
    t.start()
    t.join(timeout_s)
    files = col.snapshot()
    error: str | None = None
    if failures:
        error = failures[0]
        log.error("%s branch walk failed: %s", tier, error)
    elif not col.done:
        error = f"walk timed out after {timeout_s:.0f}s"
        log.error(
            "%s branch walk timed out after %.0fs (%d files seen)", tier, timeout_s, len(files)
        )
    return BranchWalk(
        tier=tier,
        files=files,
        complete=col.done and not failures,
        error=error,
        seconds=time.monotonic() - t0,
    )


# --- branch probes ------------------------------------------------------------


def cold_ok(settings: Settings, timeout_s: float = PROBE_TIMEOUT_S) -> bool:
    """Is the cold branch answering? Blocks on the cold mount by design, so it
    runs under a timeout and reports False rather than waiting.

    `is_dir` alone is not enough: the kernel can answer it from cache while the
    server is gone. A directory read forces a round trip."""
    root = settings.cold_root

    def probe() -> bool:
        if not root.is_dir():
            return False
        os.statvfs(str(root))
        next(iter(os.scandir(str(root))), None)
        return True

    return _with_timeout(probe, timeout_s, False, "cold-probe")


def branch_usage(path: Path, timeout_s: float = PROBE_TIMEOUT_S) -> tuple[int, int]:
    """(used, total) bytes of the filesystem holding `path`; (0, 0) when it is
    absent or does not answer. statvfs on the cold branch can block, so this is
    behind the same timeout as the probe."""

    def probe() -> tuple[int, int]:
        st = os.statvfs(str(path))
        total = st.f_blocks * st.f_frsize
        return total - st.f_bavail * st.f_frsize, total

    return _with_timeout(probe, timeout_s, (0, 0), "usage-probe")


def branch_free(path: Path, timeout_s: float = PROBE_TIMEOUT_S) -> int:
    """Free bytes on the filesystem holding `path`, 0 when it does not answer.
    Callers treat 0 as 'no headroom', which is the safe way to be wrong."""

    def probe() -> int:
        st = os.statvfs(str(path))
        return int(st.f_bavail * st.f_frsize)

    return _with_timeout(probe, timeout_s, 0, "free-probe")


def tier_of(settings: Settings, rel: str, timeout_s: float = PROBE_TIMEOUT_S) -> str | None:
    """The branch holding a union-relative path, or None if neither has it.

    Hot is checked first because it is local and cannot block, and because
    mergerfs `ff` serves the hot copy when both branches have the file — so a
    hot hit is the answer, not a tie. Only the cold check can block, and it is
    behind a timeout."""
    tail = keys.norm(rel).lstrip("/")
    if (settings.hot_root / tail).is_file():
        return "hot"
    cold = settings.cold_root / tail
    return "cold" if _with_timeout(lambda: cold.is_file(), timeout_s, False, "tier-probe") else None


# --- the scan -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScanResult:
    """Counts only. PRIVACY.md rule 11: what is logged above DEBUG says how
    many, never which."""

    hot: int
    cold: int
    dual: int
    upserted: int
    removed: int
    hot_complete: bool
    cold_complete: bool
    hot_error: str | None
    cold_error: str | None
    seconds: float


def _pin_lookup(
    settings: Settings, runtime: Iterable[sqlite3.Row], now: int
) -> Callable[[str], tuple[str | None, int | None]]:
    """Resolve the pin that decides a path: longest matching prefix wins, and
    policy.yaml wins a tie against a runtime pin of the same length (D-008,
    yaml > ui > intent)."""
    rows = [(str(r["path"]), str(r["tier"]), r["until"]) for r in runtime]

    def pin_for(rel: str) -> tuple[str | None, int | None]:
        best: tuple[str, int | None, int] | None = None
        for path, tier, until in rows:
            covers = rel == path or rel.startswith(path.rstrip("/") + "/")
            if covers and (best is None or len(path) > best[2]):
                best = (tier, until, len(path))
        from_yaml = settings.pin_for(rel, now)
        if from_yaml is not None and (best is None or len(from_yaml.path) >= best[2]):
            return from_yaml.tier, from_yaml.until
        return (best[0], best[1]) if best is not None else (None, None)

    return pin_for


def scan(
    settings: Settings,
    db: Database,
    now: int | None = None,
    timeout_s: float = WALK_TIMEOUT_S,
) -> ScanResult:
    """One pass over both branches. Safe to call on a timer: it degrades to a
    hot-only, prune-nothing scan when the cold branch is unreachable."""
    stamp = int(time.time()) if now is None else now
    t0 = time.monotonic()

    hot = walk_branch("hot", settings.hot_root, timeout_s)
    if cold_ok(settings):
        cold = walk_branch("cold", settings.cold_root, timeout_s)
    else:
        cold = BranchWalk(tier="cold", error="cold branch did not answer; walk skipped")
        log.warning("the cold branch did not answer; skipping its walk and pruning nothing there")

    dual = [rel for rel in cold.files if rel in hot.files]
    for rel in dual[:_LOG_SAMPLE]:
        log.debug("on both branches (interrupted move), counted as hot: %s", rel)
    if dual:
        log.warning("%d files are on both branches (interrupted moves); counted as hot", len(dual))
    for rel in dual:
        del cold.files[rel]

    ordinals = keys.assign_ordinals([*hot.files, *cold.files])
    with db.connection() as conn:
        pin_for = _pin_lookup(settings, q.list_pins(conn, stamp), stamp)

    def rows() -> Iterator[dict[str, object]]:
        for walk in (hot, cold):
            for rel, (size, mtime) in walk.files.items():
                pinned, until = pin_for(rel)
                yield {
                    "path": rel,
                    "size": size,
                    "tier": walk.tier,
                    "mtime": mtime,
                    "group_key": keys.group_key(rel),
                    "ordinal": ordinals.get(rel),
                    "pinned": pinned,
                    "pinned_until": until,
                }

    with db.tx() as conn:
        upserted = q.upsert_files(conn, rows(), seen=stamp)
        removed = 0
        for walk in (hot, cold):
            # AGENTS.md rule 3: a walk that did not complete says nothing about
            # what is missing, and a walk of one branch says nothing about the
            # other — hence the mandatory tier.
            if walk.complete:
                removed += q.delete_unseen_files(conn, walk.tier, stamp)

    result = ScanResult(
        hot=len(hot.files),
        cold=len(cold.files),
        dual=len(dual),
        upserted=upserted,
        removed=removed,
        hot_complete=hot.complete,
        cold_complete=cold.complete,
        hot_error=hot.error,
        cold_error=cold.error,
        seconds=round(time.monotonic() - t0, 2),
    )
    log.info(
        "scan: hot=%d cold=%d dual=%d upserted=%d removed=%d in %.2fs "
        "(hot_complete=%s cold_complete=%s)",
        result.hot,
        result.cold,
        result.dual,
        result.upserted,
        result.removed,
        result.seconds,
        result.hot_complete,
        result.cold_complete,
    )
    return result
