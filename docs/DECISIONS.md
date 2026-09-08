# Decisions

Dated, append-only. A decision here overrides the spec documents.

## D-001 — 2026-09-08 — Telemetry runs on the host
fanotify requires CAP_SYS_ADMIN in the host mount namespace; the unprivileged
container cannot run fatrace. A stdlib-only relay (`aning-relay`) runs on each
Proxmox node, one `fatrace -c` per branch with cwd set to the branch, rewrites
paths to union-relative, attributes clients, and serves JSON lines on
/run/ingenaning/access.sock. The directory is bind-mounted into CT 200. The
container consumes; it never spawns fatrace.

## D-002 — 2026-09-08 — NFS export: OPEN
Kernel nfsd cannot run in the unprivileged container. Options:
(a) nfs-ganesha in the container — keeps union, executor, and export together;
    userspace, benchmark at 25G before committing.
(b) kernel nfsd on the host exporting a host-side mergerfs union — faster,
    but moves the union and executor to the host and leaves the container
    with API and learners only.
(c) privileged container.
Default until measured: (a). Benchmark task: tests/bench/nfs_ganesha.md.
Client attribution under (a): `ganesha_mgr show_clients` via `pct exec`.

## D-003 — 2026-09-08 — No hand-written predictors
heat, next_in_sequence, group_spread, client_affinity, newest_rip and
model_recency are removed. Candidates are generated generically
(arms/candidates.py); scoring is learned (arms/sequence.py, arms/scorer.py)
or semantic (planner arms). Cold start is the planners' job.

## D-004 — 2026-09-08 — Expectations are held out
User-stated expectations are a reward signal, not an input. No arm,
candidate generator, or prompt may read them. See build doc §5.

## D-005 — 2026-09-08 — Spec corrections
The build doc's `fatrace -c -t -f RWO /srv/nas` is wrong (no path argument,
R is per-read noise) and superseded by D-001. The build doc's "container
exports NFS" is superseded by D-002.

## D-018 — 2026-09-08 — AGENTS.md path prose was wrong; the deploy file is authoritative
AGENTS.md said both branches are bind-mounted into the container "at the same
paths". True for cold, false for hot: deploy/lxc-200.conf maps host /tank/hot to
/mnt/hot. The deploy file is authoritative and the prose is corrected. No code
change: the daemon runs inside CT 200 and correctly defaults hot_root to
/mnt/hot, while systemd/aning-relay.service correctly passes the host path.
