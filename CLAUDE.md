# CLAUDE.md — ingenaning

Read AGENTS.md first. It holds the environment facts and the rules; this file
is the Claude Code specific working agreement.

## Working loop
1. Read the task. Find the section of docs/ingenaning-software-build.md it
   implements and the entries in docs/DECISIONS.md that touch it.
2. Before writing code that calls an external tool, run it with --help in
   the sandbox and paste the relevant line into a comment. If the tool is
   not installed in the sandbox, say so and write the invocation as a
   TODO(verify) with the man-page URL — never from memory.
3. Write the test first, using fixtures from tests/conftest.py. No test
   may need the real mounts, Ollama, fatrace, or the network.
4. Implement one module. Run `make check`. Keep the diff to that module.
5. If the spec and an environment fact conflict, stop. Append a dated entry
   to docs/DECISIONS.md with the conflict and your proposed resolution, and
   ask before continuing.

## Style
- Python 3.12, type hints everywhere, `from __future__ import annotations`.
- stdlib sqlite3, no ORM. Queries live in ingenaning/store/queries.py.
- Logging via `logging.getLogger(__name__)`. No print.
- Timeouts on every mount touch, every subprocess, every HTTP call.
- Errors degrade to a logged state and continue. The daemon never exits
  because a branch, a signal source, or a planner is unavailable.

## What not to do
- No media, project, or file-type semantics in core code (AGENTS.md rule 4).
- No new dependencies without a line in docs/DECISIONS.md.
- No reading of the expectations table outside executor/ and api/.
- No refactor of files the task did not name.
