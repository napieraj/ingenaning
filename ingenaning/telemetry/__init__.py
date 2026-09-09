"""Telemetry: where the daemon learns what was opened, and what exists.

`relay.py` runs on the Proxmox host (stdlib only, root, one fatrace per branch)
and serves JSON lines on a unix socket. `fatrace.py` runs in the container and
turns those lines into `access` rows. `scanner.py` walks the branches and keeps
`files` current. `keys.py` derives the generic neighbourhood keys of D-003."""

from __future__ import annotations
