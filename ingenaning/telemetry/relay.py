#!/usr/bin/env python3
"""aning-relay — host-side fatrace relay and audit-chain verifier.

STDLIB ONLY. This file is copied to each Proxmox node as
/usr/local/bin/aning-relay and runs there as root with the system python,
outside the package and outside the container. It must never import
`ingenaning` (tests/test_contract.py enforces that), and it is the one file in
the tree allowed to write to stderr.

Why it exists (D-001): fanotify needs CAP_SYS_ADMIN in the host's mount
namespace, so fatrace cannot run inside the unprivileged `nas` container. The
relay runs one fatrace per branch on the host, rewrites branch paths to
union-relative paths, attributes the client, and serves JSON lines on a unix
socket whose directory is bind-mounted into CT 200:

    {"ts": 1757355000, "client": "10.20.0.31", "op": "open",
     "path": "/g/x.bin", "tier": "cold", "bytes": 0}
    {"type": "dropped", "count": 12}

The relay is the server, not the client, so it can start at host boot and
buffer while the container is down or failing over. The buffer is bounded; it
drops the oldest lines and tells the next consumer how many, which is the
`{"type": "dropped"}` marker the container counts.

It also verifies the audit chain (`verify_chain`), because the anchor timer
runs on the host where the log and the anchors live, not in the container whose
database is the thing being checked.

Usage:
    aning-relay --hot /tank/hot --cold /mnt/cold \\
                --socket /run/ingenaning/access.sock --ctid 200
    aning-relay verify --log /var/log/ingenaning/audit.jsonl
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

log = logging.getLogger("aning-relay")

# fatrace takes NO path argument. From fatrace(8):
#   -c, --current-mount   Only record events on the mount point of the current
#                         directory.
#   -t, --timestamp       Add a timestamp to the output. Give twice for seconds
#                         since the epoch.
#   -f, --filter=TYPES    Show only the given event types: C(lose), W(rite),
#                         R(ead), O(pen), D(elete), +(create), <(move from),
#                         >(move to).
# TODO(verify): fatrace is not installed in the build sandbox, so `fatrace
# --help` could not be run and this invocation is taken from the man page, not
# from memory: https://manpages.debian.org/unstable/fatrace/fatrace.8.en.html
#
# So one fatrace per branch, with the branch root as the subprocess cwd. -tt
# gives epoch seconds with microseconds, which has no timezone and no midnight
# rollover to get wrong. R is deliberately absent: FAN_ACCESS fires per read
# syscall, so one large sequential read is thousands of lines a second, while
# the open is the first-touch signal we actually score (D-005).
FATRACE_FILTER = "CWO"
FATRACE_CMD: tuple[str, ...] = ("fatrace", "-c", "-tt", "-f", FATRACE_FILTER)

# `<epoch>.<micros> comm(pid): OPS /absolute/path`. The timestamp is optional
# because a line without one still carries an event; it falls back to the
# receiver's clock. Only one timestamp format can ever arrive: the relay
# controls its own invocation.
LINE_RE = re.compile(
    r"^(?:(?P<ts>\d+(?:\.\d+)?)\s+)?"
    r"(?P<comm>[^()\s]+)\((?P<pid>\d+)\):\s+"
    r"(?P<ops>[A-Z+<>]+)\s+(?P<path>/.*)$"
)

GENESIS = "0" * 64  # `prev` of the first audit line

_SUBPROCESS_TIMEOUT_S = 5.0
_SHUTDOWN_TIMEOUT_S = 5.0


def classify_ops(ops: str) -> str | None:
    """'O' -> open, 'W' or 'CW' -> write, bare 'C' -> None. A close that wrote
    nothing says only that a descriptor went away; the open already told us."""
    if "W" in ops:
        return "write"
    if "O" in ops:
        return "open"
    return None


def parse_fatrace_line(raw: str, branch: str, now: float) -> tuple[int, str, int, str, str] | None:
    """-> (ts, comm, pid, op, union-relative path) for a line under `branch`.

    None for a line that does not parse, names another mount, describes a
    deleted file, or is a close without a write. -c watches the whole mount of
    the cwd, so paths outside the branch root do arrive and are dropped here."""
    m = LINE_RE.match(raw.rstrip("\n"))
    if not m:
        return None
    path = m.group("path")
    if path.endswith(" (deleted)"):
        return None
    root = branch.rstrip("/")
    if not path.startswith(root + "/"):
        return None
    op = classify_ops(m.group("ops"))
    if op is None:
        return None
    stamp = m.group("ts")
    ts = int(float(stamp)) if stamp else int(now)
    return ts, m.group("comm"), int(m.group("pid")), op, path[len(root) :]


class ClientResolver:
    """host pid -> client label, refreshed at most every `ttl_s`.

    nfsd is the kernel server, so /proc/fs/nfsd/clients exists on the host; with
    exactly one client connected it can be named, otherwise the label is 'nfs'.
    smbd runs in the container: the host pid maps to the container pid through
    /proc/<pid>/status NSpid, and `pct exec <ctid> -- smbstatus -j` maps that to
    a remote machine.

    Anything else is 'local'. PRIVACY.md principle 2 keeps process names out of
    `access`; the comm is logged at DEBUG and goes no further."""

    def __init__(
        self,
        ctid: int | None,
        ttl_s: float = 60.0,
        runner: Callable[[list[str]], str] | None = None,
    ) -> None:
        self.ctid = ctid
        self.ttl_s = ttl_s
        self._runner = runner or self._run
        self._smb: dict[int, str] = {}  # container pid -> client
        self._nfs: list[str] = []
        self._at = 0.0

    @staticmethod
    def _run(cmd: list[str]) -> str:
        """Every subprocess here has a timeout: `pct exec` reaches into another
        namespace and can hang if the container is wedged."""
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT_S, check=False
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return proc.stdout

    def _refresh_smb(self) -> None:
        self._smb = {}
        cmd = ["smbstatus", "-j"]
        if self.ctid is not None:
            cmd = ["pct", "exec", str(self.ctid), "--", *cmd]
        try:
            data = json.loads(self._runner(cmd) or "{}")
            sessions = data.get("sessions") or {}
            for sess in sessions.values():
                pid = int(sess.get("pid") or 0)
                host = sess.get("remote_machine") or sess.get("hostname") or "smb"
                if pid:
                    self._smb[pid] = str(host)
        except (AttributeError, TypeError, ValueError):
            log.debug("smbstatus gave nothing usable", exc_info=True)

    def _refresh_nfs(self) -> None:
        self._nfs = []
        clients = Path("/proc/fs/nfsd/clients")
        if not clients.is_dir():
            return
        for info in clients.glob("*/info"):
            try:
                for line in info.read_text().splitlines():
                    if line.startswith("address:"):
                        addr = line.split(":", 1)[1].strip().strip('"')
                        self._nfs.append(addr.rsplit(":", 1)[0].strip("[]"))
            except OSError:
                continue

    def _refresh(self) -> None:
        self._at = time.monotonic()
        self._refresh_smb()
        self._refresh_nfs()

    @staticmethod
    def container_pid(host_pid: int) -> int | None:
        """Last entry of NSpid is the pid inside the innermost namespace."""
        try:
            for line in Path(f"/proc/{host_pid}/status").read_text().splitlines():
                if line.startswith("NSpid:"):
                    ids = line.split()[1:]
                    return int(ids[-1]) if ids else None
        except (OSError, ValueError):
            return None
        return None

    def resolve(self, comm: str, pid: int) -> str:
        if time.monotonic() - self._at > self.ttl_s:
            self._refresh()
        if comm.startswith("smbd"):
            cpid = self.container_pid(pid) if self.ctid is not None else pid
            return self._smb.get(cpid or -1, "smb")
        if comm.startswith("nfsd"):
            return self._nfs[0] if len(self._nfs) == 1 else "nfs"
        log.debug("unattributed access from %s(%d): reported as 'local'", comm, pid)
        return "local"


class Relay:
    """One fatrace per branch, one unix socket, one bounded buffer."""

    def __init__(
        self,
        branches: Mapping[str, str],
        sock_path: str,
        resolver: ClientResolver,
        buffer_lines: int = 100_000,
    ) -> None:
        self.branches = dict(branches)  # tier -> absolute branch root
        self.sock_path = sock_path
        self.resolver = resolver
        self.buf: collections.deque[bytes] = collections.deque(maxlen=buffer_lines)
        self.clients: list[socket.socket] = []
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.procs: list[subprocess.Popen[str]] = []
        self.dropped = 0
        self.emitted = 0
        # Injectable so the tests can assert the invocation without fatrace.
        self.popen: Callable[..., subprocess.Popen[str]] = subprocess.Popen

    # --- fatrace ------------------------------------------------------------

    def _spawn(self, root: str) -> subprocess.Popen[str]:
        """fatrace with the branch as cwd and no path argument (see FATRACE_CMD).
        stderr is left on the relay's own stderr so a failing fatrace is visible
        in journald."""
        return self.popen(list(FATRACE_CMD), cwd=root, stdout=subprocess.PIPE, text=True, bufsize=1)

    def _pump(self, tier: str, root: str) -> None:
        """Read one branch until told to stop, restarting fatrace if it exits.
        A branch that never starts is logged and retried: the daemon does not
        exit because a signal source is unavailable."""
        while not self.stop.is_set():
            try:
                proc = self._spawn(root)
            except OSError as exc:
                log.error("fatrace for the %s branch did not start: %s", tier, exc)
                self.stop.wait(5)
                continue
            self.procs.append(proc)
            if proc.stdout is not None:
                for raw in proc.stdout:
                    if self.stop.is_set():
                        break
                    self._handle(tier, root, raw)
            rc = self._reap(proc)
            if not self.stop.is_set():
                log.warning("fatrace for the %s branch exited rc=%s; restarting", tier, rc)
                self.stop.wait(2)

    def _reap(self, proc: subprocess.Popen[str]) -> int | None:
        """Never wait forever on a child: terminate, then kill."""
        try:
            return proc.wait(timeout=_SHUTDOWN_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.terminate()
        try:
            return proc.wait(timeout=_SHUTDOWN_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
        try:
            return proc.wait(timeout=_SHUTDOWN_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            return None

    def _handle(self, tier: str, root: str, raw: str) -> None:
        parsed = parse_fatrace_line(raw, root, time.time())
        if parsed is None:
            return
        ts, comm, pid, op, rel = parsed
        event = {
            "ts": ts,
            "client": self.resolver.resolve(comm, pid),
            "op": op,
            "path": rel,
            "tier": tier,
            "bytes": 0,
        }
        self.emit(json.dumps(event, separators=(",", ":")).encode() + b"\n")

    # --- fan-out ------------------------------------------------------------

    def emit(self, line: bytes) -> None:
        """Send to every live consumer, or buffer when there is none. A full
        buffer drops its oldest line and counts it; the count reaches the next
        consumer as a `{"type": "dropped"}` marker."""
        with self.lock:
            dead = []
            for c in self.clients:
                try:
                    c.sendall(line)
                except OSError:
                    dead.append(c)
            for c in dead:
                self.clients.remove(c)
                c.close()
            if self.clients:
                self.emitted += 1
                return
            if len(self.buf) == self.buf.maxlen:
                self.dropped += 1
            self.buf.append(line)

    def _greet(self, conn: socket.socket) -> None:
        """Replay the buffer to a consumer that just connected, after telling it
        how many lines it will never see. Called with the lock held.

        The payload is built first and the state cleared only once it is away,
        so a consumer that dies mid-replay costs us nothing: the next one gets
        the same buffer and the same drop count."""
        payload = b""
        if self.dropped:
            marker = {"type": "dropped", "count": self.dropped}
            payload = json.dumps(marker, separators=(",", ":")).encode() + b"\n"
        payload += b"".join(self.buf)
        if payload:
            conn.sendall(payload)
        self.dropped = 0
        self.buf.clear()

    def _serve(self) -> None:
        p = Path(self.sock_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists():
            p.unlink()
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            srv.bind(self.sock_path)
            # The container's daemon user is not this one; the bind-mounted
            # directory is the boundary (PRIVACY.md principle 9).
            os.chmod(self.sock_path, 0o666)
            srv.listen(4)
            srv.settimeout(1.0)
            while not self.stop.is_set():
                try:
                    conn, _ = srv.accept()
                except TimeoutError:
                    continue
                except OSError:
                    break
                log.info("consumer connected; replaying %d buffered lines", len(self.buf))
                with self.lock:
                    try:
                        self._greet(conn)
                        self.clients.append(conn)
                    except OSError:
                        conn.close()
        finally:
            srv.close()
            with self.lock:
                for c in self.clients:
                    c.close()
                self.clients.clear()

    def run(self) -> None:
        threads = [threading.Thread(target=self._serve, daemon=True, name="serve")]
        threads += [
            threading.Thread(target=self._pump, args=(tier, root), daemon=True, name=f"pump-{tier}")
            for tier, root in self.branches.items()
        ]
        for t in threads:
            t.start()
        while not self.stop.is_set():
            self.stop.wait(1)
        for proc in self.procs:
            if proc.poll() is None:
                proc.terminate()
        log.info("relay stopping: %d lines out, %d dropped", self.emitted, self.dropped)


# --- audit chain --------------------------------------------------------------


def line_hash(entry: Mapping[str, Any]) -> str:
    """sha256 of the line without its `hash` field, serialised with sorted keys
    and compact separators. Both sides must agree byte for byte, so the rule is
    written once, here, and the writer uses it too."""
    body = {k: v for k, v in entry.items() if k != "hash"}
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def verify_chain(
    lines: Iterable[Mapping[str, Any] | str | bytes], prev: str = GENESIS
) -> tuple[bool, int | None]:
    """Verify a hash-chained audit log. -> (ok, first bad seq).

    Each line carries seq, ts, prev, event, actor, data and hash. Three things
    break a chain and all three are detected: an edited line (its hash no longer
    matches its body), a relinked line (its `prev` is not the previous hash),
    and a missing line (a gap in `seq`). A gap is reported as the seq that is
    *missing*, not as the line that follows it — that is the record someone
    would have to produce.

    `lines` may be dicts or raw JSON text, so a caller can hand this the log
    file straight off disk. A line that is not JSON at all fails at the seq the
    chain was waiting for."""
    expected: int | None = None
    for raw in lines:
        entry: Mapping[str, Any]
        if isinstance(raw, str | bytes):
            try:
                loaded = json.loads(raw)
            except ValueError:
                return False, expected
            if not isinstance(loaded, dict):
                return False, expected
            entry = loaded
        else:
            entry = raw
        try:
            seq = int(entry["seq"])
        except (KeyError, TypeError, ValueError):
            return False, expected
        if expected is not None and seq != expected:
            return False, expected
        if entry.get("prev") != prev:
            return False, seq
        if entry.get("hash") != line_hash(entry):
            return False, seq
        prev = str(entry["hash"])
        expected = seq + 1
    return True, None


def verify_log(path: Path) -> tuple[bool, int | None]:
    """Verify an audit log on disk. A log that cannot be read is not a verified
    log: it fails, loudly, rather than passing by default."""
    try:
        with path.open(encoding="utf-8") as fh:
            return verify_chain(line for line in fh if line.strip())
    except OSError as exc:
        log.error("audit log unreadable: %s", exc)
        return False, None


# --- entry point --------------------------------------------------------------


def _relay_main(a: argparse.Namespace) -> int:
    relay = Relay({"hot": a.hot, "cold": a.cold}, a.socket, ClientResolver(a.ctid), a.buffer)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: relay.stop.set())
    relay.run()
    return 0


def _verify_main(a: argparse.Namespace) -> int:
    ok, at = verify_log(Path(a.log))
    if ok:
        log.info("audit chain verified")
        return 0
    log.error("audit chain broken at seq %s", at)
    return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="aning-relay", description="fatrace relay for ingenaning")
    ap.add_argument("--hot", default="/tank/hot")
    ap.add_argument("--cold", default="/mnt/cold")
    ap.add_argument("--socket", default="/run/ingenaning/access.sock")
    ap.add_argument("--ctid", type=int, default=None, help="LXC id running smbd (for smbstatus)")
    ap.add_argument("--buffer", type=int, default=100_000, help="lines held while nothing consumes")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    verify = sub.add_parser("verify", help="verify a hash-chained audit log")
    verify.add_argument("--log", default="/var/log/ingenaning/audit.jsonl")
    verify.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if a.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    return _verify_main(a) if a.cmd == "verify" else _relay_main(a)


if __name__ == "__main__":
    sys.exit(main())
