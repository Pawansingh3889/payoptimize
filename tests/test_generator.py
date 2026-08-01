"""The generator, tested at the tick — never by waiting for the loop."""

from __future__ import annotations

import asyncio
import random
from collections import Counter

import pytest

from payoptimize import tenancy
from payoptimize.engine import Engine
from payoptimize.generator import CORRIDOR_MIX, Generator
from payoptimize.models import PaymentSource, RoutingMode


@pytest.fixture
def generator(db: str) -> Generator:
    engine = Engine.build(db_path=db, rng=random.Random(42), latency_scale=0)
    engine.boot()
    tenant_id = tenancy.ensure_demo_tenant(db_path=db)
    return Generator(engine=engine, tenant_id=tenant_id, rng=random.Random(42), tps=100)


def test_corridor_weights_sum_to_one() -> None:
    assert sum(weight for _, weight in CORRIDOR_MIX) == pytest.approx(1.0)


def test_requests_follow_the_corridor_mix(generator: Generator) -> None:
    counts = Counter(generator.next_request().segment for _ in range(4_000))

    assert len(counts) == len(CORRIDOR_MIX)
    assert counts["US:USD:visa"] / 4_000 == pytest.approx(0.40, abs=0.03)
    assert counts["FR:EUR:mastercard"] / 4_000 == pytest.approx(0.10, abs=0.03)


def test_requests_are_deterministic_under_a_seed(db: str) -> None:
    def run() -> list[tuple[int, str]]:
        engine = Engine.build(db_path=db, rng=random.Random(1), latency_scale=0)
        gen = Generator(engine=engine, tenant_id=1, rng=random.Random(7), tps=1)
        return [(r.amount_cents, r.segment) for r in (gen.next_request() for _ in range(30))]

    assert run() == run()


def test_a_tick_writes_one_payment_labeled_generator(generator: Generator) -> None:
    response = asyncio.run(generator.tick())

    assert response.source is PaymentSource.GENERATOR
    assert response.status in ("succeeded", "failed")
    assert generator.created == 1


def test_generator_traffic_splits_across_both_arms(generator: Generator) -> None:
    """The A/B split is what makes the uplift measurable — if the generator
    only ever produced router traffic there would be no control."""

    async def run() -> list[RoutingMode]:
        return [(await generator.tick()).routing_mode for _ in range(200)]

    modes = Counter(asyncio.run(run()))

    assert modes[RoutingMode.ROUTER] > 60
    assert modes[RoutingMode.BASELINE] > 60


def test_rate_changes_are_validated(generator: Generator) -> None:
    generator.set_rate(5.0)
    assert generator.tps == 5.0

    for bad in (0, -1, 1_000):
        with pytest.raises(ValueError, match="tps"):
            generator.set_rate(bad)


def test_the_loop_stops_when_cancelled(generator: Generator) -> None:
    """The lifespan cancels this task on shutdown; it has to actually die."""

    async def run() -> int:
        task = asyncio.create_task(generator.run())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return generator.created

    assert asyncio.run(run()) > 0


def test_a_disabled_generator_creates_nothing(generator: Generator) -> None:
    generator.enabled = False

    async def run() -> int:
        task = asyncio.create_task(generator.run())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return generator.created

    assert asyncio.run(run()) == 0


def test_the_demo_tenant_is_found_not_recreated(db: str) -> None:
    """A restart must not leave a second demo tenant behind, or the fee meter
    splits across two rows every deploy."""
    first = tenancy.ensure_demo_tenant(db_path=db)
    second = tenancy.ensure_demo_tenant(db_path=db)
    assert first == second
