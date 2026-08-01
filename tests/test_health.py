"""Health classification and the circuit breaker it drives."""

from __future__ import annotations

import random

import pytest

from payoptimize import store
from payoptimize.engine import Engine
from payoptimize.health import (
    DEGRADED_BELOW,
    DOWN_BELOW,
    MIN_ATTEMPTS,
    REAL_MIN_ATTEMPTS,
    REAL_WINDOW_SECONDS,
    STATE_WINDOW_SECONDS,
    HealthMonitor,
    HealthState,
    classify,
    percentile,
)
from payoptimize.models import AttemptStatus, PaymentMethod, PaymentSource, PaymentStatus
from payoptimize.models import RoutingMode as Mode


@pytest.mark.parametrize(
    ("auth_rate", "attempts", "expected"),
    [
        (0.95, 100, HealthState.HEALTHY),
        (0.80, 100, HealthState.HEALTHY),  # a weak corridor is not a sick rail
        (0.66, 100, HealthState.HEALTHY),
        (0.45, 100, HealthState.DEGRADED),  # the injection lands here
        (0.20, 100, HealthState.DEGRADED),
        (0.05, 100, HealthState.DOWN),
        (0.00, 100, HealthState.DOWN),
        (0.00, MIN_ATTEMPTS - 1, HealthState.UNKNOWN),  # no traffic, no opinion
    ],
)
def test_classification(auth_rate: float, attempts: int, expected: str) -> None:
    assert classify(auth_rate, attempts) == expected


def test_thresholds_separate_a_degradation_from_a_weak_corridor() -> None:
    """0.45 is the demo's injected rate; 0.80 is adyen's honest US:USD:visa
    number. A threshold that could not tell them apart would trip constantly."""
    assert DOWN_BELOW < 0.45 < DEGRADED_BELOW < 0.80


@pytest.mark.parametrize(
    ("values", "expected"),
    [([], 0), ([5], 5), (list(range(1, 101)), 95), ([10, 20, 30, 40], 40)],
)
def test_percentile(values: list[int], expected: int) -> None:
    assert percentile(values, 0.95) == expected


def _record(db: str, provider: str, outcomes: list[bool], latency_ms: int = 100) -> None:
    """Write resolved attempts straight to the store, so a health test can pick
    the exact outcome sequence it needs rather than fishing for one."""
    existing = store.tenant_by_email("health@t.test", db_path=db)
    tenant = (
        int(existing["id"])
        if existing
        else store.create_tenant_with_key("T", "health@t.test", "health-hash", "pok_x…", db_path=db)
    )
    offset = len(store.list_payments(limit=500, db_path=db))
    for index, approved in enumerate(outcomes, start=offset):
        payment_id = f"pay_{provider}_{index}"
        store.insert_payment(
            payment_id=payment_id,
            tenant_id=tenant,
            amount_cents=1000,
            currency="USD",
            country="US",
            card_brand="visa",
            method=PaymentMethod.CARD,
            routing_mode=Mode.ROUTER,
            segment="US:USD:visa",
            status=PaymentStatus.SUCCEEDED if approved else PaymentStatus.FAILED,
            source=PaymentSource.GENERATOR,
            db_path=db,
        )
        attempt_id = store.insert_attempt(
            payment_id=payment_id,
            seq=1,
            provider=provider,
            segment="US:USD:visa",
            status=AttemptStatus.PENDING,
            db_path=db,
        )
        store.resolve_attempt(
            attempt_id,
            status=AttemptStatus.SUCCEEDED if approved else AttemptStatus.FAILED,
            decline_code="" if approved else "do_not_honor",
            latency_ms=latency_ms,
            db_path=db,
        )


@pytest.fixture
def monitor(db: str) -> HealthMonitor:
    engine = Engine.build(db_path=db, rng=random.Random(42), latency_scale=0, with_prava=False)
    engine.boot()
    return engine.health


def test_snapshot_labels_every_rail(monitor: HealthMonitor, db: str) -> None:
    _record(db, "stripe_sim", [True] * 20)

    tiles = {tile["name"]: tile for tile in monitor.snapshot()}

    assert set(tiles) == {"stripe_sim", "braintree_sim", "adyen_sim"}
    assert tiles["stripe_sim"]["state"] == HealthState.HEALTHY
    assert tiles["stripe_sim"]["auth_rate"] == 1.0
    assert tiles["stripe_sim"]["p95_ms"] == 100
    # Every simulated rail says so, on every surface it appears on.
    assert all(tile["label"] == "SIMULATED" and tile["real"] is False for tile in tiles.values())


def test_a_rail_with_no_traffic_is_unknown_not_down(monitor: HealthMonitor) -> None:
    tiles = {tile["name"]: tile for tile in monitor.snapshot()}
    assert tiles["adyen_sim"]["state"] == HealthState.UNKNOWN
    assert monitor.unavailable() == set()


def test_a_dead_rail_becomes_unavailable_to_the_router(monitor: HealthMonitor, db: str) -> None:
    _record(db, "stripe_sim", [False] * 30)
    monitor.snapshot()

    assert monitor.unavailable() == {"stripe_sim"}


def test_a_degraded_rail_is_shown_but_not_circuit_broken(monitor: HealthMonitor, db: str) -> None:
    """The bandit routes around a 45% rail better than a threshold can, because
    it knows what the alternatives are worth. The breaker must not fight it."""
    _record(db, "stripe_sim", [True] * 9 + [False] * 11)
    tiles = {tile["name"]: tile for tile in monitor.snapshot()}

    assert tiles["stripe_sim"]["state"] == HealthState.DEGRADED
    assert monitor.unavailable() == set()


def test_transitions_are_logged_once(monitor: HealthMonitor, db: str) -> None:
    _record(db, "stripe_sim", [True] * 20)
    monitor.snapshot()  # establishes healthy
    _record(db, "stripe_sim", [False] * 60)
    monitor.snapshot()  # healthy -> down
    monitor.snapshot()  # no change: must not log again

    events = store.recent_provider_events(since="2000-01-01T00:00:00.000+00:00", db_path=db)
    degraded = [e for e in events if e["kind"] == "health_degraded"]
    assert len(degraded) == 1
    assert degraded[0]["provider"] == "stripe_sim"
    assert "healthy" in degraded[0]["detail"]


# --- the real rail keeps different company -----------------------------------


def _record_prava(db: str, outcomes: list[bool]) -> None:
    _record(db, "prava", outcomes)


@pytest.fixture
def monitor_with_prava(db: str) -> HealthMonitor:
    engine = Engine.build(db_path=db, rng=random.Random(42), latency_scale=0, with_prava=True)
    engine.boot()
    return engine.health


def test_the_real_rail_is_visible_on_a_handful_of_attempts(
    monitor_with_prava: HealthMonitor, db: str
) -> None:
    """Two failed payments were invisible on production: the tile said "no
    traffic yet" because a 5-minute window and an 8-attempt bar were written for
    2 tx/s of synthetic traffic, and a human-paced rail satisfies neither."""
    _record_prava(db, [False, False])

    tiles = {t["name"]: t for t in monitor_with_prava.snapshot()}
    prava = tiles["prava"]

    assert prava["attempts"] == 2  # counted, not lost to a 5-minute window
    assert prava["lifetime_attempts"] == 2
    assert prava["state"] == HealthState.DOWN  # honest: it is not working
    assert prava["last_attempt_ts"]


def test_one_attempt_is_still_not_an_opinion(monitor_with_prava: HealthMonitor, db: str) -> None:
    _record_prava(db, [False])
    tiles = {t["name"]: t for t in monitor_with_prava.snapshot()}

    assert tiles["prava"]["state"] == HealthState.UNKNOWN
    assert tiles["prava"]["lifetime_attempts"] == 1  # visible, just not judged


def test_a_working_real_rail_reads_healthy(monitor_with_prava: HealthMonitor, db: str) -> None:
    _record_prava(db, [True, True, True])
    tiles = {t["name"]: t for t in monitor_with_prava.snapshot()}

    assert tiles["prava"]["state"] == HealthState.HEALTHY
    assert tiles["prava"]["auth_rate"] == 1.0


def test_the_simulated_rails_keep_their_own_thresholds(
    monitor_with_prava: HealthMonitor, db: str
) -> None:
    """The 90-second state window was itself a measured fix. It must not regress
    just because the real rail needed a longer one."""
    assert monitor_with_prava.state_window_seconds == STATE_WINDOW_SECONDS
    assert monitor_with_prava.real_window_seconds == REAL_WINDOW_SECONDS
    assert REAL_MIN_ATTEMPTS < MIN_ATTEMPTS

    _record(db, "stripe_sim", [False] * (MIN_ATTEMPTS - 1))
    tiles = {t["name"]: t for t in monitor_with_prava.snapshot()}
    # Below the sim bar, a simulated rail still has no opinion.
    assert tiles["stripe_sim"]["state"] == HealthState.UNKNOWN


def test_the_real_rail_fee_line_says_something(monitor_with_prava: HealthMonitor) -> None:
    """0bps + 0c rendered as "0.00% + 0¢" reads as a bug or as "this is free"."""
    tiles = {t["name"]: t for t in monitor_with_prava.snapshot()}

    assert tiles["prava"]["fee"] == "sandbox — no processing fee"
    assert "%" in tiles["stripe_sim"]["fee"]  # sims keep real schedules
