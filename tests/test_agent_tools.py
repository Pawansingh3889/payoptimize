"""The agent's eyes: tenant-bound at construction, redacted on the way out."""

from __future__ import annotations

import pytest

from payoptimize import store
from payoptimize.agent.privacy import Redactor, pseudonym
from payoptimize.agent.tools import ToolBox
from payoptimize.engine import Engine
from payoptimize.models import (
    AttemptStatus,
    PaymentMethod,
    PaymentSource,
    PaymentStatus,
    RoutingMode,
    new_payment_id,
)


def _tenant(db: str, name: str, email: str) -> int:
    return store.create_tenant_with_key(name, email, f"hash-{name}", "pok_display…", db_path=db)


def _payment(
    db: str,
    tenant_id: int,
    *,
    status: str = PaymentStatus.SUCCEEDED,
    description: str = "",
    created_ts: str | None = None,
) -> str:
    payment_id = new_payment_id()
    store.insert_payment(
        payment_id=payment_id,
        tenant_id=tenant_id,
        amount_cents=1250,
        currency="USD",
        country="US",
        card_brand="visa",
        method=PaymentMethod.CARD,
        routing_mode=RoutingMode.ROUTER,
        segment="US:USD:visa",
        status=status,
        source=PaymentSource.API,
        description=description,
        db_path=db,
    )
    if created_ts is not None:
        with store.transaction(db) as conn:
            conn.execute(
                "UPDATE payments SET created_ts = ? WHERE id = ?", (created_ts, payment_id)
            )
    return payment_id


@pytest.fixture
def engine(db: str) -> Engine:
    built = Engine.build(db_path=db, latency_scale=0.0, with_prava=False)
    built.boot()
    return built


def _box(engine: Engine, tenant_id: int) -> ToolBox:
    return ToolBox(
        engine=engine,
        tenant_id=tenant_id,
        redactor=Redactor.build(db_path=engine.db_path),
    )


def test_get_payment_is_scoped_and_redacted(engine: Engine, db: str) -> None:
    mine = _tenant(db, "Acme", "ops@acme.test")
    theirs = _tenant(db, "Rival", "x@rival.test")
    my_payment = _payment(db, mine, description="invoice for Acme, card 4242424242424242")
    their_payment = _payment(db, theirs)

    box = _box(engine, mine)
    view = box.dispatch("get_payment", {"payment_id": my_payment})
    assert view["id"] == my_payment
    assert view["tenant"] == pseudonym(mine)
    # The description crossed the boundary redacted, not verbatim.
    assert "Acme" not in view["description"]
    assert "4242424242424242" not in view["description"]

    # A foreign payment is indistinguishable from a nonexistent one.
    foreign = box.dispatch("get_payment", {"payment_id": their_payment})
    missing = box.dispatch("get_payment", {"payment_id": "pay_nope"})
    assert "error" in foreign and "error" in missing


def test_system_scope_reads_across_tenants(engine: Engine, db: str) -> None:
    tenant_id = _tenant(db, "Acme", "ops@acme.test")
    payment_id = _payment(db, tenant_id)
    box = _box(engine, 0)
    assert box.dispatch("get_payment", {"payment_id": payment_id})["id"] == payment_id


def test_list_recent_payments_only_sees_own_tenant(engine: Engine, db: str) -> None:
    mine = _tenant(db, "Acme", "ops@acme.test")
    theirs = _tenant(db, "Rival", "x@rival.test")
    _payment(db, mine)
    _payment(db, theirs)

    listed = _box(engine, mine).dispatch("list_recent_payments", {})["payments"]
    assert [p["tenant"] for p in listed] == [pseudonym(mine)]


def test_stranded_finder_matches_the_reconcile_guard(engine: Engine, db: str) -> None:
    tenant_id = _tenant(db, "Acme", "ops@acme.test")
    old_ts = "2026-07-01T00:00:00.000+00:00"
    stuck = _payment(db, tenant_id, status=PaymentStatus.PENDING, created_ts=old_ts)
    _payment(db, tenant_id, status=PaymentStatus.SUCCEEDED, created_ts=old_ts)

    found = _box(engine, 0).dispatch("find_stranded_payments", {})["stranded"]
    assert [p["id"] for p in found] == [stuck]


def test_provider_health_and_events_answer(engine: Engine, db: str) -> None:
    store.init_db(db)
    store.insert_provider_event("stripe_sim", "degraded_start", detail="auth_rate=0.45", db_path=db)
    box = _box(engine, 0)

    health = box.dispatch("provider_health", {})["providers"]
    assert {tile["name"] for tile in health} == {"stripe_sim", "braintree_sim", "adyen_sim"}

    events = box.dispatch("provider_events", {})["events"]
    assert [e["kind"] for e in events] == ["degraded_start"]


def test_analytics_summary_carries_the_uplift_interval(engine: Engine, db: str) -> None:
    tenant_id = _tenant(db, "Acme", "ops@acme.test")
    _payment(db, tenant_id)
    summary = _box(engine, tenant_id).dispatch("analytics_summary", {"window_minutes": 15})
    assert summary["uplift"]["status"] in ("no_data", "collecting", "measured")
    assert "by_provider" in summary and "by_corridor" in summary


def test_ledger_statement_needs_a_tenant(engine: Engine, db: str) -> None:
    tenant_id = _tenant(db, "Acme", "ops@acme.test")
    box = _box(engine, tenant_id)
    assert box.dispatch("ledger_statement", {})["total_fees_cents"] == 0
    assert "error" in _box(engine, 0).dispatch("ledger_statement", {})


def test_unknown_tools_and_bad_arguments_answer_instead_of_raising(engine: Engine, db: str) -> None:
    store.init_db(db)
    box = _box(engine, 0)
    assert "error" in box.dispatch("drop_tables", {})
    assert "error" in box.dispatch("provider_health", {"bogus": 1})


def test_attempt_chain_rides_along(engine: Engine, db: str) -> None:
    tenant_id = _tenant(db, "Acme", "ops@acme.test")
    payment_id = _payment(db, tenant_id, status=PaymentStatus.FAILED)
    attempt = store.insert_attempt(
        payment_id=payment_id,
        seq=1,
        provider="stripe_sim",
        segment="US:USD:visa",
        status=AttemptStatus.PENDING,
        db_path=db,
    )
    store.resolve_attempt(
        attempt, status=AttemptStatus.FAILED, decline_code="do_not_honor", db_path=db
    )
    view = _box(engine, tenant_id).dispatch("get_payment", {"payment_id": payment_id})
    assert view["attempts"] == [
        {
            "seq": 1,
            "provider": "stripe_sim",
            "status": "failed",
            "decline_code": "do_not_honor",
            "latency_ms": 0,
        }
    ]
