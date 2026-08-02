"""The watcher: what makes the agent act unprompted, and what must never happen
when it does."""

from __future__ import annotations

import asyncio
import random

import pytest
from agent_stub import OpenAIStub, completion

from payoptimize import store
from payoptimize.agent import triggers
from payoptimize.engine import Engine
from payoptimize.models import AttemptStatus, PaymentStatus


@pytest.fixture
def engine(db: str) -> Engine:
    built = Engine.build(db_path=db, rng=random.Random(42), latency_scale=0, with_prava=False)
    built.boot()
    return built


def _tenant(db: str) -> int:
    existing = store.tenant_by_email("w@acme.test", db_path=db)
    if existing:
        return int(existing["id"])
    return store.create_tenant_with_key("W", "w@acme.test", "wh", "pok_w…", db_path=db)


def _failed_attempt(db: str, code: str, payment_id: str = "pay_w1") -> None:
    store.insert_payment(
        payment_id=payment_id,
        tenant_id=_tenant(db),
        amount_cents=1250,
        currency="USD",
        country="US",
        card_brand="visa",
        method="card",
        routing_mode="router",
        segment="US:USD:visa",
        status=PaymentStatus.FAILED,
        source="api",
        db_path=db,
    )
    attempt = store.insert_attempt(
        payment_id=payment_id,
        seq=1,
        provider="stripe_sim",
        segment="US:USD:visa",
        status=AttemptStatus.PENDING,
        db_path=db,
    )
    store.resolve_attempt(attempt, status=AttemptStatus.FAILED, decline_code=code, db_path=db)


def _watcher(engine: Engine, **kwargs) -> triggers.TriggerWatcher:
    watcher = triggers.TriggerWatcher(engine=engine, **kwargs)
    watcher.prime()  # start from now, as a real deployment does
    return watcher


# --- what it notices ---------------------------------------------------------


def test_the_first_sweep_diagnoses_nothing(engine: Engine, db: str) -> None:
    """Deploying the agent must not diagnose a month of history in one burst."""
    _failed_attempt(db, "SOMETHING_STRANGE", "pay_old")
    watcher = triggers.TriggerWatcher(engine=engine)
    stub = OpenAIStub([completion(content="should not run")])

    created = asyncio.run(watcher.watch_once(http=stub.client()))

    assert created == []
    assert stub.requests == []


def test_an_unrecognised_decline_code_gets_explained(engine: Engine, db: str) -> None:
    """Exactly how FETCH_AGENTIC_CREDS_ERROR arrived: a rail said something the
    system has no vocabulary for."""
    watcher = _watcher(engine)
    _failed_attempt(db, "FETCH_AGENTIC_CREDS_ERROR")
    stub = OpenAIStub([completion(content="Visa could not mint a cryptogram.")])

    created = asyncio.run(watcher.watch_once(http=stub.client()))

    assert len(created) == 1
    runs = store.recent_agent_runs(kinds=("unknown_decline",), db_path=db)
    assert runs and "cryptogram" in runs[0]["answer"]


@pytest.mark.parametrize("code", ["do_not_honor", "issuer_unavailable", "rail_unavailable", ""])
def test_codes_the_system_understands_are_left_alone(engine: Engine, db: str, code: str) -> None:
    """A decline the taxonomy already covers is not an incident. Narrating every
    routine decline would bury the one that matters."""
    watcher = _watcher(engine)
    _failed_attempt(db, code)
    stub = OpenAIStub([completion(content="should not run")])

    assert asyncio.run(watcher.watch_once(http=stub.client())) == []
    assert stub.requests == []


def test_a_health_transition_gets_a_narrative(engine: Engine, db: str) -> None:
    watcher = _watcher(engine)
    store.insert_provider_event(
        "stripe_sim", "health_degraded", detail="healthy → degraded", db_path=db
    )
    stub = OpenAIStub([completion(content="stripe_sim fell to 45% and traffic moved.")])

    created = asyncio.run(watcher.watch_once(http=stub.client()))

    assert len(created) == 1
    assert store.recent_agent_runs(kinds=("health_event",), db_path=db)


def test_routine_provider_events_are_not_incidents(engine: Engine, db: str) -> None:
    watcher = _watcher(engine)
    store.insert_provider_event("stripe_sim", "degraded_start", detail="operator", db_path=db)
    stub = OpenAIStub([completion(content="should not run")])

    assert asyncio.run(watcher.watch_once(http=stub.client())) == []


def test_a_stranded_payment_is_raised_once_not_every_sweep(engine: Engine, db: str) -> None:
    """In propose mode it stays stranded until a human approves. Re-proposing
    every fifteen seconds would bury the operator this is meant to help."""
    from datetime import UTC, datetime, timedelta

    watcher = _watcher(engine)
    store.insert_payment(
        payment_id="pay_stuck",
        tenant_id=_tenant(db),
        amount_cents=1250,
        currency="USD",
        country="",
        card_brand="",
        method="prava",
        routing_mode="router",
        segment=":USD:prava",
        status=PaymentStatus.PENDING,
        source="agent",
        db_path=db,
    )
    old = (datetime.now(UTC) - timedelta(hours=1)).isoformat(timespec="milliseconds")
    with store.transaction(db) as conn:
        conn.execute("UPDATE payments SET created_ts = ? WHERE id = ?", (old, "pay_stuck"))

    stub = OpenAIStub([completion(content="Abandoned at startup; propose reconcile.")])
    first = asyncio.run(watcher.watch_once(http=stub.client()))
    second = asyncio.run(watcher.watch_once(http=stub.client()))

    assert len(first) == 1
    assert second == []


def test_a_sweep_is_capped(engine: Engine, db: str) -> None:
    """A burst of model calls behind a payments incident is the wrong instinct."""
    watcher = _watcher(engine, max_runs=2)
    for index in range(5):
        _failed_attempt(db, f"WEIRD_CODE_{index}", f"pay_burst{index}")
    stub = OpenAIStub([completion(content="explained") for _ in range(5)])

    created = asyncio.run(watcher.watch_once(http=stub.client()))

    assert len(created) == 2


# --- money-path isolation ----------------------------------------------------


def test_a_model_outage_costs_a_narrative_and_nothing_else(engine: Engine, db: str) -> None:
    """The invariant the whole design rests on."""
    watcher = _watcher(engine)
    _failed_attempt(db, "STRANGE_CODE")
    broken = OpenAIStub([], status=500)

    created = asyncio.run(watcher.watch_once(http=broken.client()))

    assert created == []  # no narrative
    # ...and the payment world is untouched.
    assert store.get_payment("pay_w1", db_path=db)["status"] == PaymentStatus.FAILED
    assert len(store.list_payments(limit=10, db_path=db)) == 1


def test_the_high_water_mark_advances_even_when_the_model_fails(engine: Engine, db: str) -> None:
    """Otherwise a persistent model outage would re-diagnose the same attempt
    for ever, and recovery would arrive as a stampede."""
    watcher = _watcher(engine)
    _failed_attempt(db, "STRANGE_CODE")
    asyncio.run(watcher.watch_once(http=OpenAIStub([], status=500).client()))

    working = OpenAIStub([completion(content="explained")])
    assert asyncio.run(watcher.watch_once(http=working.client())) == []
    assert working.requests == []


def test_triggers_need_both_a_switch_and_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("PAYOPTIMIZE_AGENT_TRIGGERS", "1")
    assert triggers.enabled() is True

    monkeypatch.setenv("PAYOPTIMIZE_AGENT_TRIGGERS", "0")
    assert triggers.enabled() is False

    monkeypatch.setenv("PAYOPTIMIZE_AGENT_TRIGGERS", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    assert triggers.enabled() is False


def test_the_loop_stops_when_cancelled(engine: Engine) -> None:
    """The lifespan cancels this on shutdown; it has to actually die."""
    watcher = triggers.TriggerWatcher(engine=engine, interval=0.01)

    async def run() -> None:
        task = asyncio.create_task(watcher.run())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert watcher.started  # it did sweep before dying
