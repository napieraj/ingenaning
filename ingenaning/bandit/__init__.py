"""The bandit: a Beta posterior per (arm, bucket) and the per-run space allocator."""

from __future__ import annotations

from ingenaning.bandit.allocate import Allocation, Proposal, allocate
from ingenaning.bandit.bandit import Bandit, Posterior

__all__ = ["Allocation", "Bandit", "Posterior", "Proposal", "allocate"]
