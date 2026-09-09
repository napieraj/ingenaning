"""bandit/: a Beta(alpha, beta) per (arm, bucket), Thompson samples, and the per-run
space allocator of build doc §6. Offline: the temp DB from conftest and seeded numpy
Generators. No clock, no mounts, no network."""

from __future__ import annotations

import ast
import re
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest

from ingenaning import bandit as bandit_pkg
from ingenaning.bandit.allocate import Allocation, allocate
from ingenaning.bandit.bandit import Bandit, Posterior
from ingenaning.config import BanditParams
from ingenaning.store import queries as q
from ingenaning.store.db import Database

PRIOR = (2.0, 1.0)  # deploy/policy.example.yaml `bandit.prior: [2, 1]`
B1 = "presence=home|d=day"
B2 = "presence=away|d=night"

# --- fixtures ---------------------------------------------------------------


@pytest.fixture
def params() -> BanditParams:
    """The `bandit:` block of deploy/policy.example.yaml."""
    return BanditParams(prior=PRIOR, decay_weekly=0.95, expectation_bonus=3.0, expectation_miss=0.5)


@pytest.fixture
def conn(db: Database) -> Iterator[sqlite3.Connection]:
    """This thread's autocommit connection; `db.tx()` hands back the same object."""
    with db.connection() as c:
        yield c


@pytest.fixture
def bandit(conn: sqlite3.Connection, params: BanditParams) -> Bandit:
    return Bandit(conn, params, rng=np.random.default_rng(0))


# --- Posterior --------------------------------------------------------------


def test_posterior_mean_and_evidence_count():
    """§6 'a Beta(alpha, beta)': mean = a/(a+b); evidence n = a+b - sum(prior)."""
    p = Posterior(alpha=4.0, beta=2.0, prior=PRIOR)
    assert p.mean == 4.0 / 6.0
    assert p.n == 3.0
    assert Posterior(alpha=2.0, beta=1.0, prior=PRIOR).n == 0.0


# --- Bandit: read, reward, feedback, bonus, penalise, decay -----------------


def test_posterior_of_unseen_pair_is_the_prior(bandit: Bandit, conn: sqlite3.Connection):
    """Prior [2, 1] (policy.example.yaml): an unseen (arm, bucket) reads as the prior, no row."""
    p = bandit.posterior("seq", B1)
    assert (p.alpha, p.beta) == PRIOR
    assert p.mean == 2.0 / 3.0 and p.n == 0.0
    assert q.get_posterior(conn, "seq", B1) is None  # a read never writes


def test_reward_adds_one_to_alpha_or_beta_in_that_bucket_only(
    bandit: Bandit, conn: sqlite3.Connection
):
    """§6 'hit=1 -> alpha+=1, else beta+=1, in the bucket the proposal was made under'."""
    p = bandit.reward("seq", B1, hit=True)
    assert (p.alpha, p.beta) == (3.0, 1.0)
    p = bandit.reward("seq", B1, hit=False)
    assert (p.alpha, p.beta) == (3.0, 2.0)
    assert q.get_posterior(conn, "seq", B1) == (3.0, 2.0)  # persisted in `posteriors`
    assert bandit.posterior("seq", B1).n == 2.0
    other = bandit.posterior("seq", B2)
    assert (other.alpha, other.beta) == PRIOR
    assert q.get_posterior(conn, "seq", B2) is None
    assert q.get_posterior(conn, "p-x", B1) is None


def test_feedback_adds_one_to_alpha_or_beta_or_nothing(bandit: Bandit, conn: sqlite3.Connection):
    """§6 'UI feedback adds ±1 to alpha or beta directly'; a 0 changes nothing."""
    p = bandit.feedback("seq", B1, 1)
    assert (p.alpha, p.beta) == (3.0, 1.0)
    p = bandit.feedback("seq", B1, -1)
    assert (p.alpha, p.beta) == (3.0, 2.0)
    p = bandit.feedback("seq", B1, 0)
    assert (p.alpha, p.beta) == (3.0, 2.0)
    assert q.get_posterior(conn, "seq", B1) == (3.0, 2.0)
    assert q.get_posterior(conn, "seq", B2) is None


def test_bonus_adds_the_multiplier_to_alpha(
    bandit: Bandit, params: BanditParams, conn: sqlite3.Connection
):
    """D-004: a met expectation arrives as bonus(arm, bucket, m): alpha += m, that pair only."""
    bandit.bonus("seq", B1, params.expectation_bonus)
    assert q.get_posterior(conn, "seq", B1) == (5.0, 1.0)
    assert q.get_posterior(conn, "seq", B2) is None
    assert q.get_posterior(conn, "p-x", B1) is None
    bandit.bonus("seq", B1, 1.5)
    assert q.get_posterior(conn, "seq", B1) == (6.5, 1.0)
    assert bandit.posterior("seq", B1).n == 4.5


def test_penalise_adds_the_amount_to_beta_of_every_listed_arm(
    bandit: Bandit, params: BanditParams, conn: sqlite3.Connection
):
    """D-004: a miss arrives as penalise(arms, bucket, a): beta += a for every listed arm."""
    bandit.reward("seq", B1, hit=True)
    bandit.penalise(["seq", "p-x"], B1, params.expectation_miss)
    assert q.get_posterior(conn, "seq", B1) == (3.0, 1.5)
    assert q.get_posterior(conn, "p-x", B1) == (2.0, 1.5)
    assert q.get_posterior(conn, "scorer", B1) is None  # not listed: untouched
    assert q.get_posterior(conn, "seq", B2) is None  # other bucket: untouched
    bandit.penalise([], B1, params.expectation_miss)  # nobody to blame: no-op
    assert q.get_posterior(conn, "seq", B1) == (3.0, 1.5)
    assert q.get_posterior(conn, "p-x", B1) == (2.0, 1.5)


def test_decay_shrinks_toward_the_prior_and_returns_the_row_count(
    bandit: Bandit, params: BanditParams, conn: sqlite3.Connection
):
    """§6 'Weekly decay alpha,beta x0.95 toward the prior': a' = p0 + (a - p0)*factor per row."""
    assert bandit.decay(0.5) == 0  # nothing stored yet
    q.set_posterior(conn, "seq", B1, 12.0, 6.0)
    q.set_posterior(conn, "p-x", B2, 2.0, 1.0)
    assert bandit.decay(0.5) == 2
    assert q.get_posterior(conn, "seq", B1) == (7.0, 3.5)
    assert q.get_posterior(conn, "p-x", B2) == (2.0, 1.0)  # at the prior: a fixed point
    assert bandit.decay() == 2  # default factor is BanditParams.decay_weekly
    assert q.get_posterior(conn, "seq", B1) == pytest.approx((2.0 + 5.0 * 0.95, 1.0 + 2.5 * 0.95))
    assert bandit.posterior("seq", B1).n == pytest.approx(7.5 * 0.95)
    assert Bandit(conn, params).decay(1.0) == 2  # rng is optional; factor 1 changes nothing
    assert q.get_posterior(conn, "seq", B1) == pytest.approx((2.0 + 5.0 * 0.95, 1.0 + 2.5 * 0.95))
    assert bandit.decay(0.0) == 2  # factor 0 forgets everything
    assert q.get_posterior(conn, "seq", B1) == PRIOR
    assert q.get_posterior(conn, "p-x", B2) == PRIOR


# --- Bandit: sampling -------------------------------------------------------


def test_samples_are_deterministic_under_a_seed_and_lie_in_the_unit_interval(
    conn: sqlite3.Connection, params: BanditParams
):
    """§6 'samples = {arm: beta.rvs(a, b) ...}': same seed, same draws; every draw in (0, 1)."""
    q.set_posterior(conn, "seq", B1, 3.0, 2.0)
    q.set_posterior(conn, "p-x", B1, 2.0, 5.0)
    a = Bandit(conn, params, rng=np.random.default_rng(7))
    b = Bandit(conn, params, rng=np.random.default_rng(7))
    first = a.sample("seq", B1)
    assert isinstance(first, float) and 0.0 < first < 1.0
    assert first == b.sample("seq", B1)
    arms = ["seq", "p-x", "scorer"]  # scorer is unseen: drawn from the prior
    sa, sb = a.samples(arms, B1), b.samples(arms, B1)
    assert sa == sb and list(sa) == arms
    assert all(isinstance(v, float) and 0.0 < v < 1.0 for v in sa.values())


def test_sample_reads_the_stored_posterior_of_that_bucket(bandit: Bandit, conn: sqlite3.Connection):
    """§6 '# current bucket': a peaked (alpha, beta) stored in one bucket shows only there."""
    q.set_posterior(conn, "sure", B1, 1000.0, 1.0)
    q.set_posterior(conn, "sure", B2, 1.0, 1000.0)
    assert bandit.sample("sure", B1) > 0.9
    assert bandit.sample("sure", B2) < 0.1
    assert bandit.samples(["sure"], B1)["sure"] > 0.9


# --- Bandit: reporting ------------------------------------------------------


def test_means_by_arm_is_arm_to_bucket_to_mean(bandit: Bandit, conn: sqlite3.Connection):
    """§9 GET /api/arms 'posteriors per bucket': means_by_arm() is {arm: {bucket: mean}}."""
    assert bandit.means_by_arm() == {}
    q.set_posterior(conn, "seq", B1, 3.0, 1.0)
    q.set_posterior(conn, "seq", B2, 2.0, 2.0)
    q.set_posterior(conn, "p-x", B1, 1.0, 3.0)
    assert bandit.means_by_arm() == {"seq": {B1: 0.75, B2: 0.5}, "p-x": {B1: 0.25}}


def test_expected_value_is_the_posterior_mean(bandit: Bandit, conn: sqlite3.Connection):
    """§6 eviction 'expected value = posterior mean of the backing arm'."""
    assert bandit.expected_value("seq", B1) == 2.0 / 3.0  # unseen: mean of the prior
    q.set_posterior(conn, "seq", B1, 3.0, 1.0)
    assert bandit.expected_value("seq", B1) == 0.75
    assert bandit.expected_value("seq", B1) == bandit.posterior("seq", B1).mean


# --- Bandit: convergence ----------------------------------------------------


def test_thompson_sampling_converges_on_the_better_arm(
    db: Database, conn: sqlite3.Connection, params: BanditParams
):
    """§12 'bandit convergence on synthetic rewards': rates 0.8 vs 0.2, 300 rounds, seed 0."""
    rates = {"good": 0.8, "bad": 0.2}
    world = np.random.default_rng(0)
    bandit = Bandit(conn, params, rng=np.random.default_rng(0))
    chosen: list[str] = []
    with db.tx():  # same connection as `conn`; one transaction keeps the loop well under 1 s
        for _ in range(300):
            s = bandit.samples(list(rates), B1)
            arm = max(s, key=s.__getitem__)
            chosen.append(arm)
            bandit.reward(arm, B1, hit=bool(world.random() < rates[arm]))
    means = bandit.means_by_arm()
    assert means["good"][B1] > means["bad"][B1]
    assert chosen[-100:].count("good") > 70
    assert bandit.posterior("good", B1).n + bandit.posterior("bad", B1).n == 300.0


# --- allocate ---------------------------------------------------------------


@dataclass(slots=True)
class P:
    """Stand-in for arms.base.Proposal: the allocator only needs these attributes.
    slots=True like the real one, so the allocator cannot hang extra state on it."""

    arm: str
    path: str
    size: int
    confidence: float = 1.0
    priority: float = 0.0


def props(arm: str, *items: tuple[str, int, float]) -> list[P]:
    return [P(arm, path, size, confidence) for path, size, confidence in items]


def accepted(alloc: Allocation) -> list[tuple[str, str]]:
    return [(p.arm, p.path) for p in alloc.accepted]


def rejected(alloc: Allocation) -> dict[tuple[str, str], str]:
    return {(p.arm, p.path): reason for p, reason in alloc.rejected}


def test_budgets_are_the_sample_share_of_total_space_over_proposing_arms():
    """§6 'budget = free * samples[arm] / sum(samples)', free being min(free, max_promote)."""
    proposals = {
        "seq": props("seq", ("/a", 10, 0.9)),
        "p-x": props("p-x", ("/b", 10, 0.9)),
        "quiet": [],  # ran, proposed nothing: not in sum(samples)
    }
    samples = {"seq": 0.75, "p-x": 0.25, "quiet": 0.5, "off": 0.5}  # "off" did not run
    alloc = allocate(proposals, samples, free=1000, max_promote=800)
    assert alloc.budgets["seq"] == 600 and alloc.budgets["p-x"] == 200
    assert alloc.budgets.get("quiet", 0) == 0 and alloc.budgets.get("off", 0) == 0
    assert alloc.samples == samples
    assert accepted(alloc) == [("seq", "/a"), ("p-x", "/b")] and alloc.bytes == 20
    alloc = allocate(proposals, samples, free=400, max_promote=800)  # free is the binding one
    assert alloc.budgets["seq"] == 300 and alloc.budgets["p-x"] == 100


def test_budgets_truncate_to_int():
    """§6 budgets are bytes: 1003 x 0.5 = 501.5 is truncated to 501, never rounded up."""
    proposals = {"seq": props("seq", ("/a", 1, 1.0)), "p-x": props("p-x", ("/b", 1, 1.0))}
    alloc = allocate(proposals, {"seq": 0.5, "p-x": 0.5}, free=1003, max_promote=1003)
    assert alloc.budgets == {"seq": 501, "p-x": 501}
    assert all(isinstance(b, int) for b in alloc.budgets.values())


def test_arms_are_served_in_descending_sample_order():
    """§6 'for arm in sorted(arms, key=samples, reverse=True)': sample order, not dict order."""
    low = props("low", ("/x", 10, 1.0), ("/l", 10, 1.0))
    high = props("high", ("/h", 10, 0.1), ("/x", 10, 0.1))
    proposals = {"low": low, "high": high}
    alloc = allocate(proposals, {"low": 0.25, "high": 0.75}, free=1000, max_promote=1000)
    assert accepted(alloc) == [("high", "/x"), ("high", "/h"), ("low", "/l")]
    assert rejected(alloc) == {("low", "/x"): "taken by another arm"}
    assert alloc.bytes == 30
    # /x is backed by two arms: 0.1 * (1 + 0.5*1) outranks /h at 0.1 inside "high"
    assert high[1].priority == pytest.approx(0.15) and high[0].priority == pytest.approx(0.1)


def test_consensus_raises_priority_and_sets_the_rank_within_an_arm():
    """§6 'multiply their priority by (1 + 0.5·n)', tested as confidence * (1 + 0.5*(n-1))."""
    solo, shared_seq = P("seq", "/solo", 100, 0.9), P("seq", "/shared", 100, 0.5)
    shared_px, shared_sc = P("p-x", "/shared", 100, 0.4), P("scorer", "/shared", 100, 0.3)
    proposals = {"seq": [solo, shared_seq], "p-x": [shared_px], "scorer": [shared_sc]}
    samples = {"seq": 0.5, "p-x": 0.25, "scorer": 0.25}
    alloc = allocate(proposals, samples, free=200, max_promote=200)
    # three backers: (1 + 0.5*2) = 2x; a single backer keeps its confidence
    assert shared_seq.priority == pytest.approx(1.0)
    assert shared_px.priority == pytest.approx(0.8)
    assert shared_sc.priority == pytest.approx(0.6)
    assert solo.priority == pytest.approx(0.9)
    # seq's budget (100) fits one file: the consensus path outranks the higher-confidence one
    assert alloc.budgets["seq"] == 100
    assert accepted(alloc) == [("seq", "/shared")]
    assert rejected(alloc) == {
        ("seq", "/solo"): "budget",
        ("p-x", "/shared"): "taken by another arm",
        ("scorer", "/shared"): "taken by another arm",
    }


def test_path_taken_by_a_higher_sample_arm_is_rejected_for_the_lower_one():
    """'taken by another arm': accepted once, for the higher sample; a budget reject frees it."""
    proposals = {
        "high": props("high", ("/dup", 10, 1.0), ("/big", 150, 0.5)),
        "low": props("low", ("/dup", 10, 1.0), ("/big", 150, 0.5)),
    }
    samples = {"high": 0.75, "low": 0.25}
    alloc = allocate(proposals, samples, free=800, max_promote=800, arm_caps={"high": 100})
    assert accepted(alloc) == [("high", "/dup"), ("low", "/big")]
    assert rejected(alloc) == {
        ("high", "/big"): "budget",  # 150 > cap 100: not taken, so "low" may still have it
        ("low", "/dup"): "taken by another arm",
    }
    assert alloc.bytes == 160


def test_budget_reject_does_not_stop_smaller_later_proposals():
    """§6 'take proposals[arm] in rank order until budget': one miss, smaller later ones fit."""
    proposals = {"seq": props("seq", ("/a", 60, 0.9), ("/b", 50, 0.8), ("/c", 40, 0.7))}
    alloc = allocate(proposals, {"seq": 1.0}, free=100, max_promote=100)
    assert alloc.budgets == {"seq": 100}
    assert accepted(alloc) == [("seq", "/a"), ("seq", "/c")]  # 60, then 60+50 > 100, then 100
    assert rejected(alloc) == {("seq", "/b"): "budget"}
    assert alloc.bytes == 100  # exactly the budget is allowed


def test_arm_cap_bounds_the_budget_but_never_raises_it():
    """arm_caps: budget = min(share, cap); a None or over-share cap changes nothing."""

    def run(caps: dict[str, int | None] | None) -> Allocation:
        proposals = {
            "seq": props("seq", ("/a", 100, 0.9), ("/b", 100, 0.8)),
            "p-x": props("p-x", ("/c", 100, 0.9)),
        }
        samples = {"seq": 0.75, "p-x": 0.25}
        return allocate(proposals, samples, free=800, max_promote=800, arm_caps=caps)

    assert run(None).budgets == {"seq": 600, "p-x": 200}
    assert run({"seq": None}).budgets == {"seq": 600, "p-x": 200}
    assert run({"seq": 10_000}).budgets == {"seq": 600, "p-x": 200}
    assert run({"nobody": 1}).budgets == {"seq": 600, "p-x": 200}
    capped = run({"seq": 150})
    assert capped.budgets == {"seq": 150, "p-x": 200}
    assert accepted(capped) == [("seq", "/a"), ("p-x", "/c")]
    assert rejected(capped) == {("seq", "/b"): "budget"}


@pytest.mark.parametrize(("free", "max_promote"), [(1000, 250), (250, 1000), (250, 250)])
def test_total_accepted_never_exceeds_min_of_free_and_max_promote(free: int, max_promote: int):
    """§6 '... until budget or max_promote_per_run': accepted bytes <= min(free, max_promote)."""
    proposals = {
        "seq": props("seq", ("/a", 100, 0.9), ("/b", 100, 0.8), ("/c", 100, 0.7), ("/d", 50, 0.6))
    }
    alloc = allocate(proposals, {"seq": 1.0}, free=free, max_promote=max_promote)
    assert alloc.bytes == 250 == min(free, max_promote)
    assert accepted(alloc) == [("seq", "/a"), ("seq", "/b"), ("seq", "/d")]
    # the per-arm budget is a share of min(free, max_promote), so it is what fires; the
    # allocator's overall "run cap" guard is accepted too should its budgets ever exceed it
    assert rejected(alloc) in ({("seq", "/c"): "budget"}, {("seq", "/c"): "run cap"})


@pytest.mark.parametrize("free", [0, -1])
def test_no_free_space_accepts_nothing_and_rejects_everything_with_a_reason(free: int):
    """§6 'free = hot_free_bytes()' at 0 (or below the floor): nothing accepted, all rejected."""
    proposals = {"seq": props("seq", ("/a", 1, 0.9)), "p-x": props("p-x", ("/b", 1, 0.9))}
    alloc = allocate(proposals, {"seq": 0.75, "p-x": 0.25}, free=free, max_promote=1000)
    assert alloc.accepted == [] and alloc.bytes == 0
    assert sorted((p.arm, p.path) for p, _ in alloc.rejected) == [("p-x", "/b"), ("seq", "/a")]
    assert all(isinstance(reason, str) and reason for _, reason in alloc.rejected)
    assert all(b <= 0 for b in alloc.budgets.values())


def test_empty_proposals_give_an_empty_allocation():
    """§6 'proposals = {arm: arm.propose(ctx, pool)}' may be all []: no error, nothing allocated."""
    cases: list[tuple[dict[str, list[P]], dict[str, float]]] = [
        ({}, {}),
        ({"seq": []}, {"seq": 0.5}),
        ({"seq": [], "p-x": []}, {}),
    ]
    for proposals, samples in cases:
        alloc = allocate(proposals, samples, free=1000, max_promote=1000)
        assert isinstance(alloc, Allocation)
        assert alloc.accepted == [] and alloc.rejected == [] and alloc.bytes == 0
        assert sum(alloc.budgets.values()) == 0


def test_input_lists_keep_their_order_while_priority_is_written_on_the_objects():
    """§6 'in rank order': rank a copy, never the input list; write .priority on the objects."""
    seq = props("seq", ("/c", 10, 0.1), ("/b", 10, 0.5), ("/a", 10, 0.9))
    px = props("p-x", ("/a", 10, 0.2))
    proposals = {"seq": seq, "p-x": px}
    assert all(p.priority == 0.0 for p in seq + px)
    alloc = allocate(proposals, {"seq": 0.75, "p-x": 0.25}, free=1000, max_promote=1000)
    assert list(proposals) == ["seq", "p-x"] and proposals["seq"] is seq and proposals["p-x"] is px
    assert [p.path for p in seq] == ["/c", "/b", "/a"] and [p.path for p in px] == ["/a"]
    assert [p.priority for p in seq] == pytest.approx([0.1, 0.5, 0.9 * 1.5])
    assert px[0].priority == pytest.approx(0.2 * 1.5)
    assert accepted(alloc) == [("seq", "/a"), ("seq", "/b"), ("seq", "/c")]  # rank order
    assert alloc.accepted[0] is seq[2]  # the caller's objects come back, not copies
    assert rejected(alloc) == {("p-x", "/a"): "taken by another arm"}


# --- contract ---------------------------------------------------------------

_PARAM_FIELDS = {"expectation_bonus", "expectation_miss"}  # BanditParams values, not table reads


def test_bandit_package_never_reads_expectations():
    """D-004 / AGENTS.md rule 5: the bandit hears of expectations only via bonus()/penalise()."""
    for path in Path(bandit_pkg.__file__).parent.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names: list[str] = []
            if isinstance(node, ast.Attribute):
                names.append(node.attr)
            elif isinstance(node, ast.Name):
                names.append(node.id)
            elif isinstance(node, ast.alias):
                names.append(node.name)
            for name in names:
                if "expectation" in name:
                    assert name in _PARAM_FIELDS, f"{path}: reads {name}"
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                sql = re.search(r"\b(select|insert|update|delete)\b", node.value, re.I)
                table = re.search(r"\bexpectations\b", node.value, re.I)
                assert not (sql and table), f"{path}: SQL against expectations"
