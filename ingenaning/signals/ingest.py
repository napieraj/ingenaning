"""Signal intake (build doc section 4.2). HTTP and MQTT both land in `emit()`.

An emitted signal is `{name, value}` with an optional `ts` and `source`;
batches are accepted. An unknown name creates a `signal_defs` row with
`bucket_role=none`, so nothing is lost and nothing is bucketed by accident. A
value the definition rejects is reported back per item and never stops the rest
of the batch — a producer with one bad reading must not cost the daemon the
good ones."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable, Sequence
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from ingenaning.config import Settings
from ingenaning.signals.registry import ensure_declared
from ingenaning.signals.vector import build_vector
from ingenaning.store import queries as q
from ingenaning.store.db import Database

log = logging.getLogger(__name__)

# Roles whose change is worth waking the daemon for: they move the bucket
# (primary) or the planner's view of it (secondary).
_NOTIFYING_ROLES = ("primary", "secondary")

Publish = Callable[[dict[str, Any]], None]

_MQTT_KEEPALIVE_S = 60
_MQTT_RECONNECT_MIN_S = 1
_MQTT_RECONNECT_MAX_S = 60


class SignalIn(BaseModel):
    """One emitted value. `ts` defaults to the intake time, `source` to the
    caller's transport."""

    name: str
    value: Any
    ts: int | None = None
    source: str | None = None


class SignalBatch(BaseModel):
    signals: list[SignalIn] = Field(default_factory=list)


def emit(
    db: Database,
    items: Sequence[SignalIn],
    source: str = "http",
    publish: Publish | None = None,
) -> dict[str, Any]:
    """Store `items` and report `{accepted, rejected, changed}`.

    `rejected` is `[{name, error}]` for the values their definition refused;
    `changed` names the primary and secondary signals whose value differs from
    the one stored before this batch. When anything changed and `publish` is
    given, it is called exactly once with a `context` message carrying the fresh
    vector, bucket and degraded flag — once per batch, not once per item.

    This opens its own write transaction, so it must never be called from
    inside a caller's `Database.tx()` block: that lock is process-wide and does
    not nest."""
    now = int(time.time())
    rows: list[tuple[int, str, str, str]] = []
    rejected: list[dict[str, str]] = []
    changed: list[str] = []
    vector: dict[str, dict[str, Any]] = {}
    bucket = ""
    degraded = False

    with db.tx() as conn:
        # The values as they stood before this batch, so a repeat is not a change.
        previous = {name: row["value"] for name, row in q.latest_signals(conn).items()}
        for item in items:
            d = ensure_declared(conn, item.name, item.source or source)
            try:
                value = d.normalise(item.value)
            except (TypeError, ValueError) as exc:
                rejected.append({"name": item.name, "error": str(exc)})
                continue
            ts = int(item.ts) if item.ts else now
            rows.append((ts, item.name, value, item.source or source))
            moved = d.bucket_role in _NOTIFYING_ROLES and previous.get(item.name) != value
            if moved and item.name not in changed:
                changed.append(item.name)
            previous[item.name] = value
        if rows:
            q.insert_signals(conn, rows)
        if changed:
            vector, bucket, degraded = build_vector(conn, now)

    if rejected:
        log.warning(
            "signal intake from %s: %d of %d values rejected", source, len(rejected), len(items)
        )
    if changed and publish is not None:
        publish(
            {
                "type": "context",
                "changed": changed,
                "bucket": bucket,
                "degraded": degraded,
                "vector": vector,
            }
        )
    return {"accepted": len(rows), "rejected": rejected, "changed": changed}


def parse_payload(raw: str | bytes, name: str | None = None) -> list[SignalIn]:
    """An MQTT payload as a list of `SignalIn`.

    On `<prefix>/signal/<name>` the payload is the bare value, or a JSON object
    `{value, ts, source}`. On the bare `<prefix>/signal` topic it is a JSON
    list, a `{"signals": [...]}` batch, or one `{name, value, ...}` object.
    Anything unparseable or nameless raises ValueError so the caller can drop it
    without storing half of it."""
    text = (raw.decode() if isinstance(raw, bytes) else raw).strip()
    if name:
        try:
            obj: Any = json.loads(text)
        except ValueError:
            obj = text  # a bare value that is not JSON is the value itself
        if isinstance(obj, dict) and "value" in obj:
            return [SignalIn.model_validate({**obj, "name": name})]
        return [SignalIn(name=name, value=obj)]
    obj = json.loads(text)
    if isinstance(obj, dict) and "signals" in obj:
        return SignalBatch.model_validate(obj).signals
    if isinstance(obj, list):
        return [SignalIn.model_validate(o) for o in obj]
    return [SignalIn.model_validate(obj)]


class MqttIngest:
    """Subscribes to `<prefix>/signal/#` and feeds `emit()`.

    `paho` is imported lazily and every failure degrades to a logged state, so
    the daemon runs unchanged with no broker configured, no broker reachable,
    or paho not installed. The client's own network loop runs on its thread;
    `connect_async` never blocks the caller and the keepalive bounds how long a
    dead broker goes unnoticed."""

    def __init__(self, *, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db
        self.cfg = settings.mqtt
        self._client: Any = None
        self._lock = threading.Lock()

    def start(self) -> bool:
        """True when the client was handed to its network loop. False — never an
        exception — when MQTT is not configured or cannot be started."""
        if self.cfg is None:
            log.debug("no MQTT configured; signal intake over MQTT is off")
            return False
        with self._lock:
            if self._client is not None:
                return True
            try:
                # paho-mqtt 2.1.0: Client(callback_api_version, client_id=..., ...);
                # CallbackAPIVersion lives in paho.mqtt.enums.
                import paho.mqtt.client as mqtt
                from paho.mqtt.enums import CallbackAPIVersion
            except ImportError:
                log.warning("paho-mqtt is not installed; MQTT signal intake is off")
                return False
            try:
                client = mqtt.Client(CallbackAPIVersion.VERSION2, client_id="ingenaning")
                if self.cfg.username:
                    client.username_pw_set(self.cfg.username, self.cfg.password)
                if self.cfg.tls:
                    client.tls_set()
                client.on_connect = self._on_connect
                client.on_message = self._on_message
                client.reconnect_delay_set(_MQTT_RECONNECT_MIN_S, _MQTT_RECONNECT_MAX_S)
                # connect_async only records the target; the loop thread dials and
                # keeps redialling, so an unreachable broker never blocks startup.
                client.connect_async(self.cfg.host, self.cfg.port, keepalive=_MQTT_KEEPALIVE_S)
                client.loop_start()
            except OSError:
                log.warning("MQTT signal intake could not start", exc_info=True)
                return False
            self._client = client
        log.info("MQTT signal intake started")
        return True

    def stop(self) -> None:
        """Safe whether or not `start()` ran or succeeded."""
        with self._lock:
            client, self._client = self._client, None
        if client is None:
            return
        try:
            client.loop_stop()
            client.disconnect()
        except OSError:
            log.warning("MQTT signal intake did not stop cleanly", exc_info=True)

    @property
    def _topic(self) -> str:
        prefix = self.cfg.topic_prefix if self.cfg is not None else "ingenaning"
        return f"{prefix}/signal"

    def _on_connect(self, client: Any, _userdata: Any, _flags: Any, reason: Any, *_: Any) -> None:
        log.info("MQTT connected (%s)", reason)
        client.subscribe(f"{self._topic}/#")

    def _on_message(self, _client: Any, _userdata: Any, message: Any) -> None:
        topic = str(message.topic)
        leaf = f"{self._topic}/"
        name = topic[len(leaf) :] if topic.startswith(leaf) else None
        try:
            items = parse_payload(message.payload, name or None)
        except (ValidationError, ValueError):
            # A producer's bad payload is its problem; the intake keeps running.
            log.warning("MQTT signal payload rejected", exc_info=True)
            return
        try:
            emit(self.db, items, source="mqtt")
        except Exception:
            # The network loop must survive any store error: a broker that keeps
            # publishing must not take the intake thread down with it.
            log.warning("MQTT signal batch could not be stored", exc_info=True)
