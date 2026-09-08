"""config.py: flat Settings, loaded from the nested policy.yaml layout."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from ingenaning.config import (
    QuietRange,
    Settings,
    load_settings,
    parse_duration,
    parse_percent,
    parse_size,
    when_to_cron,
)

EXAMPLE = Path(__file__).resolve().parents[1] / "deploy" / "policy.example.yaml"


def test_parsers():
    assert parse_size("400G") == 400 << 30 and parse_size("1.5M") == int(1.5 * (1 << 20))
    assert parse_size(123) == 123 and parse_size("2TiB") == 2 << 40
    assert (
        parse_duration("10m") == 600
        and parse_duration("1h") == 3600
        and parse_duration("30d") == 30 * 86400
    )
    assert parse_duration(45) == 45
    assert (
        parse_percent("80%") == 0.8 and parse_percent(0.5) == 0.5 and parse_percent("0.25") == 0.25
    )
    for bad in ("", "x", "10q"):
        with pytest.raises(ValueError):
            parse_size(bad)
    with pytest.raises(ValueError):
        parse_duration("5y")


def test_when_to_cron():
    assert when_to_cron("Sun 04:00") == "0 4 * * 0"
    assert when_to_cron("mon 23:15") == "15 23 * * 1"
    assert when_to_cron("03:30") == "30 3 * * *"
    assert when_to_cron("*/15 * * * *") == "*/15 * * * *"
    with pytest.raises(ValueError):
        when_to_cron("sometime")


def test_defaults_have_no_pins():
    s = Settings()
    assert s.pins == []
    assert s.pin_for("/models/x", now=0) is None
    assert s.dry_run is True
    assert s.hot_root == Path("/mnt/hot") and s.socket_path == Path("/run/ingenaning/access.sock")


def test_empty_policy_file_means_no_pins(tmp_path: Path):
    p = tmp_path / "policy.yaml"
    p.write_text("")
    s = load_settings(policy=p)
    assert s.pins == [] and s.events == [] and s.schedules == []
    assert s.config_dir == tmp_path


def test_loads_example_policy():
    s = load_settings(policy=EXAMPLE)
    assert s.hot_root == Path("/mnt/hot") and s.cold_root == Path("/mnt/cold")
    assert s.union_root == Path("/srv/nas") and s.socket_path == Path("/run/ingenaning/access.sock")
    assert s.hot_floor_free == 400 << 30 and s.max_promote_per_run == 200 << 30
    assert s.demote_when_used_above == 0.8 and s.max_moves_per_hour == 50
    assert {(p.path, p.tier) for p in s.pins} == {
        ("/vm-disks", "hot"),
        ("/models", "hot"),
        ("/scratch", "hot"),
        ("/plex-transcode", "hot"),
        ("/archive", "cold"),
    }
    assert all(p.source == "yaml" for p in s.pins)
    assert s.skip_if_opened_within == 600 and s.dry_run is True
    assert s.quiet_hours == [QuietRange(start="23:00", end="06:00")]
    assert (
        s.candidates.ordinal_window,
        s.candidates.co_open_window,
        s.candidates.lookback,
        s.candidates.cap,
    ) == (3, 3600, 30 * 86400, 5000)
    assert (s.sequence.min_support, s.sequence.min_confidence, s.sequence.refit) == (
        5,
        0.6,
        "0 4 * * 0",
    )
    assert (s.scorer.min_labels, s.scorer.refit) == (500, "30 4 * * 0")
    assert s.bandit.prior == (2.0, 1.0) and s.bandit.decay_weekly == 0.95
    assert s.bandit.expectation_bonus == 3.0 and s.bandit.expectation_miss == 0.5
    assert s.ollama_url == "http://mac.oskar.co:11434" and s.ollama_timeout_s == 60
    assert (
        s.planners["p-media"].model == "qwen2.5:14b-instruct"
        and s.planners["p-media"].slice == "history"
    )
    assert (
        s.planners["p-all"].only == "nightly"
        and s.planners["p-conservative"].stance == "conservative"
    )
    assert s.signals_primary == ["presence"] and s.signals_clock is True
    assert [e.on for e in s.events] == ["presence == approaching", "workstation == on"]
    assert (
        s.events[0].run == "promote"
        and s.events[0].window_h == 6
        and s.events[0].arms == ["p-context", "p-media", "sequence", "scorer"]
    )
    assert [(x.name, x.action) for x in s.schedules] == [
        ("stats", "promote"),
        ("nightly", "promote"),
        ("demote", "demote"),
    ]
    assert s.schedules[2].arms == ["all"] and s.schedules[0].cron == "*/15 * * * *"
    assert s.arm_names() == [
        "sequence",
        "scorer",
        "p-media",
        "p-project",
        "p-context",
        "p-all",
        "p-conservative",
    ]
    assert s.arm_names(kind="planner") == [
        "p-media",
        "p-project",
        "p-context",
        "p-all",
        "p-conservative",
    ]


def test_secrets_env_and_overrides(tmp_path: Path, monkeypatch):
    (tmp_path / "policy.yaml").write_text("paths: {hot: /h}\n")
    (tmp_path / "secrets.env").write_text(
        'ANING_EMITTER_TOKENS="tok1, tok2"\nANING_OLLAMA_URL=http://o:1\n'
        "ANING_MQTT_URL=mqtt://u:p@broker:1884/prefix\n# comment\n"
    )
    monkeypatch.delenv("ANING_OLLAMA_URL", raising=False)
    s = load_settings(policy=tmp_path / "policy.yaml", overrides={"dry_run": False})
    assert s.hot_root == Path("/h") and s.dry_run is False
    assert s.emitter_tokens == ["tok1", "tok2"] and s.ollama_url == "http://o:1"
    assert s.mqtt and (s.mqtt.host, s.mqtt.port, s.mqtt.username, s.mqtt.topic_prefix) == (
        "broker",
        1884,
        "u",
        "prefix",
    )
    monkeypatch.setenv("ANING_OLLAMA_URL", "http://env:2")
    assert load_settings(policy=tmp_path / "policy.yaml").ollama_url == "http://env:2"


def test_generated_policy_adds_only_expiring_entries(tmp_path: Path):
    (tmp_path / "policy.yaml").write_text(
        "pins: {hot: [/keep]}\nschedules:\n  - {name: n, cron: '0 3 * * *', action: promote}\n"
    )
    (tmp_path / "policy.generated.yaml").write_text(
        "pins:\n  - {path: /keep, tier: cold, until: '2099-01-01'}\n"
        "  - {path: /tmpwarm, tier: hot, until: '2099-01-01'}\n"
        "  - {path: /noexpiry, tier: hot}\n"
        "schedules:\n  - {name: n, cron: '0 5 * * *', action: promote, until: '2099-01-01'}\n"
        "  - {name: extra, cron: '0 5 * * *', action: promote, until: '2099-01-01'}\n"
    )
    s = load_settings(policy=tmp_path / "policy.yaml")
    pins = {p.path: p for p in s.pins}
    assert pins["/keep"].tier == "hot" and pins["/keep"].source == "yaml"  # yaml wins
    assert pins["/tmpwarm"].source == "intent" and pins["/tmpwarm"].until is not None
    assert "/noexpiry" not in pins
    assert [(x.name, x.cron, x.source) for x in s.schedules] == [
        ("n", "0 3 * * *", "yaml"),
        ("extra", "0 5 * * *", "intent"),
    ]


def test_pin_for_longest_prefix_and_expiry():
    s = Settings.model_validate(
        {
            "pins": [
                {"path": "/a", "tier": "cold"},
                {"path": "/a/b", "tier": "hot"},
                {"path": "/gone", "tier": "hot", "until": 100},
            ]
        }
    )
    assert s.pin_for("/a/x", now=0).tier == "cold"
    assert s.pin_for("/a/b/x", now=0).tier == "hot"
    assert s.pin_for("/a/bc", now=0).tier == "cold"  # prefix match is per path component
    assert s.pin_for("/gone/x", now=50).tier == "hot" and s.pin_for("/gone/x", now=150) is None
    assert s.pin_for("/other", now=0) is None


def test_quiet_range_wraps_midnight():
    r = QuietRange(start="23:00", end="06:00")
    assert r.contains(datetime(2026, 1, 1, 23, 30)) and r.contains(datetime(2026, 1, 1, 2, 0))
    assert not r.contains(datetime(2026, 1, 1, 12, 0)) and not r.contains(
        datetime(2026, 1, 1, 6, 0)
    )
    assert QuietRange(start="09:00", end="17:00").contains(datetime(2026, 1, 1, 12, 0))
    assert QuietRange.parse("23:00-06:00") == r
