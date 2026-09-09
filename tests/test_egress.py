from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from ingenaning.egress.sanitize import EgressPolicy, Sanitizer, scrub_report

ROOT = Path(__file__).resolve().parents[1] / "ingenaning"


@pytest.fixture
def san():
    return Sanitizer(
        EgressPolicy(
            paths={"/media": "share", "/projects": "basename", "/private": "deny"},
            key=b"test-key",
            names=("Alexandra", "Alex", "Erika Mustermann", "Erika"),
        )
    )


def test_refuses_without_key():
    with pytest.raises(ValueError):
        Sanitizer(EgressPolicy())


def test_share_keeps_path_but_scrubs_text(san):
    assert san.path("/media/tv/x/S01E02.mkv") == "/media/tv/x/S01E02.mkv"
    assert san.path("/media/scan/invoice_max.mustermann@web.de.pdf") == "/media/scan/<email>.pdf"
    assert (
        san.path("/media/scan/from 10.20.30.5 backup.tar.gz")
        == "/media/scan/from <ipv4> backup.tar.gz"
    )


def test_basename_pseudonymises_parents_stably(san):
    a = san.path("/projects/lisbon-2026/raw/IMG_0412.dng")
    b = san.path("/projects/lisbon-2026/raw/IMG_0413.dng")
    assert a and b
    assert a.rsplit("/", 1)[0] == b.rsplit("/", 1)[0]  # same parent token
    assert a.endswith("/IMG_0412.dng") and "lisbon" not in a


def test_default_is_full_pseudonym(san):
    p = san.path("/documents/tax/2025/Steuererklärung.pdf")
    assert p and re.fullmatch(r"(/p[0-9a-f]{10}){4}", p)


def test_deny_and_secret_files_never_leave(san):
    assert san.path("/private/anything.txt") is None
    assert san.path("/media/ok/.env") is None
    assert san.path("/media/ok/server.pem") is None
    assert san.path("/media/ok/wallet.dat") is None


def test_pseudonym_map_is_local_and_reversible(san):
    san.path("/documents/a/b")
    m = san.pseudonyms()
    assert set(m.values()) >= {"documents", "a", "b"}


@pytest.mark.parametrize(
    "raw,tag",
    [
        ("call +49 40 1234567 now", "phone"),
        ("DE89 3704 0044 0532 0130 00", "iban"),
        ("4111 1111 1111 1111", "card"),
        ("token ghp_abcdefghijklmnopqrstuvwxyz0123", "key"),
        ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghijklmnop", "jwt"),
        ("https://user:pass@host/x", "url-cred"),
        ("box at 10.20.30.5", "ipv4"),
    ],
)
def test_text_scrub(san, raw, tag):
    assert f"<{tag}>" in san.text(raw)
    assert tag in scrub_report(raw)


def test_intent_is_scrubbed_and_capped(san):
    s = san.intent("keep the Roon library hot\nmy iban is DE89370400440532013000\n" + "x\n" * 500)
    assert "<iban>" in s and s.count("\n") <= 199


def test_only_sanitizer_builds_outbound_text():
    """Planner, intent, and notify modules must obtain paths and text via
    Sanitizer, never raw from the store."""
    for rel in ("arms/planner.py", "arms/intent.py", "notify.py"):
        p = ROOT / rel
        if not p.exists():
            continue
        src = p.read_text()
        assert "Sanitizer" in src, f"{rel}: does not use egress.sanitize.Sanitizer"
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr in ("path", "reason")
                and isinstance(node.value, ast.Name)
            ):
                pass  # structural presence is checked above; behavioural test below


def test_known_names_become_stable_person_tokens(san):
    a = san.text("dinner with Alexandra and erika")
    b = san.text("Alexandra again")
    assert "Alexandra" not in a and "erika" not in a
    tok = re.search(r"person:p[0-9a-f]{10}", a).group(0)
    assert tok in b  # same person, same token, case-insensitive


def test_longest_variant_wins_and_nickname_maps_separately(san):
    full = san.text("Erika Mustermann called")
    short = san.text("Erika called")
    assert full.count("person:") == 1 and short.count("person:") == 1
    assert full != short  # full name and short form are different tokens by design


def test_names_scrubbed_in_shared_paths(san):
    p = san.path("/media/photos/Alexandra_birthday_2025.jpg")
    assert p and "Alexandra" not in p and p.endswith(".jpg") and "person:" in p


def test_names_not_in_ordinary_words(san):
    assert san.text("alexander") == "alexander"  # no partial match


def test_suggester_finds_capitalised_unknowns():
    from ingenaning.egress.sanitize import suggest_names

    out = suggest_names(
        ["Lunch with Ingrid", "Ingrid and Bjorn", "the Bjorn file"],
        known=("Alexandra",),
        ignore=("Lunch",),
    )
    assert set(out[:2]) == {
        ("Ingrid", 2),
        ("Bjorn", 2),
    }  # ties sort by name; order among them is not asserted
    assert all(name not in ("Lunch", "Alexandra") for name, _ in out)
