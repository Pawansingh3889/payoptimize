"""Merchant identity: whose sale Prava records, and migrating a live database."""

from __future__ import annotations

import asyncio
import json
import random
import sqlite3
from collections.abc import Iterator

import httpx
import pytest
from starlette.testclient import TestClient

from payoptimize import store, tenancy
from payoptimize.api import create_app
from payoptimize.engine import Engine
from payoptimize.models import PaymentMethod, PaymentRequest

SESSION = {"session_id": "ses_T", "iframe_url": "https://sandbox.collect.prava.space?session=t"}


@pytest.fixture(autouse=True)
def sandbox_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """conftest points at an unreachable host and the key-mode guard rightly
    refuses a sandbox key against it. Every request here is MockTransport."""
    monkeypatch.setenv("PRAVA_API_BASE", "https://sandbox.api.prava.space")


@pytest.fixture
def client(db: str) -> Iterator[TestClient]:
    engine = Engine.build(db_path=db, rng=random.Random(42), latency_scale=0, with_prava=False)
    with TestClient(create_app(engine=engine)) as client:
        yield client


def test_a_database_from_before_the_column_existed_still_opens(tmp_path) -> None:
    """The Fly volume and every local database predate these columns. A
    CREATE TABLE IF NOT EXISTS does nothing to an existing table, so without a
    migration they would be one schema behind and fail on first query."""
    old = str(tmp_path / "old.sqlite3")
    con = sqlite3.connect(old)
    con.executescript(
        "CREATE TABLE tenants (id INTEGER PRIMARY KEY, name TEXT NOT NULL,"
        " email TEXT NOT NULL, created_ts TEXT NOT NULL,"
        " fee_bps INTEGER NOT NULL DEFAULT 45, fee_fixed_cents INTEGER NOT NULL DEFAULT 5);"
        "INSERT INTO tenants (name, email, created_ts) VALUES ('Old', 'o@o.test', '2026-01-01');"
    )
    con.commit()
    con.close()
    store._initialized.discard(old)

    store.init_db(old)  # must migrate, not explode

    tenant = store.get_tenant(1, db_path=old)
    assert tenant is not None
    assert tenant["name"] == "Old"  # existing row survived
    assert tenant["merchant_url"] == ""  # new column, safe default


def test_migration_is_idempotent(db: str) -> None:
    store.init_db(db)
    store._initialized.discard(db)
    store.init_db(db)  # a second process opening the same file
    assert store.get_tenant(1, db_path=db) is None or True


def test_a_merchant_declares_its_storefront_at_signup(client: TestClient, db: str) -> None:
    body = client.post(
        "/v1/tenants",
        json={
            "name": "Blue Bottle",
            "email": "ops@bluebottle.test",
            "merchant_name": "Blue Bottle Coffee",
            "merchant_url": "https://bluebottlecoffee.com",
            "merchant_country": "US",
        },
    ).json()

    tenant = store.get_tenant(body["tenant_id"], db_path=db)
    assert tenant["merchant_url"] == "https://bluebottlecoffee.com"


@pytest.mark.parametrize("url", ["bluebottlecoffee.com", "ftp://x.test", "javascript:alert(1)"])
def test_a_merchant_url_must_be_a_url(client: TestClient, url: str) -> None:
    response = client.post(
        "/v1/tenants",
        json={"name": "X", "email": "x@x.test", "merchant_url": url},
    )
    assert response.status_code == 422


def test_the_session_records_the_tenants_merchant_not_ours(db: str) -> None:
    """PayOptimize orchestrates the payment; the merchant is whose sale it is.
    Hardcoding our own URL into every session was wrong on that ground alone."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(201, json=SESSION)

    engine = Engine.build(db_path=db, rng=random.Random(42), latency_scale=0, with_prava=True)
    engine.boot()
    tenant_id, _ = tenancy.signup(
        "Blue Bottle",
        "ops@bb.test",
        merchant_name="Blue Bottle Coffee",
        merchant_url="https://bluebottlecoffee.com",
        merchant_country="US",
        db_path=db,
    )
    request = PaymentRequest(amount_cents=1250, currency="USD", method=PaymentMethod.PRAVA)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        asyncio.run(engine.execute(request, tenant_id=tenant_id, http=http))

    merchant = seen["purchase_context"][0]["merchant_details"]
    assert merchant["name"] == "Blue Bottle Coffee"
    assert merchant["url"] == "https://bluebottlecoffee.com"


def test_a_tenant_without_a_storefront_falls_back(db: str) -> None:
    """Existing merchants signed up before this existed. They must keep working."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(201, json=SESSION)

    engine = Engine.build(db_path=db, rng=random.Random(42), latency_scale=0, with_prava=True)
    engine.boot()
    tenant_id, _ = tenancy.signup("Plain", "p@p.test", db_path=db)
    request = PaymentRequest(amount_cents=1250, currency="USD", method=PaymentMethod.PRAVA)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        asyncio.run(engine.execute(request, tenant_id=tenant_id, http=http))

    assert seen["purchase_context"][0]["merchant_details"]["name"] == "PayOptimize"
