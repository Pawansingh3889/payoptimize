"""The router, held to the numbers PLAN §9 asks for.

The simulation harness runs real sims through the real router with a fixed
seed, so these are behavioural assertions about the algorithm rather than
restatements of its code. `draw()` keeps them fast enough to run thousands of
payments per test.
"""

from __future__ import annotations

import random
from collections import Counter

import pytest

from payoptimize.models import AttemptStatus, RoutingMode
from payoptimize.providers import (
    CARD_PROVIDERS,
    ChargeRequest,
    InjectionMode,
    build_simulated,
)
from payoptimize.providers import simulated as sim_mod
from payoptimize.router import (
    GAMMA,
    MAX_ATTEMPTS,
    NoProviderAvailable,
    Posterior,
    Router,
    assign_mode,
)

SEGMENT = "TEST:USD:visa"
FIXED_RATES = {"stripe_sim": 0.95, "braintree_sim": 0.85, "adyen_sim": 0.80}
BEST = "stripe_sim"


@pytest.fixture
def corridor(monkeypatch: pytest.MonkeyPatch) -> str:
    """A corridor with the rates PLAN §9 names, so the test is about the
    router's learning and not about the sim's profile table."""
    monkeypatch.setitem(sim_mod.CORRIDOR_AUTH, SEGMENT, dict(FIXED_RATES))
    return SEGMENT


def _request() -> ChargeRequest:
    return ChargeRequest("pay_test", 1250, "USD", "TEST", "visa", SEGMENT)


class Harness:
    """Router + sims + the cascade loop api.py will run for real."""

    def __init__(self, seed: int = 42, **router_kwargs: float) -> None:
        self.rng = random.Random(seed)
        self.sims = build_simulated(self.rng, latency_scale=0)
        self.router = Router(self.rng, tuple(self.sims), **router_kwargs)
        self.request = _request()

    def pay(self, mode: RoutingMode) -> tuple[bool, list[str]]:
        tried: list[str] = []
        while True:
            provider = self.router.choose(SEGMENT, mode=mode, exclude=tried)
            outcome = self.sims[provider].draw(self.request)
            self.router.update(provider, SEGMENT, outcome.approved)
            tried.append(provider)
            if outcome.approved:
                return True, tried
            if not Router.should_retry(cascades=outcome.cascades, attempts_made=len(tried)):
                return False, tried

    def run(self, n: int, mode: RoutingMode | None = None) -> list[tuple[RoutingMode, bool, list]]:
        rows = []
        for i in range(n):
            chosen = mode if mode is not None else assign_mode(f"pay_{i:016x}")
            approved, tried = self.pay(chosen)
            rows.append((chosen, approved, tried))
        return rows

    def inject(self, provider: str, mode: str, **kwargs: float) -> None:
        self.sims[provider].inject(mode, **kwargs)


def _share(rows: list, provider: str) -> float:
    router_rows = [r for r in rows if r[0] is RoutingMode.ROUTER]
    if not router_rows:
        return 0.0
    return sum(1 for r in router_rows if r[2][0] == provider) / len(router_rows)


def _auth(rows: list, mode: RoutingMode) -> float:
    selected = [r for r in rows if r[0] is mode]
    return sum(1 for r in selected if r[1]) / len(selected) if selected else 0.0


# --- posteriors and decay ----------------------------------------------------


def test_prior_is_uniform() -> None:
    arm = Posterior()
    assert arm.alpha == 1.0
    assert arm.beta == 1.0
    assert arm.mean == 0.5
    assert arm.observations == 0.0


def test_success_and_failure_move_the_mean_the_right_way() -> None:
    router = Router(random.Random(0), CARD_PROVIDERS)
    for _ in range(20):
        router.update("stripe_sim", SEGMENT, True)
    assert router.posterior("stripe_sim", SEGMENT).mean > 0.9

    for _ in range(40):
        router.update("stripe_sim", SEGMENT, False)
    assert router.posterior("stripe_sim", SEGMENT).mean < 0.30


def test_memory_is_finite_so_old_evidence_stops_counting() -> None:
    """The whole point of discounting, stated as a contrast.

    Two thousand successes leave ~99 effective observations, not 2,000. Fifty
    consecutive failures then drag the mean to ~0.60 — where an undiscounted
    Beta(2001, 51) would still be sitting at 0.975, cheerfully routing traffic
    into a rail that has failed fifty times in a row.
    """
    router = Router(random.Random(0), CARD_PROVIDERS)
    for _ in range(2_000):
        router.update("stripe_sim", SEGMENT, True)
    established = router.posterior("stripe_sim", SEGMENT)
    assert established.observations < 1 / (1 - GAMMA) + 5  # ~99, not 2000
    assert established.mean > 0.98

    for _ in range(50):
        router.update("stripe_sim", SEGMENT, False)
    undiscounted = 2001 / (2001 + 51)
    assert router.posterior("stripe_sim", SEGMENT).mean < 0.65 < undiscounted

    for _ in range(100):
        router.update("stripe_sim", SEGMENT, False)
    assert router.posterior("stripe_sim", SEGMENT).mean < 0.25


def test_starved_arms_decay_toward_uncertainty_not_toward_a_verdict() -> None:
    """The property the recovery beat depends on. An arm nobody plays must end
    up at the prior, so Thompson sampling will reach for it again."""
    router = Router(random.Random(0), CARD_PROVIDERS)
    for _ in range(60):
        router.update("stripe_sim", SEGMENT, False)
    beaten = router.posterior("stripe_sim", SEGMENT)
    assert beaten.mean < 0.1

    for _ in range(1_000):  # traffic goes elsewhere; stripe is never played
        router.update("adyen_sim", SEGMENT, True)

    revived = router.posterior("stripe_sim", SEGMENT)
    assert revived.alpha == pytest.approx(1.0)
    assert revived.beta == pytest.approx(1.0)
    assert revived.mean == pytest.approx(0.5)


def test_updates_for_unknown_providers_are_ignored() -> None:
    """Prava outcomes reach this method and must not create a bandit arm."""
    router = Router(random.Random(0), CARD_PROVIDERS)
    router.update("prava", SEGMENT, True)
    assert router.snapshot() == []


# --- the A/B split -----------------------------------------------------------


def test_split_is_deterministic_and_roughly_even() -> None:
    ids = [f"pay_{i:016x}" for i in range(4_000)]
    assignments = [assign_mode(pid) for pid in ids]

    assert assignments == [assign_mode(pid) for pid in ids]  # recomputable forever
    baseline = sum(1 for m in assignments if m is RoutingMode.BASELINE)
    assert 0.47 < baseline / len(ids) < 0.53


def test_split_percentage_is_configurable() -> None:
    ids = [f"pay_{i:016x}" for i in range(2_000)]
    baseline = sum(1 for pid in ids if assign_mode(pid, split_pct=10) is RoutingMode.BASELINE)
    assert 0.07 < baseline / len(ids) < 0.13


# --- convergence and uplift (PLAN §9) ----------------------------------------


def test_converges_on_the_best_arm_and_beats_the_baseline(corridor: str) -> None:
    harness = Harness(seed=42)
    harness.run(1_500)
    measured = harness.run(4_000)

    assert _share(measured, BEST) > 0.70
    uplift = (_auth(measured, RoutingMode.ROUTER) - _auth(measured, RoutingMode.BASELINE)) * 100
    assert uplift >= 3.0


def test_the_baseline_really_is_round_robin(corridor: str) -> None:
    """The control arm must ignore learning entirely — otherwise the uplift
    number measures nothing."""
    harness = Harness(seed=42)
    rows = harness.run(600, mode=RoutingMode.BASELINE)
    first_choices = Counter(r[2][0] for r in rows)

    assert set(first_choices) == set(CARD_PROVIDERS)
    # Not exactly even: a cascading payment advances the cursor more than once,
    # so first choices drift by a couple of decisions over 600 payments.
    assert max(first_choices.values()) - min(first_choices.values()) <= 5
    assert all(0.30 < count / len(rows) < 0.36 for count in first_choices.values())


def test_learning_is_per_corridor_not_global(monkeypatch: pytest.MonkeyPatch) -> None:
    """A provider that is best in one corridor and worst in another is the
    entire reason for segmenting the arms."""
    monkeypatch.setitem(sim_mod.CORRIDOR_AUTH, "AA:USD:visa", dict(FIXED_RATES))
    monkeypatch.setitem(
        sim_mod.CORRIDOR_AUTH,
        "BB:USD:visa",
        {"stripe_sim": 0.60, "braintree_sim": 0.95, "adyen_sim": 0.70},
    )
    rng = random.Random(42)
    sims = build_simulated(rng, latency_scale=0)
    router = Router(rng, tuple(sims))

    for segment in ("AA:USD:visa", "BB:USD:visa"):
        request = ChargeRequest("p", 100, "USD", segment[:2], "visa", segment)
        for _ in range(1_500):
            provider = router.choose(segment)
            router.update(provider, segment, sims[provider].draw(request).approved)

    assert router.posterior("stripe_sim", "AA:USD:visa").mean > 0.85
    assert router.posterior("braintree_sim", "BB:USD:visa").mean > 0.85


# --- reacting to a failing rail ----------------------------------------------


def test_degradation_pushes_traffic_off_the_rail_then_clear_brings_it_back(
    corridor: str,
) -> None:
    harness = Harness(seed=42)
    harness.run(1_500)
    assert _share(harness.run(1_000, mode=RoutingMode.ROUTER), BEST) > 0.65

    harness.inject(BEST, InjectionMode.DEGRADED)
    harness.run(300, mode=RoutingMode.ROUTER)
    during = harness.run(300, mode=RoutingMode.ROUTER)
    assert _share(during, BEST) < 0.10

    harness.inject(BEST, InjectionMode.CLEAR)
    recovered = False
    for _ in range(12):  # 1,200 decisions ≈ 8 min at the demo's 2.5 tx/s
        if _share(harness.run(100, mode=RoutingMode.ROUTER), BEST) > 0.50:
            recovered = True
            break
    assert recovered, "a recovered rail must be rediscovered without an operator"


def test_a_hard_outage_still_gets_paid_through_the_cascade(corridor: str) -> None:
    """An outage emits retriable codes, so even the un-learning baseline
    cascades out of it. That is why degradation is the headline, not this."""
    harness = Harness(seed=42)
    harness.run(500)
    harness.inject(BEST, InjectionMode.OUTAGE)
    rows = harness.run(400, mode=RoutingMode.BASELINE)

    assert _auth(rows, RoutingMode.BASELINE) > 0.70


def test_router_beats_baseline_by_more_while_a_rail_is_degraded(corridor: str) -> None:
    """The demo's headline claim, asserted rather than narrated."""
    harness = Harness(seed=42)
    harness.run(1_500)
    harness.inject(BEST, InjectionMode.DEGRADED)
    harness.run(300)
    measured = harness.run(3_000)

    gap = (_auth(measured, RoutingMode.ROUTER) - _auth(measured, RoutingMode.BASELINE)) * 100
    assert gap >= 8.0


# --- cascade rules -----------------------------------------------------------


@pytest.mark.parametrize(
    ("cascades", "attempts_made", "expected"),
    [
        (True, 1, True),
        (True, 2, True),
        (True, 3, False),  # budget is 3 attempts total
        (False, 1, False),
        (False, 2, False),
    ],
)
def test_retry_budget(cascades: bool, attempts_made: int, expected: bool) -> None:
    assert Router.should_retry(cascades=cascades, attempts_made=attempts_made) is expected


def test_a_payment_never_hits_the_same_provider_twice(corridor: str) -> None:
    harness = Harness(seed=42)
    rows = harness.run(2_000)

    for _, _, tried in rows:
        assert len(tried) == len(set(tried))
        assert len(tried) <= MAX_ATTEMPTS


def test_both_arms_obey_the_same_cascade_rules(corridor: str) -> None:
    """If the arms retried differently, the uplift would measure retry policy
    rather than routing intelligence."""
    harness = Harness(seed=42)
    rows = harness.run(3_000)
    router_len = [len(r[2]) for r in rows if r[0] is RoutingMode.ROUTER]
    baseline_len = [len(r[2]) for r in rows if r[0] is RoutingMode.BASELINE]

    assert max(router_len) <= MAX_ATTEMPTS
    assert max(baseline_len) <= MAX_ATTEMPTS


def test_exhausting_every_provider_raises() -> None:
    router = Router(random.Random(0), CARD_PROVIDERS)
    with pytest.raises(NoProviderAvailable):
        router.choose(SEGMENT, exclude=CARD_PROVIDERS)


def test_unhealthy_providers_are_skipped_but_never_all_of_them() -> None:
    router = Router(random.Random(0), CARD_PROVIDERS)
    chosen = {router.choose(SEGMENT, unavailable=["stripe_sim"]) for _ in range(50)}
    assert "stripe_sim" not in chosen

    # If monitoring says every rail is sick, attempt one anyway — declining a
    # payment because our own health view is pessimistic is the worse outcome.
    assert router.choose(SEGMENT, unavailable=CARD_PROVIDERS) in CARD_PROVIDERS


def test_a_router_needs_providers() -> None:
    with pytest.raises(ValueError, match="at least one provider"):
        Router(random.Random(0), [])


# --- exploration -------------------------------------------------------------


def test_the_probe_fires_at_roughly_its_configured_rate(corridor: str) -> None:
    harness = Harness(seed=42, explore_p=0.02)
    harness.run(4_000, mode=RoutingMode.ROUTER)
    decisions = 4_000  # at least one decision per payment; cascades add more
    assert 0.01 < harness.router.explore_count / decisions < 0.06


def test_zero_exploration_is_allowed(corridor: str) -> None:
    harness = Harness(seed=42, explore_p=0.0)
    harness.run(500, mode=RoutingMode.ROUTER)
    assert harness.router.explore_count == 0


# --- boot rebuild ------------------------------------------------------------


def test_rebuild_reproduces_live_posteriors(corridor: str) -> None:
    """A restart must land on the posteriors the process would have had if it
    had never stopped — otherwise every deploy resets the router's knowledge."""
    live = Router(random.Random(0), CARD_PROVIDERS)
    history = []
    rng = random.Random(7)
    for _ in range(400):
        provider = rng.choice(CARD_PROVIDERS)
        approved = rng.random() < FIXED_RATES[provider]
        live.update(provider, SEGMENT, approved)
        history.append(
            {
                "provider": provider,
                "segment": SEGMENT,
                "status": AttemptStatus.SUCCEEDED if approved else AttemptStatus.FAILED,
            }
        )

    rebuilt = Router(random.Random(0), CARD_PROVIDERS)
    assert rebuilt.rebuild(history) == 400
    assert rebuilt.snapshot() == live.snapshot()


def test_rebuild_skips_pending_and_foreign_rows() -> None:
    router = Router(random.Random(0), CARD_PROVIDERS)
    replayed = router.rebuild(
        [
            {"provider": "stripe_sim", "segment": SEGMENT, "status": AttemptStatus.SUCCEEDED},
            {"provider": "stripe_sim", "segment": SEGMENT, "status": AttemptStatus.PENDING},
            {"provider": "prava", "segment": ":USD:prava", "status": AttemptStatus.SUCCEEDED},
        ]
    )

    assert replayed == 2  # the pending row is skipped; the prava row is counted but not learned
    assert {row["provider"] for row in router.snapshot()} == set(CARD_PROVIDERS)


def test_snapshot_is_sorted_and_serialisable() -> None:
    router = Router(random.Random(0), CARD_PROVIDERS)
    router.update("adyen_sim", "US:USD:visa", True)
    router.update("stripe_sim", "DE:EUR:visa", False)

    snapshot = router.snapshot()
    assert [row["segment"] for row in snapshot] == sorted(row["segment"] for row in snapshot)
    assert all({"provider", "segment", "alpha", "beta", "mean"} <= set(row) for row in snapshot)
