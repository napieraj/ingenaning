-- ingenaning store, migration 1. Build doc section 2 plus the additions recorded in
-- docs/DECISIONS.md D-012. Applied once by store/db.py inside one transaction; the
-- `migrations` table itself is created by db.py before this runs.
-- Times are epoch seconds. Paths are union-relative with one leading slash.

-- files: one row per file in the union. group_key/ordinal are the generic
-- neighbourhood keys of D-003; seen is the scan stamp of the last walk that saw the row.
CREATE TABLE IF NOT EXISTS files (
  path TEXT PRIMARY KEY, size INTEGER, tier TEXT CHECK(tier IN ('hot','cold')),
  mtime INTEGER, last_open INTEGER, group_key TEXT, ordinal INTEGER,
  pinned TEXT, pinned_until INTEGER, seen INTEGER);
CREATE INDEX IF NOT EXISTS files_tier ON files(tier);
CREATE INDEX IF NOT EXISTS files_group ON files(group_key, ordinal);

-- access: the telemetry stream. tier is the branch the relay saw the open on;
-- NULL when it could not tell.
CREATE TABLE IF NOT EXISTS access (
  ts INTEGER, client TEXT, op TEXT, path TEXT, bytes INTEGER, tier TEXT);
CREATE INDEX IF NOT EXISTS access_path_ts ON access(path, ts);
CREATE INDEX IF NOT EXISTS access_ts ON access(ts);

CREATE TABLE IF NOT EXISTS signal_defs (
  name TEXT PRIMARY KEY, kind TEXT CHECK(kind IN ('enum','number','bool','text')),
  values_json TEXT, ttl_s INTEGER, bucket_role TEXT, source TEXT, created INTEGER);

CREATE TABLE IF NOT EXISTS signals (
  ts INTEGER, name TEXT, value TEXT, source TEXT);
CREATE INDEX IF NOT EXISTS signals_name_ts ON signals(name, ts);

CREATE TABLE IF NOT EXISTS context (
  id INTEGER PRIMARY KEY, ts INTEGER, vector_json TEXT, bucket TEXT, degraded INTEGER);

-- proposals: features_json is the generic feature vector at proposal time (the
-- scorer's training input); reject_reason is why the executor gate dropped it.
CREATE TABLE IF NOT EXISTS proposals (
  id INTEGER PRIMARY KEY, ts INTEGER, run_id TEXT, arm TEXT, path TEXT,
  reason_class TEXT, reason TEXT, window_h INTEGER, size INTEGER,
  context_id INTEGER, accepted INTEGER, moved_at INTEGER,
  features_json TEXT, reject_reason TEXT);
CREATE INDEX IF NOT EXISTS proposals_path ON proposals(path);
CREATE INDEX IF NOT EXISTS proposals_arm_ts ON proposals(arm, ts);
CREATE INDEX IF NOT EXISTS proposals_ts ON proposals(ts);

CREATE TABLE IF NOT EXISTS outcomes (
  proposal_id INTEGER PRIMARY KEY, opened_at INTEGER, hit INTEGER,
  feedback INTEGER);           -- feedback: -1 / 0 / +1 from the UI; NULL when none given

CREATE TABLE IF NOT EXISTS moves (
  ts INTEGER, path TEXT, src TEXT, dst TEXT, reason TEXT, proposal_id INTEGER,
  bytes INTEGER, ms INTEGER, ok INTEGER, err TEXT);
CREATE INDEX IF NOT EXISTS moves_path_ts ON moves(path, ts);
CREATE INDEX IF NOT EXISTS moves_ts ON moves(ts);

CREATE TABLE IF NOT EXISTS posteriors (
  arm TEXT, bucket TEXT, alpha REAL, beta REAL, updated INTEGER,
  PRIMARY KEY (arm, bucket));

-- schedules: yaml, ui and intent entries with the same name coexist; the upsert
-- key is (source, name). until expires intent/ui entries; window_h is the
-- proposal window a promote run uses.
CREATE TABLE IF NOT EXISTS schedules (
  id INTEGER PRIMARY KEY, name TEXT, cron TEXT, action TEXT, arms TEXT,
  filter TEXT, enabled INTEGER, source TEXT, window_h INTEGER, until INTEGER,
  UNIQUE (source, name));   -- source: yaml | ui | intent

CREATE TABLE IF NOT EXISTS sequences (
  a TEXT, b TEXT, support INTEGER, confidence REAL, window_s INTEGER,
  PRIMARY KEY (a, b));

-- pins: runtime pins from the UI, CLI and intent arm. yaml pins live in policy.yaml
-- and are not stored here.
CREATE TABLE IF NOT EXISTS pins (
  path TEXT PRIMARY KEY, tier TEXT CHECK(tier IN ('hot','cold')),
  until INTEGER, source TEXT, created INTEGER);

-- expectations: held-out reward signal (D-004). Only executor/ and api/ read it.
CREATE TABLE IF NOT EXISTS expectations (
  id INTEGER PRIMARY KEY, path_glob TEXT, deadline INTEGER, bucket TEXT, created INTEGER,
  met_by TEXT, met_at INTEGER, missed INTEGER);
CREATE INDEX IF NOT EXISTS expectations_open ON expectations(missed, met_at, deadline);

-- runs: one row per executor run, updated as it progresses.
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, ts INTEGER, action TEXT, arms TEXT, trigger TEXT,
  dry_run INTEGER, context_id INTEGER, proposals INTEGER, accepted INTEGER,
  moved INTEGER, bytes INTEGER, evicted INTEGER, finished INTEGER, err TEXT);

CREATE TABLE IF NOT EXISTS counters (
  name TEXT PRIMARY KEY, value INTEGER);

CREATE TABLE IF NOT EXISTS arm_state (
  arm TEXT PRIMARY KEY, enabled INTEGER, schema_errors INTEGER, last_run INTEGER,
  last_error TEXT);

-- metrics_daily: build doc section 8, "kept in a daily table".
CREATE TABLE IF NOT EXISTS metrics_daily (
  day TEXT, name TEXT, value REAL, PRIMARY KEY (day, name));
