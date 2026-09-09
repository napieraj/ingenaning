# SECURITY.md

## Reporting
This is a personal project. Open an issue tagged `security` or contact the
maintainer directly. No bounty, no SLA, prompt attention.

## Scope
The daemon (`ingenaning/`), the host relay (`telemetry/relay.py`), the UI,
and the deployment files under `deploy/` and `systemd/`.

## Engineering rules that are security rules
- **No outbound network** except the configured Ollama URL, which must be
  an RFC1918 address. Enforced by `tests/test_privacy.py` and config
  validation.
- **No shell interpolation.** Every subprocess call uses argument lists.
  `shell=True` is forbidden repo-wide (contract test).
- **Paths are validated** against the union root before any move. A path
  containing `..`, a symlink escaping a branch, or a component outside
  `/srv/nas` is rejected and logged. Moves never follow symlinks.
- **Model output is untrusted.** Planner responses are parsed with a strict
  schema; unknown fields, paths outside the pool, and pinned paths are
  dropped. No response content is ever executed, templated into a shell, or
  written to config.
- **Signals are untrusted.** Values are validated against the declared
  `kind` and `values`; text signals are capped at 256 bytes and never
  interpolated into prompts without a length cap and delimiters.
- **Bearer tokens** for signal emitters are per-emitter, stored hashed,
  revocable, and never logged.
- **Dependencies** are pinned in `uv.lock`; CI runs `pip-audit`. Adding a
  dependency requires a DECISIONS.md entry.
- **The relay runs as root** and therefore contains no logic beyond
  fatrace parsing, path rewriting, client lookup, and a bounded socket
  server. It imports only the standard library (contract test).
- **Secrets** live in `/etc/ingenaning/secrets.env`, mode 0600, owned by
  `aning`. Never in `policy.yaml`, never in the repo, never in logs.
- **Rate limits** on the API: feedback, pins, expectations, and run
  endpoints are limited per source address; signal ingestion is limited
  per token.

## Threat model for the model endpoint
What is worth defending on the daemon → Ollama link, and what is not:

| Scenario | Effect of mTLS / DNS pinning | What actually helps |
|---|---|---|
| Mac compromised | none — prompts are delivered to the attacker faithfully | prompt slices, not the database (PRIVACY.md §2.2) |
| Container compromised | none — attacker has the database already | egress ACL, audit chain, encryption at rest |
| Hostile device on the storage VLAN spoofs the Mac | prevented | TLS + RFC1918 check already stops casual redirection; VLAN membership is five known devices |
| Misconfigured URL to a public host | prevented | RFC1918 check, ten lines |

Decision: plain TLS to a Caddy proxy in front of Ollama (free, stops
sniffing), `https://` and RFC1918 enforced in config, no client
certificates, no SPKI pinning, no custom resolver. Effort goes to
minimisation, the egress ACL, and the audit chain — the controls that bear
on the two cases that matter.

## Egress ACL (deploy step, not optional)
On the UDM Pro Max, a rule for CT 200's address on the storage VLAN:
allow → Mac:443 (Caddy/Ollama), allow → broker:8883, allow → UNAS:2049
(NFS), allow → Technitium:53; drop and log everything else outbound.
The container cannot reach a public model host even if every in-daemon
check is defeated, and the drop is logged on a device the container
cannot touch. Recorded in deploy/README.md.

## Phase gates
| Phase | Requirement before proceeding |
|---|---|
| API reachable beyond the storage VLAN | Traefik + Authentik in front; emitter tokens issued |
| Any non-local model | Not permitted. Requires a new DECISIONS.md entry and a change to this file. |
| Backups of the DB outside PBS | Encrypted archive only; key not stored with the archive |

## Append-only audit log

A compromised daemon must not be able to rewrite what it did. The audit
trail therefore lives where the daemon cannot write freely, is hash-chained
so gaps and edits are detectable, and is anchored off-box so the chain head
cannot be forged.

### What is logged
Every security-relevant event, as one JSON line:
move (path, src, dst, arm, proposal_id), pin create/delete, expectation
create/met/missed, feedback, run start/finish, config reload (with file
hash), purge, arm enable/disable, token issue/revoke, auth failure, planner
request (prompt hash, model, arm) and response (hash), relay start/stop,
daemon start/stop, audit-anchor publication.

Each line:
```
{"seq": 4817, "ts": 1757356000, "prev": "<sha256 of previous line>",
 "event": "move", "actor": "aningd@nas", "data": {...},
 "hash": "<sha256 of this line without the hash field>"}
```

### Where it goes
1. **Daemon → host receiver.** The daemon never writes the audit file. It
   sends lines to a second unix socket served by the host relay
   (`/run/ingenaning/audit.sock`). The relay runs as root and appends to
   `/var/log/ingenaning/audit.jsonl` on a dedicated ZFS dataset
   `tank/audit`.
2. **Immutable at the file level.** The file carries `chattr +a` (append
   only). Root on the host can lift it; the container cannot — it has no
   host root and no write path to the file except through the receiver,
   which only appends.
3. **Immutable at the dataset level.** `tank/audit` is snapshotted every
   15 minutes with a `zfs hold` on each snapshot and a 90-day rotation.
   Deleting a held snapshot needs host root and leaves a trace in the
   pool history (`zpool history`), which is itself append-only.
4. **Replicated raw.** Snapshots ship to pve2 with `zfs send -w`; the
   replica is encrypted and held the same way. Compromising one node does
   not let you edit both histories consistently.
5. **Anchored off-cluster.** Every 15 minutes the receiver writes the
   current `seq` and `hash` to two places the cluster cannot alter after
   the fact: a remote syslog line to the basement UCG-Industrial (its
   syslog store is a separate site), and a file on the DS1525+ over an
   append-only SMB share. Verifying the chain later means walking the log
   and comparing against the anchors.

### Verification
`aning audit verify [--since]` re-hashes the chain, checks `prev` links,
checks that every anchor matches the log at that `seq`, and reports the
first divergence. It is run by CI against a fixture and by a weekly host
timer against the live log; a failure pages via the notify webhook.

### What the daemon can still do
Stop sending. That is why the receiver logs `daemon start/stop` from the
socket lifecycle, and why a gap between `run start` and `run finish`, or
silence longer than the run interval, is itself an alert.

### Rules
- No code path under `ingenaning/` opens the audit file. The only writer
  is the relay's receiver.
- `audit.emit()` is fire-and-forget with a bounded local queue; if the
  socket is down, events are queued and `audit_backlog` is exposed in
  `/api/status`. The daemon does not proceed with moves while the backlog
  exceeds 1,000 events — an unauditable move is not made.
- Audit lines contain paths. They are P0 and stay on the audit dataset;
  the anchors contain only `seq` and `hash`.
