"""Space allocation across arms for one run (build doc section 6).

    consensus = paths proposed by more than one arm get their priority raised
    for arm in descending Thompson sample:
        budget = base * sample / sum(samples)
        take that arm's proposals in rank order until the budget is spent

`base` is min(free, max_promote_per_run) and the sample sum runs over the arms
that actually proposed something, so an arm that ran and stayed silent does not
hold back space, and the budgets can never add up to more than the run cap.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Protocol

log = logging.getLogger(__name__)

TAKEN = "taken by another arm"
OVER_BUDGET = "budget"
OVER_RUN_CAP = "run cap"


class Proposal(Protocol):
    """What the allocator needs of arms.base.Proposal: it reads size and
    confidence, ranks by the priority it writes back, and dedupes on path."""

    arm: str
    path: str
    size: int
    confidence: float
    priority: float


@dataclass(slots=True)
class Allocation:
    """One run's decision: what fits, what did not, and the budgets behind it."""

    accepted: list[Proposal] = field(default_factory=list)
    rejected: list[tuple[Proposal, str]] = field(default_factory=list)
    budgets: dict[str, int] = field(default_factory=dict)
    samples: dict[str, float] = field(default_factory=dict)

    @property
    def bytes(self) -> int:
        return sum(p.size for p in self.accepted)


def _apply_consensus(proposals: dict[str, list[Proposal]]) -> None:
    """Raise the priority of paths several arms agree on, in place.

    Section 6 writes the multiplier as (1 + 0.5*n) for n backers, but that is
    discontinuous at n=1: a path only one arm proposed would have its confidence
    inflated by half for no agreement at all. The multiplier used here is
    (1 + 0.5*(n - 1)), which leaves a solo proposal's confidence untouched and
    still pays half a step per additional backer.
    """
    backers: dict[str, set[str]] = defaultdict(set)
    for arm, props in proposals.items():
        for p in props:
            backers[p.path].add(arm)
    for props in proposals.values():
        for p in props:
            n = len(backers[p.path])
            p.priority = p.confidence * (1.0 + 0.5 * (n - 1))


def allocate(
    proposals: dict[str, list[Proposal]],
    samples: dict[str, float],
    free: int,
    max_promote: int,
    arm_caps: dict[str, int | None] | None = None,
) -> Allocation:
    """Split the run's space between the arms and pick what to promote.

    `free` is the hot branch's free space and `max_promote` the run cap; a
    negative `free` behaves like zero. `arm_caps` bounds an arm's budget from
    above and never raises it; an entry for an arm that did not propose is
    ignored. Proposals are ranked on a copy, so the caller's lists keep their
    order, and the accepted list holds the caller's own objects.
    """
    caps = arm_caps or {}
    base = max(0, min(free, max_promote))
    alloc = Allocation(samples=dict(samples))

    _apply_consensus(proposals)

    active = [arm for arm, props in proposals.items() if props]
    total_sample = sum(samples.get(arm, 0.0) for arm in active) or 1.0

    taken: set[str] = set()
    spent = 0
    for arm in sorted(active, key=lambda a: samples.get(a, 0.0), reverse=True):
        budget = int(base * samples.get(arm, 0.0) / total_sample)
        cap = caps.get(arm)
        if cap is not None:
            budget = min(budget, cap)
        alloc.budgets[arm] = budget
        used = 0
        for p in sorted(proposals[arm], key=lambda p: p.priority, reverse=True):
            if p.path in taken:
                alloc.rejected.append((p, TAKEN))
            elif used + p.size > budget:
                # Rejected here, but not taken: a lower-sampled arm may still get it.
                alloc.rejected.append((p, OVER_BUDGET))
            elif spent + p.size > base:
                # Unreachable while the budgets are shares of `base`; kept so the
                # run cap holds even if a caller ever supplies budgets directly.
                alloc.rejected.append((p, OVER_RUN_CAP))
            else:
                used += p.size
                spent += p.size
                taken.add(p.path)
                alloc.accepted.append(p)

    log.info(
        "allocated %d/%d proposals, %d bytes of %d across %d arms",
        len(alloc.accepted),
        len(alloc.accepted) + len(alloc.rejected),
        spent,
        base,
        len(active),
    )
    return alloc
