"""config.py: flat Settings, loaded from the nested policy.yaml layout."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import pytest

from ingenaning import config
from ingenaning.config import (
    QuietRange,
    Settings,
    egress_key,
    load_settings,
    parse_duration,
    parse_percent,
    parse_size,
    when_to_cron,
)

EXAMPLE = Path(__file__).resolve().parents[1] / "deploy" / "policy.example.yaml"


@pytest.fixture(autouse=True)
def _no_inherited_aning_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """load_settings merges every ANING_* process variable over secrets.env, so a
    variable in the developer's or the runner's shell decides what these tests see.
    Clear them all; a test that needs one sets it itself, after this. EGRESS_KEY is
    read straight from the environment by egress_key(), so it goes too.

    The planner URL check resolves a host name that is not an address literal.
    These tests run offline, so `_resolve` — the one DNS path in config.py — is
    replaced for the whole module; what it returns is exercised in
    tests/test_privacy.py."""
    for key in [k for k in os.environ if k.startswith("ANING_")]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("EGRESS_KEY", raising=False)
    monkeypatch.setattr(config, "_resolve", lambda host: ["10.20.30.5"])


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
    assert s.planners.ollama.url == "https://mac.oskar.co:443"
    assert s.planners.ollama.timeout_s == 60
    assert s.planners.ollama.ca_file == Path("/etc/ingenaning/ollama-ca.pem")
    assert s.planners.token_budget == 12_000  # not in the file; the default stands
    assert (
        s.planners.arms["p-media"].model == "qwen2.5:14b-instruct"
        and s.planners.arms["p-media"].slice == "history"
    )
    assert (
        s.planners.arms["p-all"].only == "nightly"
        and s.planners.arms["p-conservative"].stance == "conservative"
    )
    assert (s.retention.access, s.retention.signals, s.retention.context) == (
        90 * 86400,
        90 * 86400,
        90 * 86400,
    )
    assert (s.retention.proposals, s.retention.outcomes, s.retention.prompt_log) == (
        180 * 86400,
        180 * 86400,
        90 * 86400,
    )
    assert s.retention.purge_job == "15 4 * * *"
    assert s.egress.default == "pseudonym"
    assert s.egress.paths["/projects"] == "basename" and s.egress.paths["/vm-disks"] == "deny"
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
        'ANING_EMITTER_TOKENS="tok1, tok2"\nANING_OLLAMA_URL=https://10.0.0.1:1\n'
        "ANING_MQTT_URL=mqtt://u:p@broker:1884/prefix\n# comment\n"
    )
    s = load_settings(policy=tmp_path / "policy.yaml", overrides={"dry_run": False})
    assert s.hot_root == Path("/h") and s.dry_run is False
    assert s.emitter_tokens == ["tok1", "tok2"]
    assert s.planners.ollama.url == "https://10.0.0.1:1"
    assert s.mqtt and (s.mqtt.host, s.mqtt.port, s.mqtt.username, s.mqtt.topic_prefix) == (
        "broker",
        1884,
        "u",
        "prefix",
    )
    monkeypatch.setenv("ANING_OLLAMA_URL", "https://10.0.0.2:2")
    s = load_settings(policy=tmp_path / "policy.yaml")
    assert s.planners.ollama.url == "https://10.0.0.2:2"


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


def test_bad_generated_entries_are_dropped_and_yaml_survives(tmp_path: Path, caplog):
    """A malformed policy.generated.yaml must never stop the daemon: the entries
    that do not validate are dropped, the rest of the file and policy.yaml stand."""
    (tmp_path / "policy.yaml").write_text("pins: {hot: [/keep]}\n")
    (tmp_path / "policy.generated.yaml").write_text(
        "pins:\n"
        "  - {path: /warmish, tier: warm, until: '2099-01-01'}\n"
        "  - {path: /undated, tier: hot, until: 'the day after tomorrow'}\n"
        "  - {path: /good, tier: hot, until: '2099-01-01'}\n"
        "schedules:\n"
        "  - {name: bad, cron: sometime, action: promote, until: '2099-01-01'}\n"
        "  - {name: fine, cron: '0 5 * * *', action: promote, until: '2099-01-01'}\n"
    )
    with caplog.at_level("WARNING"):
        s = load_settings(policy=tmp_path / "policy.yaml")
    assert {(p.path, p.tier, p.source) for p in s.pins} == {
        ("/keep", "hot", "yaml"),
        ("/good", "hot", "intent"),
    }
    assert [(x.name, x.source) for x in s.schedules] == [("fine", "intent")]
    assert "/warmish" not in caplog.text  # counts, not paths


def test_ignored_generated_entries_are_counted_without_naming_them(tmp_path: Path, caplog):
    """PRIVACY.md: WARNING and above carry how many entries were ignored, never
    which. The paths and names live at DEBUG."""
    (tmp_path / "policy.yaml").write_text("pins: {hot: [/keep]}\n")
    (tmp_path / "policy.generated.yaml").write_text(
        "pins:\n"
        "  - {path: /noexpiry, tier: hot}\n"
        "  - {path: /keep, tier: cold, until: '2099-01-01'}\n"
    )
    with caplog.at_level("WARNING"):
        s = load_settings(policy=tmp_path / "policy.yaml")
    assert {(p.path, p.source) for p in s.pins} == {("/keep", "yaml")}
    assert "1 ignored for having no until" in caplog.text
    assert "1 already set by policy.yaml" in caplog.text
    for name in ("/noexpiry", "/keep"):
        assert name not in caplog.text
    caplog.clear()
    with caplog.at_level("DEBUG"):
        load_settings(policy=tmp_path / "policy.yaml")
    assert "/noexpiry" in caplog.text  # the detail is kept, just not at WARNING


def test_bad_hand_written_policy_still_raises(tmp_path: Path):
    """policy.yaml is authoritative: a typo there must not silently mean 'default'."""
    (tmp_path / "policy.yaml").write_text("pins:\n  - {path: /warmish, tier: warm}\n")
    with pytest.raises(ValueError):
        load_settings(policy=tmp_path / "policy.yaml")


@pytest.mark.parametrize("generated", [False, True])
def test_broken_policy_entry_names_the_file_and_the_entry(tmp_path: Path, generated: bool):
    """A pin without a path or a schedule without a name is a typo in the
    hand-written file. The error must say which file and which entry, and must not
    depend on whether policy.generated.yaml happens to exist."""
    if generated:
        (tmp_path / "policy.generated.yaml").write_text(
            "pins:\n  - {path: /x, tier: hot, until: '2099-01-01'}\n"
        )
    policy = tmp_path / "policy.yaml"
    policy.write_text("pins:\n  - {tier: hot}\n")
    with pytest.raises(ValueError, match=r"policy\.yaml: pin entry .* has no 'path'"):
        load_settings(policy=policy)
    policy.write_text("schedules:\n  - {cron: '0 3 * * *', action: promote}\n")
    with pytest.raises(ValueError, match=r"policy\.yaml: schedule entry .* has no 'name'"):
        load_settings(policy=policy)


def test_process_env_beats_secrets_file_and_overrides_win_last(tmp_path: Path, monkeypatch):
    """D-013 precedence, kept covered now that the environment is cleared for every
    test in this file: secrets.env < ANING_* process variables < explicit overrides."""
    (tmp_path / "policy.yaml").write_text("paths: {hot: /h}\n")
    (tmp_path / "secrets.env").write_text(
        "ANING_OLLAMA_URL=https://10.0.0.1:1\nANING_EMITTER_TOKENS=filetok\n"
    )
    monkeypatch.setenv("ANING_OLLAMA_URL", "https://10.0.0.2:2")
    s = load_settings(policy=tmp_path / "policy.yaml")
    assert s.planners.ollama.url == "https://10.0.0.2:2"  # the variable beats the file
    assert s.emitter_tokens == ["filetok"]  # the file stands where no variable is set
    s = load_settings(
        policy=tmp_path / "policy.yaml",
        overrides={"planners": {"ollama": {"url": "https://10.0.0.3:3"}}},
    )
    assert s.planners.ollama.url == "https://10.0.0.3:3"  # an explicit override wins last


def test_privacy_block_maps_onto_retention_and_egress(tmp_path: Path):
    """The file nests; Settings does not. `privacy.purge_job` lands on
    RetentionParams beside the horizons it drives, and an HH:MM time becomes cron
    like every other timer in this file."""
    (tmp_path / "policy.yaml").write_text(
        "privacy:\n"
        "  retention: {access: 7d, prompt_log: 3600}\n"
        '  purge_job: "04:15"\n'
        "  egress:\n"
        "    default: deny\n"
        "    paths: {'/a/': share, /b: basename}\n"
    )
    s = load_settings(policy=tmp_path / "policy.yaml")
    assert s.retention.access == 7 * 86400 and s.retention.prompt_log == 3600
    assert s.retention.signals == 90 * 86400  # untouched horizons keep their default
    assert s.retention.purge_job == "15 4 * * *"
    assert s.egress.default == "deny"
    assert s.egress.paths == {"/a": "share", "/b": "basename"}  # keys are normalised


def test_egress_config_is_handed_to_the_sanitiser_with_a_key(tmp_path: Path):
    """config.py never imports the egress package; it produces the arguments the
    sanitiser's EgressPolicy takes, with the key supplied separately."""
    s = Settings.model_validate({"egress": {"default": "basename", "paths": {"/a": "deny"}}})
    assert s.egress.policy_kwargs("k") == {
        "paths": {"/a": "deny"},
        "default": "basename",
        "key": "k",
    }


@pytest.mark.parametrize(
    "text",
    [
        "privacy: {retention: {access: 7d, unknown: 1}}",
        "privacy: {retention: {access: never}}",
        "privacy: {purge_job: sometime}",
        "privacy: {egress: {default: sometimes}}",
        "privacy: {egress: {paths: {/a: sometimes}}}",
        "privacy: {typo: 1}",
    ],
)
def test_bad_privacy_block_stops_the_daemon(tmp_path: Path, text: str):
    """policy.yaml is hand-written: a typo in the privacy block must not silently
    mean 'default retention' or 'default egress mode'."""
    (tmp_path / "policy.yaml").write_text(text + "\n")
    with pytest.raises(ValueError):
        load_settings(policy=tmp_path / "policy.yaml")


def test_ollama_url_from_secrets_keeps_the_rest_of_the_planner_block(tmp_path: Path):
    (tmp_path / "policy.yaml").write_text(
        "planners:\n"
        "  ollama: {url: 'https://10.1.1.1:443', ca_file: /etc/ingenaning/ca.pem}\n"
        "  arms: {a1: {model: m}}\n"
    )
    (tmp_path / "secrets.env").write_text("ANING_OLLAMA_URL=https://10.0.0.9:443\n")
    s = load_settings(policy=tmp_path / "policy.yaml")
    assert s.planners.ollama.url == "https://10.0.0.9:443"
    assert s.planners.ollama.ca_file == Path("/etc/ingenaning/ca.pem")
    assert s.arm_names(kind="planner") == ["a1"]


def test_egress_key_comes_from_the_environment_or_secrets_env(tmp_path: Path, monkeypatch):
    """AGENTS.md rule 14. The key is a secret: it is never a Settings field, so it
    is read at daemon start, not at config load."""
    s = Settings(config_dir=tmp_path)
    with pytest.raises(RuntimeError, match="EGRESS_KEY"):
        egress_key(s)
    (tmp_path / "secrets.env").write_text("EGRESS_KEY=fromfile\n")
    assert egress_key(s) == "fromfile"
    monkeypatch.setenv("EGRESS_KEY", "fromenv")
    assert egress_key(s) == "fromenv"


def test_settings_never_carries_the_egress_key(tmp_path: Path):
    (tmp_path / "policy.yaml").write_text("privacy: {egress: {default: deny}}\n")
    (tmp_path / "secrets.env").write_text("EGRESS_KEY=sekrit\n")
    s = load_settings(policy=tmp_path / "policy.yaml")
    assert "sekrit" not in repr(s) and "EGRESS_KEY" not in repr(s)
