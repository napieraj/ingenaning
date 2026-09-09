"""Signals: build doc section 4.

A signal is a name, a kind, a TTL and a bucket role — nothing else. The daemon
accepts named signals from any producer, stores them, and derives a context
vector and a bandit bucket from whatever has been declared. What a name means
is the producer's business (AGENTS.md rule 4).

- `registry`: the `SignalDef` model, the two built-in clock signals, and the
  declare/remove helpers.
- `vector`: the context vector, the bucket, and the degraded flag.
- `ingest`: the HTTP and MQTT intake, both landing in one `emit()`.
"""

from __future__ import annotations
