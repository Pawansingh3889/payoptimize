"""The simulated rails: seeded determinism, corridor auth rates that hold up
over 2,000 draws, and injections that actually change what the rail does.

Everything statistical runs against `draw()` — the pure core — so 2,000
authorizations cost no event loops and no wall-clock latency.
"""

from __future__ import annotations

import asyncio
import random
from collections import Counter

import pytest

from payoptimize.models import AttemptStatus, DeclineCode
from payoptimize.providers import (
    CARD_PROVIDERS,
    CORRIDOR_AUTH,
    INFRASTRUCTURE_DECLINES,
    SOFT_DECLINES,
    TERMINAL_DECLINES,
    ChargeRequest,
    InjectionMode,
    ProviderAdapter,
    SimState,
    build_registry,
    build_simulated,
    should_cascade,
)

DRAWS = 2_000
TOLERANCE = 0.03  # ±3 points, per PLAN §9


def _request(segment: str = "US:USD:visa") -> ChargeRequest:
    country, currency, brand = segment.split(":")
    return ChargeRequest(
        payment_id="pay_test",
        amount_cents=1250,
        currency=currency,
        country=country,
        card_brand=brand,
        segment=segment,
    )


def _observed_auth(provider, segment: str, draws: int = DRAWS) -> float:
    request = _request(segment)
    approved = sum(provider.draw(request).approved for _ in range(draws))
    return approved / draws


def _declines(provider, segment: str, draws: int = DRAWS) -> Counter[str]:
    request = _request(segment)
    outcomes = [provider.draw(request) for _ in range(draws)]
    return Counter(o.decline_code for o in outcomes if not o.approved)


# --- taxonomy ----------------------------------------------------------------


def test_every_decline_code_is_classified() -> None:
    assert set(DeclineCode) == INFRASTRUCTURE_DECLINES | SOFT_DECLINES | TERMINAL_DECLINES
    assert not INFRASTRUCTURE_DECLINES & TERMINAL_DECLINES
    assert not INFRASTRUCTURE_DECLINES & SOFT_DECLINES


@pytest.mark.parametrize(
    ("code", "cascades"),
    [
        (DeclineCode.ISSUER_UNAVAILABLE, True),
        (DeclineCode.PROVIDER_TIMEOUT, True),
        (DeclineCode.PROCESSING_ERROR, True),
        (DeclineCode.RATE_LIMITED, True),
        (DeclineCode.DO_NOT_HONOR, False),
        (DeclineCode.INSUFFICIENT_FUNDS, False),
        (DeclineCode.EXPIRED_CARD, False),
        (DeclineCode.INVALID_CARD, False),
        (DeclineCode.FRAUD_SUSPECTED, False),
        (DeclineCode.STOLEN_CARD, False),
        (DeclineCode.APPROVAL_TIMEOUT, False),
        ("", False),
        ("PRAVA_SOMETHING_NEW", False),  # an unrecognised code never cascades
    ],
)
def test_cascade_table(code: str, cascades: bool) -> None:
    assert should_cascade(code) is cascades


# --- determinism -------------------------------------------------------------


def test_same_seed_replays_exactly() -> None:
    def run() -> list[tuple[str, int]]:
        sims = build_simulated(random.Random(42), latency_scale=0)
        request = _request()
        return [
            (o.decline_code, o.latency_ms)
            for _ in range(50)
            for o in (sims["stripe_sim"].draw(request),)
        ]

    assert run() == run()


def test_different_seeds_diverge() -> None:
    first = build_simulated(random.Random(1), latency_scale=0)["stripe_sim"]
    second = build_simulated(random.Random(2), latency_scale=0)["stripe_sim"]
    request = _request()
    assert [first.draw(request).latency_ms for _ in range(20)] != [
        second.draw(request).latency_ms for _ in range(20)
    ]


# --- corridor profiles -------------------------------------------------------


@pytest.mark.parametrize("segment", sorted(CORRIDOR_AUTH))
@pytest.mark.parametrize("provider_name", CARD_PROVIDERS)
def test_corridor_auth_rates_hold(segment: str, provider_name: str) -> None:
    sims = build_simulated(random.Random(42), latency_scale=0)
    expected = CORRIDOR_AUTH[segment][provider_name]

    assert abs(_observed_auth(sims[provider_name], segment) - expected) < TOLERANCE


def test_unknown_corridor_falls_back_to_the_baseline() -> None:
    sims = build_simulated(random.Random(42), latency_scale=0)
    stripe = sims["stripe_sim"]

    assert "JP:JPY:visa" not in CORRIDOR_AUTH
    assert abs(_observed_auth(stripe, "JP:JPY:visa") - stripe.spec.baseline_auth) < TOLERANCE


def test_no_provider_wins_every_corridor() -> None:
    """If one rail dominated everywhere, there would be nothing to learn."""
    winners = {max(rates, key=lambda name: rates[name]) for rates in CORRIDOR_AUTH.values()}
    assert winners == set(CARD_PROVIDERS)


# --- decline mixes -----------------------------------------------------------


def test_healthy_declines_are_mostly_terminal_with_some_infrastructure() -> None:
    sims = build_simulated(random.Random(42), latency_scale=0)
    declines = _declines(sims["adyen_sim"], "US:USD:visa")
    total = sum(declines.values())

    assert total > 0
    assert set(declines) <= set(DeclineCode)
    infrastructure = sum(n for code, n in declines.items() if code in INFRASTRUCTURE_DECLINES)
    assert 0.05 < infrastructure / total < 0.25


# --- injections --------------------------------------------------------------


def test_outage_stops_authorizations_and_emits_retriable_codes() -> None:
    sims = build_simulated(random.Random(42), latency_scale=0)
    stripe = sims["stripe_sim"]
    stripe.inject(InjectionMode.OUTAGE)

    assert stripe.state is SimState.DOWN
    assert _observed_auth(stripe, "US:USD:visa", draws=200) == 0.0
    declines = _declines(stripe, "US:USD:visa", draws=200)
    assert set(declines) <= INFRASTRUCTURE_DECLINES
    assert all(should_cascade(code) for code in declines)


def test_outage_also_inflates_latency() -> None:
    sims = build_simulated(random.Random(42), latency_scale=0)
    stripe = sims["stripe_sim"]
    request = _request()
    healthy = sorted(stripe.draw(request).latency_ms for _ in range(300))[150]

    stripe.inject(InjectionMode.OUTAGE)
    down = sorted(stripe.draw(request).latency_ms for _ in range(300))[150]

    assert down > healthy * 2


def test_degradation_is_the_lever_it_is_supposed_to_be() -> None:
    """Auth collapses to the injected rate and the declines do NOT cascade —
    that is what makes the baseline eat them and the router route around them."""
    sims = build_simulated(random.Random(42), latency_scale=0)
    stripe = sims["stripe_sim"]
    stripe.inject(InjectionMode.DEGRADED)

    assert stripe.state is SimState.DEGRADED
    assert abs(_observed_auth(stripe, "US:USD:visa") - 0.45) < TOLERANCE

    declines = _declines(stripe, "US:USD:visa")
    total = sum(declines.values())
    assert declines[DeclineCode.DO_NOT_HONOR] / total > 0.75
    cascading = sum(n for code, n in declines.items() if should_cascade(code))
    assert cascading / total < 0.10


def test_degradation_accepts_an_explicit_rate() -> None:
    sims = build_simulated(random.Random(42), latency_scale=0)
    stripe = sims["stripe_sim"]
    stripe.inject(InjectionMode.DEGRADED, auth_rate=0.20)

    assert abs(_observed_auth(stripe, "US:USD:visa") - 0.20) < TOLERANCE


def test_clear_restores_the_corridor_profile() -> None:
    sims = build_simulated(random.Random(42), latency_scale=0)
    stripe = sims["stripe_sim"]
    stripe.inject(InjectionMode.OUTAGE)
    stripe.inject(InjectionMode.CLEAR)

    assert stripe.state is SimState.HEALTHY
    assert stripe.injected_auth_rate is None
    assert abs(_observed_auth(stripe, "US:USD:visa") - 0.94) < TOLERANCE


def test_injecting_one_rail_leaves_the_others_alone() -> None:
    sims = build_simulated(random.Random(42), latency_scale=0)
    sims["stripe_sim"].inject(InjectionMode.OUTAGE)

    assert sims["adyen_sim"].state is SimState.HEALTHY
    assert abs(_observed_auth(sims["adyen_sim"], "US:USD:visa") - 0.85) < TOLERANCE


@pytest.mark.parametrize("mode", ["down", "OUTAGE", "", "degrade"])
def test_unknown_injection_mode_raises(mode: str) -> None:
    sims = build_simulated(random.Random(42), latency_scale=0)
    with pytest.raises(ValueError, match="unknown injection mode"):
        sims["stripe_sim"].inject(mode)


@pytest.mark.parametrize("rate", [-0.1, 1.5])
def test_out_of_range_degradation_rate_raises(rate: float) -> None:
    sims = build_simulated(random.Random(42), latency_scale=0)
    with pytest.raises(ValueError, match="auth_rate"):
        sims["stripe_sim"].inject(InjectionMode.DEGRADED, auth_rate=rate)


# --- the adapter contract ----------------------------------------------------


def test_sims_satisfy_the_adapter_protocol_and_are_labeled_simulated() -> None:
    registry = build_registry(random.Random(42), latency_scale=0)

    assert set(registry) == set(CARD_PROVIDERS)
    for provider in registry.values():
        assert isinstance(provider, ProviderAdapter)
        assert provider.real is False
        assert provider.fee_bps > 0


def test_charge_reports_latency_without_spending_it_when_scaled_to_zero() -> None:
    sims = build_simulated(random.Random(42), latency_scale=0)
    outcome = asyncio.run(sims["stripe_sim"].charge(_request()))

    assert outcome.provider == "stripe_sim"
    assert outcome.status in (AttemptStatus.SUCCEEDED, AttemptStatus.FAILED)
    assert outcome.latency_ms >= 15  # drawn and reported, just never slept


def test_charge_actually_sleeps_when_latency_is_live() -> None:
    sims = build_simulated(random.Random(42), latency_scale=1.0)
    stripe = sims["stripe_sim"]

    async def timed() -> tuple[float, int]:
        loop = asyncio.get_running_loop()
        started = loop.time()
        outcome = await stripe.charge(_request())
        return loop.time() - started, outcome.latency_ms

    elapsed, latency_ms = asyncio.run(timed())
    assert elapsed >= latency_ms / 1000 * 0.9


def test_outcome_cascades_only_on_infrastructure_failures() -> None:
    sims = build_simulated(random.Random(42), latency_scale=0)
    stripe = sims["stripe_sim"]
    request = _request()
    outcomes = [stripe.draw(request) for _ in range(500)]

    assert all(not o.cascades for o in outcomes if o.approved)
    assert all(o.cascades == should_cascade(o.decline_code) for o in outcomes if not o.approved)
