"""Egress sanitiser. Everything that leaves the container for a planner, a
notification, or a log line at INFO or above passes through here. Nothing
else may build outbound text (tests/test_egress.py).

Two mechanisms, applied in order:

1. Path policy per directory prefix (policy.yaml privacy.egress.paths):
     share      -> path sent as-is (default for nothing)
     basename   -> only the final component; parent replaced by a stable
                   pseudonym so sequence/sibling structure survives
     pseudonym  -> every component replaced by a stable per-deployment
                   token (HMAC of the component with a local key). The
                   planner sees structure and ordinals, never names.
     deny       -> path never leaves; candidates under it are dropped
                   before the pool is built
   Default for an unlisted prefix is `pseudonym`.

2. Pattern scrub over any free text (intent, signal text values, reasons,
   basenames that were allowed through): emails, phone numbers, IBANs,
   card numbers, national IDs, IPv4/IPv6, MACs, JWTs and long API-key-shaped
   tokens, AWS/GCP/GitHub key prefixes, URLs with credentials, and any
   line matching secret-file names (.env, id_rsa, *.pem, *.key, wallet.dat,
   *.kdbx). Matches are replaced with a class tag: <email>, <iban>, <key>.

The pseudonym key lives in secrets.env (EGRESS_KEY). Pseudonyms are stable
for a deployment so the learners and the planner's feedback block stay
consistent across runs, and are reversible only inside the container via
the `pseudonyms` table. Rotating the key invalidates planner feedback
history; do it deliberately.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass, field
from typing import Literal

Mode = Literal["share", "basename", "pseudonym", "deny"]

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # Order matters: specific shapes first, the greedy "digits with separators"
    # phone pattern last so it cannot eat IPs, cards, or IBANs.
    ("url-cred", re.compile(r"[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@")),
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    (
        "key",
        re.compile(
            r"\b(?:AKIA|ASIA|ghp_|gho_|github_pat_|xox[baprs]-|sk-[A-Za-z0-9]|AIza)[A-Za-z0-9_-]{16,}\b"
        ),
    ),
    ("key", re.compile(r"\b[A-Za-z0-9+/_-]{40,}={0,2}\b")),
    ("mac", re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b")),
    ("ipv6", re.compile(r"\b(?:[0-9a-fA-F]{1,4}:){2,7}[0-9a-fA-F]{1,4}\b")),
    ("ipv4", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    ("iban", re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){3,7}[ ]?[A-Z0-9]{1,4}\b")),
    ("card", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ("id", re.compile(r"\b\d{6}[-\s]?\d{4}\b")),
    ("phone", re.compile(r"(?<![\w/<])\+?\d[\d\s().-]{7,}\d(?![\w/>])")),
]
_SECRET_FILE = re.compile(
    r"(^|/)(\.env(\..*)?|id_(rsa|ed25519|ecdsa)(\.pub)?|[^/]*\.(pem|key|p12|pfx|kdbx|gpg|asc)|wallet\.dat|\.netrc|\.npmrc|\.pypirc)$",
    re.I,
)


@dataclass
class EgressPolicy:
    paths: dict[str, Mode] = field(default_factory=dict)  # prefix -> mode
    default: Mode = "pseudonym"
    key: bytes = b""
    names: tuple[str, ...] = ()  # people you know: names, variants, nicknames (P0, local only)

    def mode_for(self, path: str) -> Mode:
        best: tuple[int, Mode] | None = None
        for prefix, mode in self.paths.items():
            pre = prefix.rstrip("/") or "/"
            matches = path == pre or path.startswith(pre + "/") or pre == "/"
            if matches and (best is None or len(pre) > best[0]):
                best = (len(pre), mode)
        return best[1] if best else self.default


class Sanitizer:
    def __init__(self, policy: EgressPolicy):
        if not policy.key:
            raise ValueError("EGRESS_KEY is required; refusing to send unpseudonymised paths")
        self.policy = policy
        self._seen: dict[str, str] = {}
        self._names_rx = _names_regex(policy.names)

    # --- paths ---------------------------------------------------------------

    def _token(self, component: str) -> str:
        if component in self._seen:
            return self._seen[component]
        t = "p" + hmac.new(self.policy.key, component.encode(), hashlib.sha256).hexdigest()[:10]
        self._seen[component] = t
        return t

    def path(self, path: str) -> str | None:
        """Return the outbound form of a path, or None if it must not leave."""
        mode = self.policy.mode_for(path)
        if mode == "deny" or _SECRET_FILE.search(path):
            return None
        parts = [p for p in path.split("/") if p]
        if mode == "share":
            return "/" + "/".join(self._component(p) for p in parts)
        if mode == "basename":
            head = [self._token(p) for p in parts[:-1]]
            return "/" + "/".join([*head, self._component(parts[-1])])
        return "/" + "/".join(self._token(p) for p in parts)

    def _component(self, name: str) -> str:
        """Scrub a path component but keep a trailing extension intact."""
        stem, dot, ext = name.rpartition(".")
        if dot and stem and len(ext) <= 8 and ext.isalnum():
            return self.text(stem) + "." + ext
        return self.text(name)

    def pseudonyms(self) -> dict[str, str]:
        """token -> original, for the local `pseudonyms` table only."""
        return {v: k for k, v in self._seen.items()}

    # --- people --------------------------------------------------------------

    def _person(self, m: re.Match[str]) -> str:
        return "person:" + self._token("person\x00" + m.group(0).lower())

    def names(self, s: str) -> str:
        """Replace known people's names with stable pseudonyms. Case-insensitive,
        word-bounded, longest variant first, applied before pattern scrub so a
        name inside an email's local part is already gone."""
        if self._names_rx is None:
            return s
        return self._names_rx.sub(self._person, s)

    # --- free text -----------------------------------------------------------

    def text(self, s: str, max_len: int = 512) -> str:
        out = self.names(s[:max_len])
        for tag, rx in _PATTERNS:
            out = rx.sub(f"<{tag}>", out)
        return out

    def intent(self, s: str) -> str:
        return "\n".join(self.text(line, 256) for line in s.splitlines()[:200])

    def signal_value(self, name: str, value: str) -> str:
        return self.text(value, 256)


def _names_regex(names: tuple[str, ...]) -> re.Pattern[str] | None:
    variants = sorted({n.strip() for n in names if n.strip()}, key=len, reverse=True)
    if not variants:
        return None
    # word-bounded on both sides; treat _ - . as separators inside filenames
    # boundary = not adjacent to a letter/digit; underscore, dash, dot and
    # space all count as separators
    return re.compile(
        r"(?<![^\W_])(?:" + "|".join(re.escape(v) for v in variants) + r")(?![^\W_])", re.I
    )


_CAP_TOKEN = re.compile(
    r"(?<![^\W_])[A-ZÅÄÖÜ][a-zåäöüßéè]{2,}(?:[ _-][A-ZÅÄÖÜ][a-zåäöüßéè]{2,})?(?![^\W_])"
)


def suggest_names(
    texts: list[str], known: tuple[str, ...], ignore: tuple[str, ...] = ()
) -> list[tuple[str, int]]:
    """Capitalised tokens that look like names and are not yet known or
    ignored, with counts. For the UI's "add to people?" list. Heuristic only."""
    kn = {k.lower() for k in known} | {i.lower() for i in ignore}
    counts: dict[str, int] = {}
    for t in texts:
        for m in _CAP_TOKEN.finditer(t):
            tok = m.group(0)
            if tok.lower() in kn:
                continue
            counts[tok] = counts.get(tok, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def scrub_report(s: str) -> dict[str, int]:
    """What the scrub would replace, by class. For the UI preview."""
    return {tag: len(rx.findall(s)) for tag, rx in _PATTERNS if rx.search(s)}


# The `people` list is loaded from the local `people` table (name, variant,
# added, source) — never from policy.yaml, never exported, purged with P0.
