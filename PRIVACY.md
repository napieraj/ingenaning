# PRIVACY.md — what this system knows, and the rules that follow

ingenaning records every file you open, when, from which device, where you
were, what was switched on, and what you said you intended to do. Joined
together, that is a behavioural profile more detailed than most services
hold. Treat the database as the most sensitive thing in the flat. These rules
exist so that stays true in code, not just intent.

## 1. Data classification

| Class | Examples | Rule |
|---|---|---|
| **P0 — profile** | `access`, `signals`, `context`, `proposals`, `outcomes`, `expectations`, `intent.md`, planner prompts and responses | Never leaves the storage VLAN. Never sent to any model that is not local. Encrypted at rest. Retention-limited. |
| **P1 — operational** | `moves`, `runs`, `posteriors`, `arm_state`, `metrics_daily` | Local only. May appear in logs. |
| **P2 — configuration** | `policy.yaml`, schedules, pins | Local only. Safe to back up in the clear. |

`files` (paths, sizes, tiers) is P0: a path list is a catalogue of what you own.

## 2. Principles

1. **Local only, by construction.** The only outbound network calls the
   daemon may make are to the Ollama URL in `policy.yaml` and the MQTT
   broker. The Ollama URL must be `https://` and its host must be an
   RFC1918 address or resolve to one. No cloud model, no analytics, no
   update checks, no crash reporting. `tests/test_privacy.py` enforces
   this by scanning for outbound clients and by refusing `http://` and
   non-private Ollama hosts in config validation. Endpoint pinning and
   client certificates are deliberately not used — see SECURITY.md
   "Threat model for the model endpoint".
2. **Minimise.** Store what a decision needs and nothing more. `access`
   stores path, client, op, and time — not file contents, not bytes read
   beyond size, not process names. Signals store the declared value, not the
   source payload. Planners receive slices, never the whole database.
3. **Expire.** P0 rows have a retention horizon (`privacy.retention` in
   `policy.yaml`, default 90 d for `access`/`signals`/`context`, 180 d for
   `proposals`/`outcomes`). A nightly job deletes beyond it. Aggregates
   (`sequences`, `posteriors`, scorer models) are kept; they are derived and
   cannot reconstruct a day.
4. **Encrypt at rest.** The container's rootfs dataset — which holds the
   database, `intent.md`, and prompts — is a ZFS native-encryption dataset
   with the key loaded at boot from the host. Replication to pve2 is raw
   (`zfs send -w`) so the replica is encrypted too. Backups of the database
   go only through PBS, which is itself encrypted.
5. **Purge on demand.** `aning purge --all` and `aning purge --path <glob>`
   delete P0 rows for everything or for a subtree, then `VACUUM`. The UI has
   the same button. Purging does not touch files on either tier.
6. **No inference beyond the task.** The daemon predicts which files to move.
   It does not label moods, health, relationships, or habits, and prompts
   must not ask a model to. A planner prompt may say "rank these paths for
   likely access"; it may not say "describe this person."
7. **Prompts are P0 and logged as such.** Every prompt sent to a planner and
   every response is stored under the same retention as `access` and shown
   in the UI on request. Nothing a model was told is hidden from you.
8. **Least privilege.** The daemon runs as user `aning`, owns nothing
   outside `/var/lib/ingenaning` and `/srv/nas`, and has no sudo. The relay
   on the host is root only because fanotify requires it; it holds no P0
   data beyond the lines in flight and its bounded buffer.
9. **Authenticated when exposed.** The API and UI bind to the storage VLAN
   with no auth in phase 1. Before the API is reachable from any other
   network — a second VLAN, the basement tunnel, a phone — it goes behind
   Traefik with Authentik, and every signal emitter uses its own revocable
   bearer token. This is a gate in the build order, not a nice-to-have.
10. **Everything outbound is sanitised.** Planner prompts, notifications,
    and INFO+ log lines pass through `egress/sanitize.py` and nothing
    else may build outbound text. Per-directory policy decides whether a
    path leaves as-is, as basename with pseudonymised parents, fully
    pseudonymised (default), or not at all; secret-shaped filenames never
    leave regardless. Free text is scrubbed of emails, phone numbers,
    IBANs, cards, IDs, IPs, MACs, JWTs, key-shaped tokens, and credential
    URLs. Pseudonyms are stable per deployment, keyed by `EGRESS_KEY`, and
    reversible only inside the container. The UI shows the scrub preview
    for `intent.md` before you save it.

    **People.** A local `people` table holds the names, variants, and
    nicknames of people you know. Every occurrence in outbound text or a
    shared path becomes a stable `person:<token>` pseudonym; the mapping is
    reversible only inside the container. The table is the most sensitive
    thing in the database — a list of everyone in your life — and is never
    exported, never in prompts, purged with P0, and shown in the UI only
    behind the same VLAN boundary. The UI suggests capitalised tokens that
    look like names and aren't yet listed; you add or ignore them. A local
    NER model may be added later as a suggester (DECISIONS), never as the
    only gate.

11. **Logs are not a second database.** Application logs record paths
    only at DEBUG. INFO and above log counts, arms, durations, and errors.
    Journald retention for the daemon is 14 d.

## 3. Threat model

| Threat | Mitigation |
|---|---|
| Someone with container shell access reads the DB | Encrypted dataset only helps at rest; runtime access means full profile. The container has no interactive users; SSH is host-only. |
| A compromised device on the storage VLAN reads the API | Phase 1 accepts this. Phase 2 (auth) closes it. Do not emit signals from untrusted devices before phase 2. |
| Backup or replica leaks | Raw encrypted send; PBS encryption. |
| A planner model is swapped for a remote one | Config validation rejects non-RFC1918 hosts; the test suite fails on any HTTP client pointed elsewhere. |
| A dependency phones home | Pinned lockfile, `pip-audit` in CI, no dependency with network behaviour beyond `httpx` and `paho-mqtt`. |
| Prompt injection via file names | Paths are data, not instructions. Prompts wrap the candidate list in a delimited block and instruct the model to treat it as opaque; responses are schema-validated and paths must exist in the pool. |

## 4. What you can always do

- See every prompt and response: UI → Arms → any planner → last responses.
- See why any file moved: `aning why <path>`.
- Delete everything the system knows: `aning purge --all`.
- Turn off learning and keep the union: `aning arms disable --all`.
