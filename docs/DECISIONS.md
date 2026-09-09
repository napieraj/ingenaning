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

## D-006 — 2026-09-08 — Reuse OSS for union and mover
Survey found no open-source tool that predicts first open or learns from
outcomes; every tiering tool (mergerfs movers, autotier, HSMs) places by
frequency/age/fullness after the fact. The union and the move primitive are
therefore reused, not rewritten:
- mergerfs stays the union, per its documented tiered-cache pattern.
- executor/move.py adopts the mechanics of mergerfs-cache-mover (GPL-3,
  mechanics only, no code vendored): it issues no rsync at all — it copies
  to a temporary name and then atomically renames, specifically to avoid
  racing mergerfs's newest-file resolution, then removes the source and
  cleans empty directories. Taken with it: the single-instance lock and the
  threshold/target hysteresis. The rsync flag set, if rsync is used at all,
  comes from trapexit's example movers under tools/ in the mergerfs repo
  (ISC). Only arm-driven file selection is original code.
- autotier is reference only. Speedloader is a candidate for /models if
  that directory dominates.
See AGENTS.md "Prior art" and rule 11.

## D-007 — 2026-09-08 — Privacy and security as repo rules
The database is a behavioural profile. PRIVACY.md classifies data (P0/P1/P2),
mandates local-only inference, retention, ZFS-encrypted rootfs with raw
replication, purge commands, and an auth gate before any exposure beyond the
storage VLAN. SECURITY.md sets engineering rules (no shell interpolation,
path validation, untrusted model output and signals, hashed emitter tokens,
pinned deps with pip-audit). tests/test_privacy.py enforces the mechanical
subset. Rule 12 added to AGENTS.md.

## D-008 — 2026-09-08 — Append-only, hash-chained, off-box-anchored audit
A compromised daemon must not be able to hide its actions. Audit events are
hash-chained JSON lines written only by the host relay (root) to a
`chattr +a` file on `tank/audit`, snapshotted every 15 min with holds,
replicated raw to pve2, and anchored (seq, hash) every 15 min to the
basement gateway's syslog and an append-only share on the DS1525+.
`aning audit verify` checks the chain against anchors; a weekly timer runs
it. The daemon stops moving files when it cannot audit. See SECURITY.md.

## D-009 — 2026-09-08 — No endpoint pinning for Ollama
mTLS and DNS pinning defend only a hostile device on a five-member VLAN.
They do nothing if the Mac or the container is compromised, which are the
cases that matter. Decision: `https://` and RFC1918 enforced in config,
plain TLS via Caddy on the Mac, optional CA file, no client certs, no
custom resolver. Effort goes to prompt minimisation, the UDM egress ACL for
CT 200, and the audit chain. See SECURITY.md threat-model table.

## D-010 — 2026-09-08 — Egress sanitiser
The planner prompt is the one designed exit for P0 data. `egress/sanitize.py`
is the single choke point: per-prefix path policy (share / basename /
pseudonym / deny, default pseudonym), secret-file denylist, pattern scrub
for PII and credentials, stable HMAC pseudonyms with a local reverse map.
Rule 14 added. Trade-off accepted: fully pseudonymised directories lose
semantic ranking by the planners and rely on sequence/scorer arms; the
per-prefix policy lets you choose per subtree.

## D-011 — 2026-09-08 — Names of known people are pseudonymised by dictionary
No regex finds a name. A local `people` table (names, variants, nicknames)
drives replacement with stable HMAC `person:` tokens before pattern scrub;
a capitalised-token suggester feeds the UI; NER (spaCy) deferred until the
dictionary's misses are observed. The table is P0, never exported.

## D-012 — 2026-09-08 — Schema additions to build doc section 2
store/schema.sql is section 2 with these changes; everything else is verbatim.
(a) files.group_key TEXT and files.ordinal INTEGER replace project_key/media_key
    (D-003). files.seen INTEGER stamps the walk that last saw a row so
    delete_unseen_files can prune one tier after one complete walk of that
    branch (rule 3; the tier argument is mandatory). Indexes on tier and
    (group_key, ordinal).
(b) access.tier TEXT: the branch the relay saw the open on, NULL when unknown;
    needed for first_open_hot_rate. Index on ts.
(c) proposals.features_json TEXT (the scorer's training set) and
    proposals.reject_reason TEXT (the feed shows 'rejected: <why>'). Indexes on
    path, (arm, ts) and ts.
(d) expectations per D-004: id INTEGER PRIMARY KEY, path_glob TEXT,
    deadline INTEGER, bucket TEXT, created INTEGER, met_by TEXT, met_at INTEGER,
    missed INTEGER; index (missed, met_at, deadline) for the open lookup. Only
    executor/ and api/ call its query helpers.
(e) pins(path PRIMARY KEY, tier, until, source, created) for runtime pins from
    the UI, CLI and intent arm; section 2 only has files.pinned per file. yaml
    pins stay in policy.yaml.
(f) runs, counters, arm_state, metrics_daily (section 8, "kept in a daily
    table") and migrations tables.
(g) schedules gets UNIQUE(source, name) so yaml/ui/intent entries with the same
    name coexist and the upsert is keyed by (source, name); plus window_h
    INTEGER and until INTEGER, because intent and UI entries expire (build doc
    section 5 and 10) and a promote timer needs a proposal window like an
    event does (section 4.4).
(h) sequences gets PRIMARY KEY (a, b).
(i) moves gets indexes on (path, ts) and ts.
Paths are normalised on every write and lookup (store/queries.norm_path: one
leading slash, no doubled or trailing slashes) so the relay and the scanner
never fork one file into two rows.

## D-013 — 2026-09-08 — Flat Settings, nested policy.yaml, no default pins
config.Settings is flat (hot_root, hot_floor_free, skip_if_opened_within, ...;
sub-models only for candidates, sequence, scorer, bandit, planners, mqtt, api).
load_settings maps the nested layout of deploy/policy.example.yaml (paths,
budgets, pins, executor, candidates, sequence, scorer, bandit, planners,
signals, events, schedules) onto it; unknown keys are errors because the file
is hand-written and a typo must not silently mean "default". There are no
default pins: Settings().pins == [] and an empty policy pins nothing; the
example's /vm-disks, /models, /scratch, /plex-transcode and /archive come from
the file, not the code. policy.generated.yaml may only add pins and schedules
that carry `until` and never overrides a yaml entry with the same path or name
(yaml > ui > intent). secrets.env carries ANING_EMITTER_TOKENS (comma list),
ANING_OLLAMA_URL and ANING_MQTT_URL (mqtt://user:pass@host:port/prefix);
ANING_* process variables override the file; explicit overrides (CLI flags)
win last.

A typo in policy.yaml is the operator's mistake and must stop the daemon so
it is noticed; a bad entry in policy.generated.yaml is a model's mistake and
must be dropped with a logged warning so the daemon keeps going. Same parser,
opposite consequences by design — do not collapse the two paths into a single
try block.

## D-014 — 2026-09-08 — Tests import the checkout; build backend hatchling
pyproject.toml had no [build-system], so uv treated the project as virtual and
never installed it; `uv run pytest` could not import `ingenaning`, and pytest
was given `pythonpath = ["."]` so the suite ran from the checkout. That left
the build doc's "single wheel" and the `aning`/`aningd` entry points open.
Resolved: [build-system] requires hatchling with build-backend
`hatchling.build`, and [tool.hatch.build.targets.wheel] packages =
["ingenaning"]. hatchling is the uv default, needs no plugin for a single
top-level package, and is already a transitive build dependency, so it adds
nothing to the runtime set. `uv sync` now installs the project editable into
.venv: imports resolve from any working directory, the pytest `pythonpath`
line is deleted, and `aning`/`aningd` appear in .venv/bin. Those two scripts
raise ImportError until cli.py and daemon.py land; that is expected and is not
stubbed.

## D-015 — reserved; the plaintext entities draft was superseded before
   it was written. See D-017.

## D-016 — reserved; the HA-importer draft with ha_ref lookup was
   superseded before it was written. See D-017.
