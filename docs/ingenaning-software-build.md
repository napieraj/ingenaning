# ingenaning — software build

`ingenaning` is the tiering daemon for the apartment cluster. It runs inside the `nas` LXC on pve1 (HA to pve2), watches a mergerfs union of local NVMe and the UNAS, and moves files between tiers so the first open lands on NVMe. Placement comes from competing arms — statistical predictors and LLM planners — allocated by a contextual bandit and gated by an executor that enforces pins and never lets a model demote.

## 1. Repository layout

```
ingenaning/
├── pyproject.toml
├── ingenaning/
│   ├── __init__.py
│   ├── cli.py              # click CLI: status, why, arms, plan, pin, run
│   ├── daemon.py           # aningd: scheduler + event loop
│   ├── config.py           # pydantic models for policy.yaml, secrets
│   ├── store/
│   │   ├── db.py           # SQLite via sqlite3 + WAL, migrations
│   │   ├── schema.sql
│   │   └── queries.py
│   ├── telemetry/
│   │   ├── fatrace.py      # tail fatrace, normalise, batch insert
│   │   ├── scanner.py      # periodic walk: sizes, tiers, keys
│   │   └── keys.py         # project_key / media_key derivation
│   ├── signals/
│   │   ├── ingest.py       # HTTP + MQTT intake, schema validation
│   │   ├── registry.py     # declared signals, TTLs, bucket mapping
│   │   └── vector.py       # current context vector + bucketing
│   ├── arms/
│   │   ├── base.py         # Arm protocol: propose(ctx) -> [Proposal]
│   │   ├── heat.py
│   │   ├── next_episode.py
│   │   ├── project_spread.py
│   │   ├── client_affinity.py
│   │   ├── sequence.py     # association rules, weekly refit
│   │   ├── newest_rip.py
│   │   ├── model_recency.py
│   │   ├── planner.py      # LLM arm: prompt build, Ollama call, schema check
│   │   └── intent.py       # compiles intent.md -> policy.generated.yaml
│   ├── bandit/
│   │   ├── bandit.py       # contextual Thompson sampling
│   │   └── allocate.py     # space allocation across arms
│   ├── executor/
│   │   ├── plan.py         # gate: pins, budgets, quiet hours, opened-recently
│   │   ├── move.py         # rsync --inplace, verify, unlink, log
│   │   ├── evict.py        # lowest-expected-value eviction
│   │   └── events.py       # signal-triggered runs
│   ├── outcomes/
│   │   ├── outcomes.py     # match opens to proposals, write rewards
│   │   └── metrics.py      # first-open-on-hot, per-arm hit rate
│   ├── egress/
│   │   └── sanitize.py     # single outbound choke point (D-010)
│   ├── audit.py            # emit(); client for the relay's audit socket; local bounded queue
│   ├── api/
│   │   ├── app.py          # FastAPI, serves the single-page UI + JSON
│   │   ├── templates/      # Jinja partials for htmx
│   │   ├── static/         # index.html, app.js, htmx.min.js
│   │   └── routes/         # proposals, arms, pins, intent, feedback, signals, events
│   └── prompts/
│       ├── p_media.md
│       ├── p_project.md
│       ├── p_context.md
│       ├── p_all.md
│       ├── p_conservative.md
│       └── p_intent.md
├── systemd/
│   ├── aningd.service
│   ├── aning-relay.service   # host side: telemetry + audit receiver
│   ├── aning-audit-anchor.timer  # host: publish (seq, hash) every 15 min
│   ├── aning-audit-verify.timer  # host: weekly chain verification
│   └── aning-ui.service
├── deploy/
│   ├── lxc-200.conf
│   ├── fstab.container
│   └── install.sh
└── tests/
```

Python 3.12, `uv` for env, single wheel. Dependencies: `click`, `pydantic`, `fastapi`, `uvicorn`, `paho-mqtt`, `httpx`, `apscheduler`, `numpy`, `scipy` (Beta sampling), `pyyaml`. No ORM.

## 2. Data model

SQLite, WAL mode, at `/var/lib/ingenaning/aning.db`. Snapshotted by ZFS with the container.

```sql
CREATE TABLE files (
  path TEXT PRIMARY KEY, size INTEGER, tier TEXT CHECK(tier IN ('hot','cold')),
  mtime INTEGER, last_open INTEGER, project_key TEXT, media_key TEXT,
  pinned TEXT, pinned_until INTEGER);
CREATE TABLE access (
  ts INTEGER, client TEXT, op TEXT, path TEXT, bytes INTEGER);
CREATE INDEX access_path_ts ON access(path, ts);
CREATE TABLE signal_defs (
  name TEXT PRIMARY KEY, kind TEXT CHECK(kind IN ('enum','number','bool','text')),
  values_json TEXT, ttl_s INTEGER, bucket_role TEXT, source TEXT, created INTEGER);
CREATE TABLE signals (
  ts INTEGER, name TEXT, value TEXT, source TEXT);
CREATE INDEX signals_name_ts ON signals(name, ts);
CREATE TABLE context (
  id INTEGER PRIMARY KEY, ts INTEGER, vector_json TEXT, bucket TEXT, degraded INTEGER);
CREATE TABLE proposals (
  id INTEGER PRIMARY KEY, ts INTEGER, run_id TEXT, arm TEXT, path TEXT,
  reason_class TEXT, reason TEXT, window_h INTEGER, size INTEGER,
  context_id INTEGER, accepted INTEGER, moved_at INTEGER);
CREATE TABLE outcomes (
  proposal_id INTEGER PRIMARY KEY, opened_at INTEGER, hit INTEGER,
  feedback INTEGER);           -- feedback: -1 / 0 / +1 from the UI
CREATE TABLE moves (
  ts INTEGER, path TEXT, src TEXT, dst TEXT, reason TEXT, proposal_id INTEGER,
  bytes INTEGER, ms INTEGER, ok INTEGER, err TEXT);
CREATE TABLE posteriors (
  arm TEXT, bucket TEXT, alpha REAL, beta REAL, updated INTEGER,
  PRIMARY KEY (arm, bucket));
CREATE TABLE schedules (
  id INTEGER PRIMARY KEY, name TEXT, cron TEXT, action TEXT, arms TEXT,
  filter TEXT, enabled INTEGER, source TEXT);   -- source: yaml | ui | intent
CREATE TABLE sequences (
  a TEXT, b TEXT, support INTEGER, confidence REAL, window_s INTEGER);
```

## 3. Telemetry

Telemetry is produced on the Proxmox host by `aning-relay` (D-001): one `fatrace -c -t -f CWO` per branch with the branch as cwd, paths rewritten to union-relative, client attributed on the host, JSON lines served on `/run/ingenaning/access.sock`. `telemetry/fatrace.py` in the container consumes the socket and batches into `access` — 1,000 rows or 1 s, with a timer flush. Lines may arrive out of order across branches and may include `{"type":"dropped"}` markers.

`scanner.py` walks the union every 15 min, updates `files` (size, tier via `getfattr -n user.mergerfs.relpath`, keys). Full walk on a 100k-file tree takes seconds.

Keys: `project_key` = second path component under `/projects`; `media_key` = series or album folder under `/media` with `SxxEyy` stripped.

## 4. Signals

The daemon knows nothing about homes, phones, or VPNs. It accepts named signals from any producer, stores them, and derives a context vector and a bucket from whatever has been declared. Home Assistant, a UniFi webhook, a cron job on the Mac, a shell one-liner — all equal.

### 4.1 Declaring a signal

A producer registers a signal once (or the operator does it in the UI). Undeclared signals are stored but ignored for bucketing until declared.

```
POST /api/signals/defs
{"name":"presence","kind":"enum","values":["home","away","approaching"],
 "ttl_s":900,"bucket_role":"primary","source":"home-assistant"}
```

| Field | Meaning |
|---|---|
| `kind` | `enum`, `number`, `bool`, `text` |
| `values` | allowed values for `enum`; optional ranges for `number` |
| `ttl_s` | after this without an update the signal is `unknown` |
| `bucket_role` | `primary`, `secondary`, `feature`, or `none` — see 4.3 |
| `source` | free text, for the UI |

### 4.2 Emitting

```
POST /api/signals
{"name":"presence","value":"approaching","ts":1757355000}
```

or MQTT `ingenaning/signal/<name>` with the value as payload. Batches accepted. Unknown names create a `signal_defs` row with `bucket_role=none`. Emitters need a bearer token from `secrets.env`; nothing else.

Two signals are built in and always present: `clock.daypart` (`day`/`evening`/`night`) and `clock.daytype` (`weekday`/`weekend`).

### 4.3 Context vector and bucket

Every run, `signals/vector.py` builds the vector from the latest non-expired value of each declared signal. The bandit bucket is the cross product of every signal with `bucket_role=primary`, plus the two clock signals. `secondary` signals are appended to the vector for planners but don't split the bandit; `feature` signals go only to planners.

Keep `primary` to two or three signals. Every extra one multiplies the buckets and thins the data each posterior learns from.

Expired or missing signals contribute `unknown`; if every `primary` signal is `unknown` the run is flagged `degraded` and timers continue as normal.

### 4.4 Events

Event triggers in `policy.yaml` reference signals by name and value:

```yaml
events:
  - on: presence == approaching
    run: promote  window_h: 6  arms: [p-context, p-media, next-episode, heat]
  - on: vpn.phone == true
    run: promote  window_h: 8  arms: [p-project, project-spread, client-affinity]
  - on: workstation == on
    run: promote  window_h: 4  arms: [client-affinity, p-project, sequence]
```

Any signal, any value. What emits them is not the daemon's concern.

## 5. Arms

Protocol:

```python
class Arm(Protocol):
    name: str
    kind: Literal["stat", "planner", "intent"]

    def propose(self, ctx: Context, pool: CandidatePool) -> list[Proposal]: ...
```

`CandidatePool` = cold files plus their `files` row and 30 d of `access`, filtered by the run's `filter` glob. Statistical arms return within 1 s. Planner arms call Ollama at `http://mac.oskar.co:11434` with a 60 s timeout and return `[]` on failure.

Planner prompt assembly (`arms/planner.py`):

1. System prompt from `prompts/<arm>.md`.
2. Context block: the current vector, one line per signal with its source and age.
3. Slice: per-arm selection of history and pool, capped at 12k tokens, every path and text field passed through `egress.sanitize.Sanitizer`; responses are mapped back through the local pseudonym table before validation against the pool.
4. Feedback block: this arm's last 50 proposals with `hit` and UI `feedback`.
5. Output schema:

```json
{"proposals":[{"path":"...","reason_class":"next-episode|similar|project-sibling|calendar|context|other",
               "reason":"<=120 chars","window_h":6,"confidence":0.0}]}
```

Parsed with `pydantic`; any path not in the pool, any pinned path, any unknown field → the proposal is dropped, the arm gets a `schema_error` counter. Responses are JSON-only via Ollama's `format: json`.

Intent arm: reads `/srv/nas/intent.md`, emits `policy.generated.yaml` fragments with `until`. Fragments become `schedules` rows with `source=intent` and pins with `pinned_until`.

## 6. Bandit

Per `(arm, bucket)` a Beta(α, β). Run:

```python
free = hot_free_bytes()
samples = {arm: beta.rvs(α, β) for arm in arms}          # current bucket
proposals = {arm: arm.propose(ctx, pool) for arm in arms}
consensus = count paths proposed by >1 arm; multiply their priority by (1 + 0.5·n)
for arm in sorted(arms, key=samples, reverse=True):
    budget = free * samples[arm] / sum(samples)
    take proposals[arm] in rank order until budget or max_promote_per_run
```

Reward on outcome: `hit=1` → α+=1, else β+=1, in the bucket the proposal was made under. Weekly decay α,β ×0.95 toward the prior. UI feedback adds ±1 to α or β directly.

Eviction (`executor/evict.py`) when `free < minfreespace` before a run: rank hot, unpinned files by expected value = posterior mean of the backing arm × recency factor; files with no backing arm first; evict until the run's allocation fits.

## 7. Executor

`executor/plan.py` produces a `Plan` from proposals: drops pinned, drops files opened within `skip_if_opened_within`, drops anything violating `policy.generated.yaml`, applies `max_moves_per_hour`. `--dry-run` writes the plan to `proposals.accepted=0` and stops.

`executor/move.py`:

```
rsync -a --inplace --no-compress src dst
stat compare size + mtime
unlink src
INSERT moves
UPDATE files SET tier=...
```

A move never crosses a directory boundary and never renames. mergerfs presents the same path throughout.

Events (`executor/events.py`) subscribe to context changes and fire runs per `policy.yaml` `events:`; timers via APScheduler for `schedules`.

## 8. Outcomes and metrics

Every 15 min, join `access` since last run with open `proposals` (moved, window not expired): a read of the path → `hit=1`. Expired windows → `hit=0`. Both update `posteriors`.

Metrics exposed at `/api/metrics` and kept in a daily table:

- `first_open_hot_rate` — of all first opens of a file in 24 h, share that were hot. The number.
- per-arm `hit_rate_7d`, `proposals_7d`, `bytes_moved_7d`
- `hot_utilisation`, `evictions_7d`, `moves_failed_7d`

## 8b. Audit

See SECURITY.md "Append-only audit log". `audit.emit()` is called before every state change; the relay appends; `aning audit verify` walks the chain. `/api/status` exposes `audit_backlog` and `audit_last_anchor_ts`.

## 9. API

FastAPI on `:8080` inside the container, bound to the storage VLAN. No reverse proxy or auth for now; the VLAN is the boundary. Traefik and Authentik can front it later without changing the app.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/status` | tiers, free, degraded flags, last runs |
| GET | `/api/proposals?since=&arm=&state=` | proposal feed |
| POST | `/api/proposals/{id}/feedback` | `{"value": -1|0|1}` |
| GET | `/api/arms` | posteriors per bucket, 7 d stats |
| POST | `/api/arms/{name}/enabled` | toggle |
| GET/POST/DELETE | `/api/pins` | pins with `until` |
| GET/PUT | `/api/intent` | `intent.md` |
| GET/POST/PUT/DELETE | `/api/schedules` | UI-owned schedules |
| GET/POST/DELETE | `/api/signals/defs` | declared signals |
| POST | `/api/signals` | emit one or many |
| GET | `/api/signals/current` | vector, bucket, ages |
| POST | `/api/run` | `{"arms":[...],"window_h":6,"dry_run":true}` |
| GET | `/api/why?path=` | move history and reasons |
| GET | `/api/files?path=&tier=` | browse the union |
| GET | `/api/metrics?days=` | dashboard series |
| WS | `/ws/events` | live moves, proposals, context changes |

## 10. Configuration

`/etc/ingenaning/policy.yaml` (hand-written, authoritative), `/etc/ingenaning/policy.generated.yaml` (intent arm, expiring), `/etc/ingenaning/secrets.env` (emitter bearer tokens, optional MQTT broker, Ollama URL). Schedules created in the UI live in the `schedules` table with `source=ui` and are merged at load; on conflict `yaml > ui > intent`.

## 11. Deployment

`deploy/install.sh` on the container: creates `/var/lib/ingenaning`, installs the wheel into `/opt/ingenaning/.venv`, drops the three units, enables `fatrace`. Failover: the container is in Proxmox HA group `storage`; the DB and config are on the container rootfs, replicated with it.

Ollama on the Mac: `ollama serve` bound to the storage VLAN, models pulled once: `qwen2.5:7b-instruct`, `qwen2.5:14b-instruct`, one 30B-class at Q4. The Mac's `launchd` keeps it up.

## 12. Testing

- Unit: arms against fixture pools; bandit convergence on synthetic rewards; executor gate cases.
- Integration: a `tmpfs` hot branch and a loop-mounted cold branch under mergerfs; run a synthetic access replay and assert `first_open_hot_rate` rises.
- Chaos: kill Ollama mid-run, let every signal expire, fill hot to 100 %, pull the cold mount — each must degrade to a logged state, never to silence.

## 13. Build order

1. `store`, `telemetry`, `cli status` — a week of real data.
2. `executor` with pins and `evict`, `demote` only.
3. Statistical arms, `bandit`, `outcomes`, dry-run promotion.
4. Promotion live; metrics endpoint.
5. `signals/`, events; first emitters (Home Assistant, UniFi webhook) as external scripts outside the repo.
6. `arms/planner.py` with `p-media` and `p-project`, dry-run; then live.
7. `arms/intent.py`; remaining planners.
8. UI (separate doc).
9. `sequence` mining; weekly decay; LoRA export job.
