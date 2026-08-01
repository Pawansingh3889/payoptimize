"""Fees: the arithmetic, and the rule that only authorizations are billable."""

from __future__ import annotations

import random
from collections.abc import Iterator

import pytest
from starlette.testclient import TestClient

from payoptimize import billing, store
from payoptimize.api import create_app
from payoptimize.engine import Engine

CARD = {"amount_cents": 1250, "currency": "USD", "country": "US", "card_brand": "visa"}


@pytest.fixture
def client(db: str) -> Iterator[TestClient]:
    engine = Engine.build(db_path=db, rng=random.Random(42), latency_scale=0, with_prava=False)
    with TestClient(create_app(engine=engine)) as client:
        yield client


@pytest.fixture
def merchant(client: TestClient) -> dict:
    body = client.post("/v1/tenants", json={"name": "Acme", "email": "ops@acme.test"}).json()
    return {
        "id": body["tenant_id"],
        "key": body["api_key"],
        "auth": {"Authorization": f"Bearer {body['api_key']}"},
    }


# --- the arithmetic ----------------------------------------------------------


@pytest.mark.parametrize(
    ("amount", "bps", "fixed", "expected"),
    [
        (1_250, 45, 5, 11),  # 5.625c -> 6c, + 5c fixed
        (10_000, 45, 5, 50),  # 45c + 5c
        (100, 45, 5, 5),  # 0.45c -> 0c, + 5c
        (0, 45, 5, 5),  # fixed applies even at zero
        (1_000_000, 45, 5, 4_505),
        (1_250, 0, 0, 0),  # a free tier is expressible
        (1_250, 290, 30, 66),  # a card-network-like schedule
    ],
)
def test_fee_is_fixed_plus_basis_points(amount: int, bps: int, fixed: int, expected: int) -> None:
    assert billing.fee_for(amount, fee_bps=bps, fee_fixed_cents=fixed) == expected


def test_half_a_cent_always_rounds_up() -> None:
    """Python's round() is half-to-even, so a half-cent would land differently
    depending on parity. Merchants reconcile these by hand; "sometimes up,
    sometimes down" is a support ticket."""
    # These are the cases where the two rules actually disagree: an exact half
    # cent landing on an even number. round() gives 4 and 22; a merchant adding
    # up 45bps by hand gives 5 and 23.
    for amount, expected in ((1_000, 5), (5_000, 23)):
        exact = amount * 45 / 10_000
        assert exact % 1 == 0.5, "this case must be an exact half to prove anything"
        assert round(exact) == expected - 1  # what Python would have said
        assert billing.fee_for(amount, fee_bps=45, fee_fixed_cents=0) == expected

    # And ordinary rounding is unaffected either way.
    assert billing.fee_for(1_111, fee_bps=45, fee_fixed_cents=0) == 5  # 4.9995
    assert billing.fee_for(111, fee_bps=45, fee_fixed_cents=0) == 0  # 0.4995


def test_a_negative_amount_is_refused() -> None:
    with pytest.raises(ValueError, match="negative"):
        billing.fee_for(-100, fee_bps=45, fee_fixed_cents=5)


# --- what gets billed --------------------------------------------------------


def test_a_ledger_line_lands_for_every_authorization(
    client: TestClient, merchant: dict, db: str
) -> None:
    created = [
        client.post("/v1/payments", json=CARD, headers=merchant["auth"]).json() for _ in range(40)
    ]
    succeeded = [p for p in created if p["status"] == "succeeded"]
    failed = [p for p in created if p["status"] == "failed"]
    assert succeeded and failed, "need both outcomes for this test to mean anything"

    totals = store.ledger_totals(tenant_id=merchant["id"], db_path=db)

    assert int(totals["entries"]) == len(succeeded)
    expected = sum(
        billing.fee_for(p["amount_cents"], fee_bps=45, fee_fixed_cents=5) for p in succeeded
    )
    assert int(totals["total_cents"]) == expected


def test_declines_are_never_charged(client: TestClient, merchant: dict, db: str) -> None:
    """Billing for a payment we failed to get through would invert the entire
    claim the product makes."""
    created = [
        client.post("/v1/payments", json=CARD, headers=merchant["auth"]).json() for _ in range(40)
    ]
    failed = {p["id"] for p in created if p["status"] == "failed"}
    assert failed

    billed = {e["payment_id"] for e in store.ledger_entries(tenant_id=merchant["id"], db_path=db)}
    assert not (billed & failed)


def test_metering_twice_charges_once(client: TestClient, merchant: dict, db: str) -> None:
    """Both the cascade and the Prava poller settle payments, and a restart can
    replay either. Double-charging is the worst bug this product could ship."""
    payment = client.post("/v1/payments", json=CARD, headers=merchant["auth"]).json()
    if payment["status"] != "succeeded":
        pytest.skip("needed an authorized payment")

    before = store.ledger_totals(tenant_id=merchant["id"], db_path=db)
    assert billing.record_fee(payment["id"], db_path=db) is None  # already metered
    billing.record_fee(payment["id"], db_path=db)
    after = store.ledger_totals(tenant_id=merchant["id"], db_path=db)

    assert before == after


def test_metering_an_unknown_payment_is_a_no_op(db: str) -> None:
    assert billing.record_fee("pay_nope", db_path=db) is None


# --- the statement -----------------------------------------------------------


def test_ledger_endpoint_reports_fees_and_pricing(client: TestClient, merchant: dict) -> None:
    for _ in range(20):
        client.post("/v1/payments", json=CARD, headers=merchant["auth"])

    body = client.get("/v1/ledger", headers=merchant["auth"]).json()

    assert body["billed_payments"] > 0
    assert body["total_fees_cents"] > 0
    assert body["entries"]
    assert body["entries"][0]["kind"] == "txn_fee"
    assert body["pricing"]["fee_bps"] == 45
    assert "declines are never charged" in body["pricing"]["description"]


def test_the_total_counts_every_line_not_just_the_page(client: TestClient, merchant: dict) -> None:
    """A statement whose total only reflects the visible rows is worse than one
    with no total at all."""
    for _ in range(30):
        client.post("/v1/payments", json=CARD, headers=merchant["auth"])

    paged = client.get("/v1/ledger?limit=2", headers=merchant["auth"]).json()
    full = client.get("/v1/ledger", headers=merchant["auth"]).json()

    assert len(paged["entries"]) == 2
    assert paged["total_fees_cents"] == full["total_fees_cents"]
    assert paged["billed_payments"] == full["billed_payments"]


def test_the_ledger_needs_a_key(client: TestClient) -> None:
    assert client.get("/v1/ledger").status_code == 401


def test_one_merchant_cannot_see_anothers_fees(client: TestClient, merchant: dict) -> None:
    for _ in range(10):
        client.post("/v1/payments", json=CARD, headers=merchant["auth"])
    other = client.post("/v1/tenants", json={"name": "Rival", "email": "r@rival.test"}).json()

    body = client.get("/v1/ledger", headers={"Authorization": f"Bearer {other['api_key']}"}).json()

    assert body["entries"] == []
    assert body["total_fees_cents"] == 0


def test_the_merchant_strip_meters_fees(client: TestClient, merchant: dict) -> None:
    for _ in range(20):
        client.post("/v1/payments", json=CARD, headers=merchant["auth"])

    html = client.get("/fragments/tenants").text

    assert "Acme" in html
    assert "in fees" in html
    assert "@" not in html  # still no email on a public page
