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

## Layout
See docs/ingenaning-software-build.md §1. Documents in docs/ are the spec;
docs/DECISIONS.md overrides them where they conflict.
