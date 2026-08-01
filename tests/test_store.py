"""The storage layer: pragmas that are actually on, a schema that survives
being applied twice, and a payment that carries its cascade."""

from __future__ import annotations

import sqlite3

import pytest

from payoptimize import store
from payoptimize.models import (
    AttemptStatus,
    PaymentMethod,
    PaymentSource,
    PaymentStatus,
    RoutingMode,
    new_payment_id,
    segment_key,
)

TABLES = {"tenants", "api_keys", "payments", "attempts", "provider_events", "ledger"}
INDEXES = {
    "idx_payments_created",
    "idx_payments_tenant",
    "idx_attempts_payment",
    "idx_attempts_provider_ts",
    "idx_ledger_tenant",
}


def _names(db: str, kind: str) -> set[str]:
    with store.transaction(db) as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = ?", (kind,)).fetchall()
    return {row["name"] for row in rows}


def _tenant(db: str, name: str = "Acme", email: str = "ops@acme.test") -> int:
    return store.create_tenant_with_key(name, email, f"hash-{name}", "pok_test1234…", db_path=db)


def test_schema_init_is_idempotent(db: str) -> None:
    store.init_db(db)
    store.init_db(db)
    # A second process opening the same file re-runs the schema from scratch.
    store._initialized.discard(db)
    store.init_db(db)

    assert TABLES <= _names(db, "table")


def test_indexes_exist(db: str) -> None:
    store.init_db(db)
    assert INDEXES <= _names(db, "index")


def test_pragmas_are_set(db: str) -> None:
    with store.transaction(db) as conn:
        journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
        synchronous = conn.execute("PRAGMA synchronous").fetchone()[0]
        busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]

    assert journal.lower() == "wal"
    assert synchronous == 1  # NORMAL
    assert busy_timeout == 5000
    assert foreign_keys == 1


def test_payment_and_cascade_round_trip(db: str) -> None:
    tenant_id = _tenant(db)
    payment_id = new_payment_id()
    segment = segment_key("US", "USD", "visa")

    created = store.insert_payment(
        payment_id=payment_id,
        tenant_id=tenant_id,
        amount_cents=1250,
        currency="USD",
        country="US",
        card_brand="visa",
        method=PaymentMethod.CARD,
        routing_mode=RoutingMode.ROUTER,
        segment=segment,
        status=PaymentStatus.PENDING,
        source=PaymentSource.API,
        description="Pro plan",
        db_path=db,
    )
    assert created["amount_cents"] == 1250
    assert created["resolved_ts"] == ""

    first = store.insert_attempt(
        payment_id=payment_id,
        seq=1,
        provider="stripe_sim",
        segment=segment,
        status=AttemptStatus.PENDING,
        db_path=db,
    )
    store.resolve_attempt(
        first,
        status=AttemptStatus.FAILED,
        decline_code="issuer_unavailable",
        latency_ms=180,
        db_path=db,
    )
    second = store.insert_attempt(
        payment_id=payment_id,
        seq=2,
        provider="adyen_sim",
        segment=segment,
        status=AttemptStatus.PENDING,
        db_path=db,
    )
    store.resolve_attempt(second, status=AttemptStatus.SUCCEEDED, latency_ms=240, db_path=db)
    store.finalize_payment(
        payment_id, status=PaymentStatus.SUCCEEDED, final_provider="adyen_sim", db_path=db
    )

    payment = store.get_payment(payment_id, db_path=db)
    assert payment is not None
    assert payment["status"] == PaymentStatus.SUCCEEDED
    assert payment["final_provider"] == "adyen_sim"
    assert payment["resolved_ts"] != ""

    attempts = store.attempts_for_payment(payment_id, db_path=db)
    assert [(a["seq"], a["provider"], a["status"]) for a in attempts] == [
        (1, "stripe_sim", AttemptStatus.FAILED),
        (2, "adyen_sim", AttemptStatus.SUCCEEDED),
    ]
    assert attempts[0]["decline_code"] == "issuer_unavailable"
    assert attempts[1]["decline_code"] == ""


def test_list_payments_filters_by_tenant_and_status(db: str) -> None:
    mine = _tenant(db, "Mine", "a@mine.test")
    theirs = _tenant(db, "Theirs", "b@theirs.test")
    for tenant_id, status in ((mine, PaymentStatus.SUCCEEDED), (mine, PaymentStatus.FAILED)):
        store.insert_payment(
            payment_id=new_payment_id(),
            tenant_id=tenant_id,
            amount_cents=500,
            currency="USD",
            country="US",
            card_brand="visa",
            method=PaymentMethod.CARD,
            routing_mode=RoutingMode.ROUTER,
            segment="US:USD:visa",
            status=status,
            source=PaymentSource.GENERATOR,
            db_path=db,
        )
    store.insert_payment(
        payment_id=new_payment_id(),
        tenant_id=theirs,
        amount_cents=500,
        currency="USD",
        country="US",
        card_brand="visa",
        method=PaymentMethod.CARD,
        routing_mode=RoutingMode.ROUTER,
        segment="US:USD:visa",
        status=PaymentStatus.SUCCEEDED,
        source=PaymentSource.GENERATOR,
        db_path=db,
    )

    assert len(store.list_payments(tenant_id=mine, db_path=db)) == 2
    assert len(store.list_payments(tenant_id=theirs, db_path=db)) == 1
    succeeded = store.list_payments(tenant_id=mine, status=PaymentStatus.SUCCEEDED, db_path=db)
    assert len(succeeded) == 1
    assert len(store.list_payments(tenant_id=mine, limit=1, db_path=db)) == 1


def test_foreign_keys_are_enforced(db: str) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        store.insert_payment(
            payment_id=new_payment_id(),
            tenant_id=9999,  # no such tenant
            amount_cents=100,
            currency="USD",
            country="US",
            card_brand="visa",
            method=PaymentMethod.CARD,
            routing_mode=RoutingMode.ROUTER,
            segment="US:USD:visa",
            status=PaymentStatus.PENDING,
            source=PaymentSource.API,
            db_path=db,
        )


def test_updates_to_missing_rows_fail_loudly(db: str) -> None:
    store.init_db(db)
    with pytest.raises(store.NotFoundError):
        store.finalize_payment("pay_missing", status=PaymentStatus.SUCCEEDED, db_path=db)
    with pytest.raises(store.NotFoundError):
        store.resolve_attempt(4242, status=AttemptStatus.SUCCEEDED, db_path=db)
    with pytest.raises(store.NotFoundError):
        store.set_payment_status("pay_missing", PaymentStatus.FAILED, db_path=db)


def test_rollback_leaves_no_partial_write(db: str) -> None:
    tenant_id = _tenant(db)
    payment_id = new_payment_id()
    with pytest.raises(RuntimeError, match="boom"), store.transaction(db) as conn:
        conn.execute(
            "INSERT INTO payments (id, tenant_id, amount_cents, currency, country,"
            " card_brand, method, routing_mode, segment, status, source, created_ts)"
            " VALUES (?, ?, 1, 'USD', 'US', 'visa', 'card', 'router', 'US:USD:visa',"
            " 'pending', 'api', '2026-08-01T00:00:00.000+00:00')",
            (payment_id, tenant_id),
        )
        raise RuntimeError("boom")

    assert store.get_payment(payment_id, db_path=db) is None
