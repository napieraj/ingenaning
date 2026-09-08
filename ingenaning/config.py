"""Configuration. policy.yaml is hand-written and authoritative; policy.generated.yaml
(intent arm) may only add pins and schedules that expire; secrets.env carries the
emitter tokens, the Ollama URL and the MQTT URL, with ANING_* process variables on
top of the file.

`Settings` is flat so every module reads `settings.x`; `load_settings` maps the
nested layout of deploy/policy.example.yaml onto it (D-008). There are no default
pins: an empty policy pins nothing."""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote, urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

log = logging.getLogger(__name__)

Tier = Literal["hot", "cold"]
Action = Literal["promote", "demote"]
ArmKind = Literal["stat", "planner"]
Source = Literal["yaml", "ui", "intent", "cli"]

STAT_ARMS: tuple[str, ...] = ("sequence", "scorer")

_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmgtpe]?)(?:i?b)?\s*$", re.I)
_SIZE_MULT = {"": 1, "k": 1 << 10, "m": 1 << 20, "g": 1 << 30, "t": 1 << 40, "p": 1 << 50}
_SIZE_MULT["e"] = 1 << 60
_DUR_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhdw]?)\s*$", re.I)
_DUR_MULT = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_HM_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_WHEN_RE = re.compile(r"^(?:([A-Za-z]{3})[A-Za-z]*\s+)?([01]?\d|2[0-3]):([0-5]\d)$")
_CRON_FIELD_RE = re.compile(r"^[A-Za-z0-9*,/?#LW-]+$")
_DAYS = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}


# --- parsers ----------------------------------------------------------------------


def parse_size(value: int | float | str) -> int:
    """'400G', '1.5M', '2TiB' -> bytes (binary units). Numbers pass through as bytes."""
    if isinstance(value, int | float):
        return int(value)
    m = _SIZE_RE.match(str(value))
    if not m:
        raise ValueError(f"bad size {value!r}: use a number with an optional K/M/G/T suffix")
    return int(float(m.group(1)) * _SIZE_MULT[m.group(2).lower()])


def parse_duration(value: int | float | str) -> int:
    """'10m', '1h', '30d', '2w' -> seconds. Numbers pass through as seconds."""
    if isinstance(value, int | float):
        return int(value)
    m = _DUR_RE.match(str(value))
    if not m:
        raise ValueError(f"bad duration {value!r}: use a number with an s/m/h/d/w suffix")
    return int(float(m.group(1)) * _DUR_MULT[m.group(2).lower()])


def parse_percent(value: int | float | str) -> float:
    """'80%' -> 0.8; 0.5 and '0.25' pass through. Must land in [0, 1]."""
    if isinstance(value, str):
        s = value.strip()
        frac = float(s[:-1]) / 100 if s.endswith("%") else float(s)
    else:
        frac = float(value)
    if not 0.0 <= frac <= 1.0:
        raise ValueError(f"bad percentage {value!r}: use '80%' or a fraction between 0 and 1")
    return frac


def when_to_cron(value: str) -> str:
    """'Sun 04:00' -> '0 4 * * 0', 'HH:MM' -> 'M H * * *'; five cron fields pass
    through unchanged. Anything else is a ValueError."""
    if not isinstance(value, str):
        raise ValueError(f'bad schedule {value!r}: quote times like "04:00" in YAML')
    s = value.strip()
    fields = s.split()
    if len(fields) == 5 and all(_CRON_FIELD_RE.match(f) for f in fields):
        return " ".join(fields)
    m = _WHEN_RE.match(s)
    if not m:
        raise ValueError(f"bad schedule {value!r}: use 'HH:MM', 'Sun HH:MM' or five cron fields")
    day, hh, mm = m.group(1), int(m.group(2)), int(m.group(3))
    dow = "*"
    if day:
        if day.lower() not in _DAYS:
            raise ValueError(f"bad schedule {value!r}: unknown day {day!r}")
        dow = str(_DAYS[day.lower()])
    return f"{mm} {hh} * * {dow}"


def _norm_path(path: str) -> str:
    return "/" + "/".join(part for part in path.split("/") if part)


def _to_epoch(v: Any) -> int | None:
    """Epoch seconds from an int, a digit string, an ISO date/datetime string, or
    the date/datetime objects YAML produces for unquoted dates. Naive values are
    local time."""
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        raise ValueError(f"bad time {v!r}")
    if isinstance(v, int | float):
        return int(v)
    if isinstance(v, datetime):
        return int(v.timestamp())
    if isinstance(v, date):
        return int(datetime(v.year, v.month, v.day).timestamp())
    s = str(v).strip()
    if s.isdigit():
        return int(s)
    return int(datetime.fromisoformat(s).timestamp())


def _minutes(hm: str) -> int:
    h, m = hm.split(":")
    return int(h) * 60 + int(m)


def _as_list(v: Any) -> list[Any]:
    if v is None:
        return []
    if isinstance(v, list):
        return v
    return [v]


# --- models -----------------------------------------------------------------------


class QuietRange(BaseModel):
    """A local-time window in which no moves happen (timers keep running).
    `end` is exclusive; a range that ends before it starts wraps midnight."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    start: str
    end: str

    @field_validator("start", "end")
    @classmethod
    def _hm(cls, v: str) -> str:
        m = _HM_RE.match(v.strip())
        if not m:
            raise ValueError(f"bad time {v!r}: use HH:MM")
        return f"{int(m.group(1)):02d}:{m.group(2)}"

    @classmethod
    def parse(cls, text: str) -> QuietRange:
        """'23:00-06:00' -> QuietRange."""
        parts = text.split("-")
        if len(parts) != 2:
            raise ValueError(f"bad quiet range {text!r}: use 'HH:MM-HH:MM'")
        return cls(start=parts[0].strip(), end=parts[1].strip())

    def contains(self, now: datetime) -> bool:
        s, e, cur = _minutes(self.start), _minutes(self.end), now.hour * 60 + now.minute
        if s <= e:
            return s <= cur < e
        return cur >= s or cur < e


class PinRule(BaseModel):
    """A path prefix pinned to a tier. `until` (epoch) makes it expire; generated
    and runtime pins must carry one. Pins are absolute (AGENTS.md rule 9)."""

    model_config = ConfigDict(extra="forbid")

    path: str
    tier: Tier
    until: int | None = None
    source: Source = "yaml"

    @field_validator("path")
    @classmethod
    def _path(cls, v: str) -> str:
        return _norm_path(v)

    @field_validator("until", mode="before")
    @classmethod
    def _until(cls, v: Any) -> int | None:
        return _to_epoch(v)

    def active(self, now: int) -> bool:
        return self.until is None or self.until > now

    def covers(self, rel_path: str) -> bool:
        """Prefix match per path component: '/a' covers '/a/x' but not '/ab'."""
        rel = _norm_path(rel_path)
        return self.path == "/" or rel == self.path or rel.startswith(self.path + "/")


class EventRule(BaseModel):
    """policy.yaml `events:`: run an action when a signal takes a value."""

    model_config = ConfigDict(extra="forbid")

    on: str  # "<signal> == <value>"
    run: Action = "promote"
    window_h: int = 6
    arms: list[str] = Field(default_factory=lambda: ["all"])
    filter: str | None = None


class ScheduleRule(BaseModel):
    """A timer. `cron` accepts the same forms as `when_to_cron`."""

    model_config = ConfigDict(extra="forbid")

    name: str
    cron: str
    action: Action
    arms: list[str] = Field(default_factory=lambda: ["all"])
    filter: str | None = None
    enabled: bool = True
    window_h: int = 6
    source: Source = "yaml"
    until: int | None = None

    @field_validator("cron", mode="before")
    @classmethod
    def _cron(cls, v: Any) -> str:
        return when_to_cron(v)

    @field_validator("until", mode="before")
    @classmethod
    def _until(cls, v: Any) -> int | None:
        return _to_epoch(v)


class PlannerConfig(BaseModel):
    """One LLM arm: its model, which slice of history/pool it sees, its stance
    (prompt under prompts/), and optionally the only run name it takes part in."""

    model_config = ConfigDict(extra="forbid")

    model: str
    slice: str = "all"
    stance: str = "rank"
    only: str | None = None
    enabled: bool = True


class CandidateParams(BaseModel):
    """Generic candidate generation (arms/candidates.py). Durations in seconds."""

    model_config = ConfigDict(extra="forbid")

    ordinal_window: int = 3  # k: neighbours on each side within a group
    co_open_window: int = 3600  # T: opens within this many seconds count as co-opens
    lookback: int = 30 * 86400  # access history the pool carries
    cap: int = 5000  # candidate pool size

    @field_validator("co_open_window", "lookback", mode="before")
    @classmethod
    def _dur(cls, v: Any) -> int:
        return parse_duration(v)


class SequenceParams(BaseModel):
    """Association mining over (open A -> open B within `window` seconds)."""

    model_config = ConfigDict(extra="forbid")

    min_support: int = 5  # s
    min_confidence: float = 0.6  # c
    refit: str = "0 4 * * 0"  # cron
    window: int = 3600  # T, seconds
    window_h: int = 6  # proposal window

    @field_validator("refit", mode="before")
    @classmethod
    def _cron(cls, v: Any) -> str:
        return when_to_cron(v)

    @field_validator("window", mode="before")
    @classmethod
    def _dur(cls, v: Any) -> int:
        return parse_duration(v)


class ScorerParams(BaseModel):
    """Learned scorer over generic features (arms/scorer.py)."""

    model_config = ConfigDict(extra="forbid")

    min_labels: int = 500  # stays off until this many labelled proposals exist
    refit: str = "30 4 * * 0"  # cron
    threshold: float = 0.35  # propose candidates scored above this
    max_proposals: int = 200
    window_h: int = 6

    @field_validator("refit", mode="before")
    @classmethod
    def _cron(cls, v: Any) -> str:
        return when_to_cron(v)


class BanditParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prior: tuple[float, float] = (2.0, 1.0)  # Beta(alpha, beta) for a new (arm, bucket)
    decay_weekly: float = 0.95
    expectation_bonus: float = 3.0  # alpha added to the arm that met an expectation
    expectation_miss: float = 0.5  # beta added to every enabled arm in the bucket on a miss


class MqttConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str
    port: int = 1883
    username: str | None = None
    password: str | None = None
    topic_prefix: str = "ingenaning"
    tls: bool = False


class ApiConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = "0.0.0.0"  # the storage VLAN is the boundary (build doc section 9)
    port: int = 8080


def _pin_entries(raw: Any, default_source: str) -> list[Any]:
    """Accept both pin layouts: policy.yaml's {hot: [...], cold: [...]} mapping
    (items are paths or {path, until} mappings) and a flat list of {path, tier,
    until, source} mappings or PinRule objects."""
    if raw is None:
        return []
    out: list[Any] = []
    if isinstance(raw, Mapping):
        for tier, items in raw.items():
            if tier not in ("hot", "cold"):
                raise ValueError(f"pins: unknown tier {tier!r}")
            for item in _as_list(items):
                if isinstance(item, str):
                    out.append({"path": item, "tier": tier, "source": default_source})
                elif isinstance(item, Mapping):
                    if item.get("tier", tier) != tier:
                        raise ValueError(f"pins.{tier}: entry {item!r} names another tier")
                    out.append({"source": default_source, **item, "tier": tier})
                else:
                    raise ValueError(f"pins.{tier}: bad entry {item!r}")
        return out
    for item in _as_list(raw):
        if isinstance(item, Mapping):
            out.append({"source": default_source, **item})
        elif isinstance(item, PinRule):
            out.append(item)
        else:
            raise ValueError(f"pins: bad entry {item!r}; use {{path, tier, until}}")
    return out


class Settings(BaseModel):
    """Everything the daemon reads. Flat; sub-models only where a group of
    parameters belongs to one component."""

    model_config = ConfigDict(extra="forbid")

    # paths
    hot_root: Path = Path("/mnt/hot")
    cold_root: Path = Path("/mnt/cold")
    union_root: Path = Path("/srv/nas")
    socket_path: Path = Path("/run/ingenaning/access.sock")
    db_path: Path = Path("/var/lib/ingenaning/aning.db")
    state_dir: Path = Path("/var/lib/ingenaning")
    config_dir: Path = Path("/etc/ingenaning")
    intent_path: Path | None = None  # default: <union_root>/intent.md

    # budgets
    hot_floor_free: int = 400 << 30
    demote_when_used_above: float = 0.8
    max_promote_per_run: int = 200 << 30
    max_moves_per_hour: int = 50

    # pins: none by default. Only policy.yaml, the generated policy, and runtime
    # pins (UI/CLI/intent) create them.
    pins: list[PinRule] = Field(default_factory=list)

    # executor
    skip_if_opened_within: int = 600
    quiet_hours: list[QuietRange] = Field(default_factory=list)
    dry_run: bool = True

    # learners
    candidates: CandidateParams = Field(default_factory=CandidateParams)
    sequence: SequenceParams = Field(default_factory=SequenceParams)
    scorer: ScorerParams = Field(default_factory=ScorerParams)
    bandit: BanditParams = Field(default_factory=BanditParams)

    # planners
    ollama_url: str = "http://mac.oskar.co:11434"
    ollama_timeout_s: float = 60.0
    planner_token_budget: int = 12_000
    planners: dict[str, PlannerConfig] = Field(default_factory=dict)

    # signals, events, timers
    signals_primary: list[str] = Field(default_factory=list)
    signals_clock: bool = True
    events: list[EventRule] = Field(default_factory=list)
    schedules: list[ScheduleRule] = Field(default_factory=list)

    # integrations
    mqtt: MqttConfig | None = None
    emitter_tokens: list[str] = Field(default_factory=list)
    api: ApiConfig = Field(default_factory=ApiConfig)
    log_level: str = "INFO"

    @field_validator("hot_floor_free", "max_promote_per_run", mode="before")
    @classmethod
    def _size(cls, v: Any) -> int:
        return parse_size(v)

    @field_validator("demote_when_used_above", mode="before")
    @classmethod
    def _pct(cls, v: Any) -> float:
        return parse_percent(v)

    @field_validator("skip_if_opened_within", mode="before")
    @classmethod
    def _dur(cls, v: Any) -> int:
        return parse_duration(v)

    @field_validator("quiet_hours", mode="before")
    @classmethod
    def _quiet(cls, v: Any) -> list[Any]:
        return [QuietRange.parse(x) if isinstance(x, str) else x for x in _as_list(v)]

    @field_validator("pins", mode="before")
    @classmethod
    def _pins(cls, v: Any) -> list[Any]:
        return _pin_entries(v, "yaml")

    @field_validator("events", mode="before")
    @classmethod
    def _events(cls, v: Any) -> list[Any]:
        return [_event_entry(e) for e in _as_list(v)]

    # --- derived ------------------------------------------------------------------

    @property
    def intent_file(self) -> Path:
        return self.intent_path or (self.union_root / "intent.md")

    @property
    def generated_policy_path(self) -> Path:
        return self.config_dir / "policy.generated.yaml"

    @property
    def secrets_path(self) -> Path:
        return self.config_dir / "secrets.env"

    def active_pins(self, now: int) -> list[PinRule]:
        return [p for p in self.pins if p.active(now)]

    def pin_for(self, rel_path: str, now: int) -> PinRule | None:
        """The pin that decides a union-relative path: longest matching prefix,
        per path component, among pins not expired at `now`."""
        best: PinRule | None = None
        for p in self.active_pins(now):
            if p.covers(rel_path) and (best is None or len(p.path) > len(best.path)):
                best = p
        return best

    def in_quiet_hours(self, now: datetime) -> bool:
        return any(r.contains(now) for r in self.quiet_hours)

    def arm_names(self, kind: ArmKind | None = None, enabled_only: bool = True) -> list[str]:
        """The statistical arms, then the planners in policy order. Statistical arms
        are always configured; runtime toggles live in the arm_state table."""
        names: list[str] = []
        if kind in (None, "stat"):
            names.extend(STAT_ARMS)
        if kind in (None, "planner"):
            names.extend(n for n, p in self.planners.items() if p.enabled or not enabled_only)
        return names

    def arm_kind(self, name: str) -> ArmKind | None:
        if name in STAT_ARMS:
            return "stat"
        if name in self.planners:
            return "planner"
        return None


# --- loading ----------------------------------------------------------------------

_PATH_KEYS = {
    "hot": "hot_root",
    "cold": "cold_root",
    "union": "union_root",
    "socket": "socket_path",
    "db": "db_path",
    "state": "state_dir",
    "intent": "intent_path",
}
_BUDGET_KEYS = {
    k: k
    for k in (
        "hot_floor_free",
        "demote_when_used_above",
        "max_promote_per_run",
        "max_moves_per_hour",
    )
}
_EXECUTOR_KEYS = {k: k for k in ("skip_if_opened_within", "quiet_hours", "dry_run")}
_PLANNER_KEYS = {
    "ollama_url": "ollama_url",
    "timeout_s": "ollama_timeout_s",
    "token_budget": "planner_token_budget",
    "arms": "planners",
}
_SIGNAL_KEYS = {"primary": "signals_primary", "clock": "signals_clock"}
_SECTIONS = {
    "paths": _PATH_KEYS,
    "budgets": _BUDGET_KEYS,
    "executor": _EXECUTOR_KEYS,
    "planners": _PLANNER_KEYS,
    "signals": _SIGNAL_KEYS,
}
# Sections handed to Settings under their own name.
_PASSTHROUGH = ("candidates", "sequence", "scorer", "bandit", "mqtt", "api", "log_level")


def _event_entry(e: Any) -> Any:
    """YAML 1.1 (PyYAML) reads the bare key `on` as boolean True; put it back so
    `- on: presence == approaching` means what the build doc says."""
    if isinstance(e, Mapping) and True in e and "on" not in e:
        fixed = dict(e)
        fixed["on"] = fixed.pop(True)
        return fixed
    return e


def _map_section(where: str, section: str, value: Any, keys: Mapping[str, str]) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{where}: {section} must be a mapping")
    out: dict[str, Any] = {}
    for k, v in value.items():
        if k not in keys:
            raise ValueError(f"{where}: unknown key {section}.{k}")
        out[keys[k]] = v
    return out


def _require_key(entries: list[Any], key: str, where: str, what: str) -> list[Any]:
    """Every mapping entry must carry `key`. The file is hand-written, so name the
    file and the offending entry here rather than failing later, elsewhere, or only
    when another file happens to exist."""
    for e in entries:
        if isinstance(e, Mapping) and key not in e:
            raise ValueError(f"{where}: {what} entry {dict(e)!r} has no {key!r}")
    return entries


def flatten_policy(raw: Mapping[str, Any], where: str = "policy.yaml") -> dict[str, Any]:
    """Nested policy layout -> flat Settings input. Unknown keys are errors: the
    file is hand-written and a typo must not silently mean 'default'."""
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if key in _SECTIONS:
            out.update(_map_section(where, key, value, _SECTIONS[key]))
        elif key == "pins":
            out["pins"] = _require_key(_pin_entries(value, "yaml"), "path", where, "pin")
        elif key == "schedules":
            out["schedules"] = _require_key(
                [{**s, "source": "yaml"} for s in _as_list(value)], "name", where, "schedule"
            )
        elif key == "events":
            out["events"] = [_event_entry(e) for e in _as_list(value)]
        elif key in _PASSTHROUGH:
            out[key] = value
        else:
            raise ValueError(f"{where}: unknown top-level key {key!r}")
    return out


def merge_generated(data: dict[str, Any], generated: Mapping[str, Any], where: str) -> None:
    """Fold policy.generated.yaml into flattened policy data. It may only add pins
    and schedules that carry `until`; on the same path or name policy.yaml wins.
    Everything else in it is ignored with a warning.

    Each generated entry is validated here and dropped if it does not hold, so a
    file the intent arm wrote can never reach Settings and stop the daemon. The
    hand-written policy is authoritative and is never dropped: it keeps raising."""
    dropped = 0
    pins: list[Any] = list(data.get("pins") or [])
    # Entries reaching here have been through flatten_policy, which rejects a pin
    # without a path; a direct caller gets its entry skipped rather than a KeyError.
    have_paths = {
        _norm_path(str(p["path"])) for p in pins if isinstance(p, Mapping) and "path" in p
    }
    for p in _pin_entries(generated.get("pins"), "intent"):
        pin: PinRule | None = None
        if isinstance(p, Mapping):
            try:
                pin = PinRule.model_validate({**p, "source": "intent"})
            except ValidationError:
                pin = None  # bad tier, unparseable until, missing path, unknown key
        if pin is None:
            dropped += 1
            continue
        if pin.until is None:
            log.warning("%s: pin %s has no until and is ignored", where, pin.path)
            continue
        if pin.path in have_paths:
            log.info("%s: pin %s is set by policy.yaml; generated entry ignored", where, pin.path)
            continue
        pins.append(pin)
        have_paths.add(pin.path)
    data["pins"] = pins

    scheds: list[Any] = list(data.get("schedules") or [])
    have_names = {str(s["name"]) for s in scheds if isinstance(s, Mapping) and "name" in s}
    for s in _as_list(generated.get("schedules")):
        sched: ScheduleRule | None = None
        if isinstance(s, Mapping):
            try:
                sched = ScheduleRule.model_validate({**s, "source": "intent"})
            except ValidationError:
                sched = None  # missing name, bad cron or action, unparseable until
        if sched is None:
            dropped += 1
            continue
        if sched.until is None:
            log.warning("%s: schedule %s has no until and is ignored", where, sched.name)
            continue
        if sched.name in have_names:
            log.info(
                "%s: schedule %s is set by policy.yaml; generated entry ignored", where, sched.name
            )
            continue
        scheds.append(sched)
        have_names.add(sched.name)
    data["schedules"] = scheds

    for key in generated:
        if key not in ("pins", "schedules"):
            log.warning("%s: key %r is not something the generated policy may set", where, key)
    if dropped:
        # Counts only: the paths in this file are the user's, and this is a warning.
        log.warning("%s: dropped %d entries that did not validate", where, dropped)


def read_yaml(path: Path) -> dict[str, Any]:
    """A YAML mapping; an empty file is an empty mapping."""
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top level must be a mapping")
    return data


def read_env_file(path: Path) -> dict[str, str]:
    """KEY=value lines; blank lines, comments and an `export ` prefix are fine;
    surrounding quotes are stripped. Missing file -> empty."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        line = line.removeprefix("export ").strip()
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k.strip()] = v
    return out


def parse_mqtt_url(url: str) -> dict[str, Any]:
    """mqtt://user:pass@host:port/prefix -> MqttConfig fields. mqtts:// sets tls."""
    u = urlparse(url)
    if u.scheme not in ("mqtt", "mqtts"):
        raise ValueError(f"bad MQTT URL {url!r}: scheme must be mqtt or mqtts")
    if not u.hostname:
        raise ValueError(f"bad MQTT URL {url!r}: no host")
    tls = u.scheme == "mqtts"
    return {
        "host": u.hostname,
        "port": u.port or (8883 if tls else 1883),
        "username": unquote(u.username) if u.username else None,
        "password": unquote(u.password) if u.password else None,
        "topic_prefix": u.path.strip("/") or "ingenaning",
        "tls": tls,
    }


def apply_secrets(data: dict[str, Any], env: Mapping[str, str]) -> None:
    """ANING_EMITTER_TOKENS (comma list), ANING_OLLAMA_URL, ANING_MQTT_URL."""
    if tokens := env.get("ANING_EMITTER_TOKENS"):
        data["emitter_tokens"] = [t.strip() for t in tokens.split(",") if t.strip()]
    if url := env.get("ANING_OLLAMA_URL"):
        data["ollama_url"] = url
    if murl := env.get("ANING_MQTT_URL"):
        data["mqtt"] = parse_mqtt_url(murl)


def load_settings(
    policy: Path | None = None,
    generated: Path | None = None,
    secrets: Path | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> Settings:
    """Build Settings from policy.yaml (required), policy.generated.yaml and
    secrets.env (optional, next to it unless given), ANING_* process variables,
    then `overrides` (flat Settings keys, e.g. from CLI flags). Precedence rises
    in that order; the generated policy can only add, never override."""
    if policy is None:
        config_dir = Path("/etc/ingenaning")
    else:
        config_dir = policy.parent if policy.is_absolute() else policy.resolve().parent
    policy_path = policy if policy is not None else config_dir / "policy.yaml"
    generated_path = generated if generated is not None else config_dir / "policy.generated.yaml"
    secrets_path = secrets if secrets is not None else config_dir / "secrets.env"

    data = flatten_policy(read_yaml(policy_path), str(policy_path))
    data.setdefault("config_dir", config_dir)
    if generated_path.exists():
        try:
            merge_generated(data, read_yaml(generated_path), str(generated_path))
        except (yaml.YAMLError, ValueError) as exc:
            # The intent arm writes this file; a bad one must not stop the daemon.
            log.warning("%s: ignored (%s)", generated_path, exc)
    env = read_env_file(secrets_path)
    env.update({k: v for k, v in os.environ.items() if k.startswith("ANING_")})
    apply_secrets(data, env)
    if overrides:
        data.update(overrides)
    return Settings.model_validate(data)
