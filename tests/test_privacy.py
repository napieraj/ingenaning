"""PRIVACY.md and SECURITY.md rules that can be checked mechanically."""

from __future__ import annotations

import ast
import ipaddress
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "ingenaning"
OUTBOUND = re.compile(r"\b(requests|urllib\.request|aiohttp|socket\.create_connection)\b")


def _py(exclude: tuple[str, ...] = ()) -> list[Path]:
    return [p for p in ROOT.rglob("*.py") if p.name not in exclude]


def test_only_httpx_and_mqtt_do_network():
    """The daemon's only outbound clients are httpx (Ollama) and paho (broker)."""
    for p in _py(exclude=("relay.py",)):
        src = p.read_text()
        assert not OUTBOUND.search(src), f"{p}: unexpected network client"


def test_httpx_only_used_in_planner_and_intent():
    for p in _py():
        if "httpx" in p.read_text():
            assert (
                p.parts[-2:] in (("arms", "planner.py"), ("arms", "intent.py"))
                or p.name == "config.py"
            ), f"{p}: httpx used outside planner/intent"


def test_no_shell_true():
    for p in _py():
        assert "shell=True" not in p.read_text(), f"{p}: shell=True"


def test_no_hardcoded_public_hosts():
    url_re = re.compile(r"https?://([^/:\"']+)")
    for p in _py():
        for m in url_re.finditer(p.read_text()):
            host = m.group(1)
            if host in ("127.0.0.1", "localhost") or host.endswith((".local", ".oskar.co")):
                continue
            try:
                ip = ipaddress.ip_address(host)
                assert ip.is_private, f"{p}: public IP literal {host}"
            except ValueError:
                # a hostname: only allowed in docstrings/comments as documentation
                line = p.read_text()[: m.start()].rsplit("\n", 1)[-1]
                assert line.lstrip().startswith(("#", '"', "'")) or "Args" in line, (
                    f"{p}: hostname {host} outside a comment"
                )


def test_prompts_do_not_profile_the_person():
    banned = re.compile(
        r"personality|mood|health|relationship|describe (this|the) (person|user)", re.I
    )
    for p in (ROOT / "prompts").glob("*.md"):
        assert not banned.search(p.read_text()), f"{p}: prompt asks the model to profile the user"


PATH_WORDS = frozenset({"path", "rel", "src", "dst"})


def _path_words(call: ast.Call) -> set[str]:
    """Every word a log call could be naming a user path by.

    Bare names alone are not enough: collecting only `ast.Name` ids reported NONE
    on 2026-09-09 while four lines were naming user paths at WARNING, because the
    Name in `pin.path` is `pin`. So also take the attribute of an `ast.Attribute`,
    the string key of an `ast.Subscript`, and everything interpolated into an
    f-string.
    """
    words: set[str] = set()
    for n in ast.walk(call):
        if isinstance(n, ast.Name):
            words.add(n.id)
        elif isinstance(n, ast.Attribute):
            words.add(n.attr)
        elif isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant):
            if isinstance(n.slice.value, str):
                words.add(n.slice.value)
        elif isinstance(n, ast.FormattedValue):
            for m in ast.walk(n.value):
                if isinstance(m, ast.Name):
                    words.add(m.id)
                elif isinstance(m, ast.Attribute):
                    words.add(m.attr)
    return words


def test_no_print_of_paths_at_info():
    """INFO-level log calls must not include a path variable."""
    for p in _py(exclude=("relay.py",)):
        tree = ast.parse(p.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("info", "warning", "error")
                and getattr(node.func.value, "id", "") == "log"
            ):
                assert not (PATH_WORDS & _path_words(node)), (
                    f"{p}:{node.lineno}: path in {node.func.attr}() log; use debug()"
                )


@pytest.mark.parametrize("host", ["10.20.30.5", "192.168.1.4", "mac.oskar.co"])
def test_config_accepts_private_https_ollama(host, monkeypatch):
    from ingenaning import config

    monkeypatch.setattr(config, "_resolve", lambda h: ["10.20.30.5"])
    config.Settings.model_validate({"planners": {"ollama": {"url": f"https://{host}:443"}}})


@pytest.mark.parametrize(
    "url",
    [
        "http://10.20.30.5:11434",  # plaintext
        "https://api.openai.com",  # public host
        "https://8.8.8.8",  # public IP
    ],
)
def test_config_rejects_insecure_or_public_ollama(url, monkeypatch):
    from ingenaning import config

    monkeypatch.setattr(config, "_resolve", lambda h: ["8.8.8.8"])
    with pytest.raises(ValueError):
        config.Settings.model_validate({"planners": {"ollama": {"url": url}}})
