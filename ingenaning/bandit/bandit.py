"""Contextual Thompson sampling: one Beta(alpha, beta) per (arm, bucket).

Build doc section 6. The posterior of an (arm, bucket) pair that has never been
rewarded is the prior from settings; reading it never writes a row, so the
`posteriors` table holds evidence only.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass

import numpy as np

from ingenaning.config import BanditParams
from ingenaning.store import queries as q

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Posterior:
    """A Beta(alpha, beta) together with the prior it grew from.

    The prior travels with the posterior because `n` — how much evidence this
    pair has actually seen — is only meaningful relative to it, and the prior is
    configuration (BanditParams.prior), not a constant.
    """

    alpha: float
    beta: float
    prior: tuple[float, float]

    @property
    def mean(self) -> float:
        return self.alpha / (self.alpha + self.beta)

    @property
    def n(self) -> float:
        """Observations behind this posterior: the mass added on top of the prior."""
        return self.alpha + self.beta - sum(self.prior)


class Bandit:
    """Reads and writes `posteriors`; draws Thompson samples for one bucket.

    All writes go through the connection handed in, so a caller may wrap a whole
    run in one `Database.tx()`.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        params: BanditParams,
        rng: np.random.Generator | None = None,
    ) -> None:
        self.conn = conn
        self.params = params
        self.rng = np.random.default_rng() if rng is None else rng

    @property
    def prior(self) -> tuple[float, float]:
        a, b = self.params.prior
        return (float(a), float(b))

    # --- reading ------------------------------------------------------------

    def posterior(self, arm: str, bucket: str) -> Posterior:
        """The stored posterior, or the prior. A read never creates a row."""
        ab = q.get_posterior(self.conn, arm, bucket)
        prior = self.prior
        return Posterior(*ab, prior=prior) if ab else Posterior(prior[0], prior[1], prior)

    def expected_value(self, arm: str, bucket: str) -> float:
        """Section 6 eviction ranks by 'posterior mean of the backing arm'."""
        return self.posterior(arm, bucket).mean

    def means_by_arm(self) -> dict[str, dict[str, float]]:
        """{arm: {bucket: mean}} over the stored pairs, for GET /api/arms."""
        prior = self.prior
        out: dict[str, dict[str, float]] = {}
        for r in q.all_posteriors(self.conn):
            p = Posterior(float(r["alpha"]), float(r["beta"]), prior)
            out.setdefault(str(r["arm"]), {})[str(r["bucket"])] = p.mean
        return out

    # --- sampling -----------------------------------------------------------

    def sample(self, arm: str, bucket: str) -> float:
        """One Beta draw for this arm in this bucket. Deterministic under a seeded rng."""
        p = self.posterior(arm, bucket)
        return float(self.rng.beta(p.alpha, p.beta))

    def samples(self, arms: Iterable[str], bucket: str) -> dict[str, float]:
        """One draw per arm, in the order given."""
        return {arm: self.sample(arm, bucket) for arm in arms}

    # --- writing ------------------------------------------------------------

    def _store(self, arm: str, bucket: str, p: Posterior) -> Posterior:
        q.set_posterior(self.conn, arm, bucket, p.alpha, p.beta)
        return p

    def reward(self, arm: str, bucket: str, hit: bool) -> Posterior:
        """Section 6: 'hit=1 -> alpha+=1, else beta+=1', in the proposal's bucket."""
        p = self.posterior(arm, bucket)
        if hit:
            p.alpha += 1.0
        else:
            p.beta += 1.0
        return self._store(arm, bucket, p)

    def feedback(self, arm: str, bucket: str, value: int) -> Posterior:
        """Section 6: 'UI feedback adds +-1 to alpha or beta directly'. 0 is a no-op."""
        p = self.posterior(arm, bucket)
        if value > 0:
            p.alpha += 1.0
        elif value < 0:
            p.beta += 1.0
        else:
            return p  # nothing said, nothing written
        return self._store(arm, bucket, p)

    def bonus(self, arm: str, bucket: str, amount: float) -> Posterior:
        """Credit one arm. The executor decides what earns it; the bandit only adds."""
        p = self.posterior(arm, bucket)
        p.alpha += amount
        return self._store(arm, bucket, p)

    def penalise(self, arms: Iterable[str], bucket: str, amount: float) -> int:
        """Blame every listed arm in this bucket. An empty list is a no-op."""
        n = 0
        for arm in arms:
            p = self.posterior(arm, bucket)
            p.beta += amount
            self._store(arm, bucket, p)
            n += 1
        return n

    def decay(self, factor: float | None = None) -> int:
        """Section 6: 'weekly decay alpha,beta x0.95 toward the prior'.

        Per row a' = p0 + (a - p0) * factor, so the prior is a fixed point and
        factor 0 forgets everything. Returns the number of rows rewritten.
        """
        f = self.params.decay_weekly if factor is None else factor
        a0, b0 = self.prior
        rows = q.all_posteriors(self.conn)
        for r in rows:
            q.set_posterior(
                self.conn,
                str(r["arm"]),
                str(r["bucket"]),
                a0 + (float(r["alpha"]) - a0) * f,
                b0 + (float(r["beta"]) - b0) * f,
            )
        log.info("decayed %d posteriors by %.3f", len(rows), f)
        return len(rows)
