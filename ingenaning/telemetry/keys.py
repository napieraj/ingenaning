"""Neighbourhood keys for a path. Generic by decision (D-003): the core's domain
is files, paths, sizes, times and order, so there is nothing here about what a
file *is* — only where it sits and what comes next to it.

`group_key` is the file's parent directory. `ordinal` is the file's position in
the natural sort of its group, so `b2` comes before `b10` where a plain string
sort would not. Both land in the `files` columns of the same name (D-007a) and
are the only structure `arms/candidates.py` gets to work with."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable

_DIGITS = re.compile(r"\d+")


def norm(path: str) -> str:
    """One leading slash, no doubled or trailing slashes. Same rule as
    `store.queries.norm_path`, restated here so this module stays free of
    imports and can be used while a row is being built."""
    return "/" + "/".join(part for part in path.split("/") if part)


def group_key(path: str) -> str:
    """The parent directory of `path`. A file at the root gets '/'."""
    p = norm(path)
    parent = p.rsplit("/", 1)[0]
    return parent or "/"


def natural_key(name: str) -> tuple[tuple[int, int, str], ...]:
    """Sort key that reads digit runs as numbers: b2 < b10, and b1 < b2 < b10.

    Each element is (kind, number, text) so numbers and text never compare
    against each other; numbers sort first. The whole name is appended as a
    final text element so names that differ only in zero padding ('b02' and
    'b2') still get a stable, total order."""
    out: list[tuple[int, int, str]] = []
    pos = 0
    for m in _DIGITS.finditer(name):
        if m.start() > pos:
            out.append((1, 0, name[pos : m.start()].casefold()))
        out.append((0, int(m.group()), ""))
        pos = m.end()
    if pos < len(name):
        out.append((1, 0, name[pos:].casefold()))
    out.append((1, 0, name))
    return tuple(out)


def _sorted_group(paths: Iterable[str]) -> list[str]:
    return sorted({norm(p) for p in paths}, key=lambda p: natural_key(p.rsplit("/", 1)[-1]))


def ordinal(path: str, group: Iterable[str]) -> int | None:
    """Zero-based position of `path` in the natural sort of `group`, or None when
    `path` is not one of them. `group` is the set of paths sharing its
    `group_key`; the caller supplies it because one path alone cannot know its
    neighbours."""
    p = norm(path)
    members = _sorted_group(group)
    try:
        return members.index(p)
    except ValueError:
        return None


def assign_ordinals(paths: Iterable[str]) -> dict[str, int]:
    """Ordinals for many paths at once: group by `group_key`, then number each
    group from 0 in natural order. This is what the scanner uses — it has the
    whole walk in hand, so every group is numbered in one pass instead of
    re-sorting a group per file."""
    groups: dict[str, list[str]] = defaultdict(list)
    for path in paths:
        p = norm(path)
        groups[group_key(p)].append(p)
    out: dict[str, int] = {}
    for members in groups.values():
        for i, p in enumerate(_sorted_group(members)):
            out[p] = i
    return out
