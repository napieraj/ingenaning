# ingenaning

Tiering daemon for a two-node Proxmox cluster: mirrored NVMe on each node as
the hot tier, a NAS as the cold tier, one mergerfs namespace, and learned
placement so the first open of a file lands on NVMe.

- Spec: `docs/ingenaning-software-build.md`, `docs/ingenaning-ui.md`
- Decisions that override the spec: `docs/DECISIONS.md`
- Rules for anyone editing: `AGENTS.md` (and `CLAUDE.md` for Claude Code)
- Deploy: `deploy/README.md`

`make sync && make check` to start.
