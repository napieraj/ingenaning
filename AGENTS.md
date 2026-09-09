# AGENTS.md — ingenaning

Applies to any agent or person working in this repository.

## Environment facts — verify, never assume
- `nas` (CT 200) is an UNPRIVILEGED LXC on Proxmox (pve1, HA to pve2).
  It cannot: mount NFS, run kernel nfsd, use fanotify on host mounts, load
  kernel modules, or see host pids. Anything needing those runs on the host
  as a small stdlib-only script (see ingenaning/telemetry/relay.py) or in
  userspace inside the container.
- Hot branch: /tank/hot — ZFS mirror on the node, local, fast.
- Cold branch: /mnt/cold — NFS 4.1 from the UNAS, mounted `hard` on the host.
  Any I/O on it may block indefinitely when the UNAS is unreachable.
- Both branches are bind-mounted into the container at the same paths.
  Union: mergerfs inside the container at /srv/nas, policy `ff`
  (create on hot), `moveonenospc`.
- SMB is served from the container (samba). NFS: see DECISIONS.md D-002.
- Planners: Ollama at http://mac.oskar.co:11434. Frequently unavailable.
- Telemetry arrives on a unix socket at /run/ingenaning/access.sock,
  bind-mounted as a directory from the host. Lines are JSON, may be
  out of order across branches, and may include {"type":"dropped"} markers.
- `fatrace` has no path argument. It watches the mount of its cwd with -c.

## Rules
1. External tools: read --help or the man page before writing an
   invocation; quote it in a comment next to the call.
2. Mount touches: state in a comment whether the call can block on the
   cold branch. If it can, run it in a thread with a timeout.
3. Scans that do not complete are read-only. Never delete or bulk-update
   rows from a partial walk.
4. The core's domain is files, paths, sizes, times, order, and access.
   No media, project, or file-type semantics in core code. Semantics
   belong only in planner prompts under ingenaning/prompts/.
5. arms/, candidates, and prompt assembly must not read expectations.
   Planners see met expectations only through their feedback block,
   after met_at.
6. Spec vs fact: the fact wins. Record the conflict in docs/DECISIONS.md
   and ask.
7. Every module has tests that run offline against fixtures.
8. One module per change. No drive-by refactors.
9. Pins are absolute. No code path moves a pinned file against its pin.
10. Models propose promotion only. No code path lets a planner demote,
    delete, or pin.

## Privacy and security
PRIVACY.md and SECURITY.md are rules, not policy prose. Read both before
touching store/, arms/, api/, telemetry/, or prompts/. tests/test_privacy.py
enforces what can be enforced. Rule 12: any change that adds a network
call, a new stored field, a new prompt, or a new log line at INFO or above
cites the PRIVACY.md principle it complies with in the PR description.

## Layout
See docs/ingenaning-software-build.md §1. Documents in docs/ are the spec;
docs/DECISIONS.md overrides them where they conflict.

## Prior art — reuse before writing
The union and the mover are solved problems. Do not reimplement what these
already do; read them, vendor or depend where licence and shape allow, and
record the choice in docs/DECISIONS.md.

| Concern | Use | Notes |
|---|---|---|
| Union filesystem | mergerfs (trapexit) | Already the design. Follow the documented two-pool tiered-cache pattern: https://trapexit.github.io/mergerfs/usage_patterns/ |
| Threshold mover mechanics | mergerfs-cache-mover (MonsterMuffin), `mergerfs.percent-full-mover` (trapexit tools) | Lift: single-instance lock, threshold/target hysteresis, oldest-first fallback, empty-dir cleanup, rsync flags. Our executor adds arm-driven selection on top; it does not replace the move primitive. |
| Model-directory tiering | Speedloader (Skylark-Software) | If `/models` becomes the dominant hot-tier workload, evaluate running it for that directory instead of our own placement. |
| Reference design | 45Drives autotier | FUSE tiering by frequency/age/fullness. Read its conflict handling (`.autotier_conflict.<tier>`) and quota semantics before writing ours. Do not adopt: it replaces mergerfs and appears unmaintained. |

What is ours and only ours: candidate generation, learned scoring, planner
arms, the bandit, outcomes, expectations, signals. Effort goes there.

Rule 11. Before writing any module under executor/ or telemetry/, check
this table and the linked sources. A PR that reimplements a listed concern
without a DECISIONS.md entry explaining why is rejected.

Rule 13. Every state-changing action in executor/, api/, and arms/intent.py
calls `audit.emit(event, data)` before the change is applied. No module
other than telemetry/relay.py writes to the audit file. The daemon refuses
moves while the audit backlog exceeds the configured limit.

Rule 14. Outbound text — planner prompts, notifications, INFO+ logs — is
built only via egress.sanitize.Sanitizer. Raw paths or free text from the
store never reach an httpx call, a webhook, or a log.info(). The daemon
refuses to start without EGRESS_KEY.

Rule 15. A new module lands with its tests run in the sandbox, and the run
output pasted in the PR. Written-but-not-run tests are not tests.
