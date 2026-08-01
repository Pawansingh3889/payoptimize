"""The judges' page: it renders, it labels the rails honestly, and it never
leaks a secret."""

from __future__ import annotations

import random
import re
from collections.abc import Iterator

import pytest
from starlette.testclient import TestClient

from payoptimize import dashboard, store, tenancy
from payoptimize.api import create_app
from payoptimize.engine import Engine
from payoptimize.models import AttemptStatus, PaymentMethod, PaymentSource, PaymentStatus
from payoptimize.models import RoutingMode as Mode

CARD = {"amount_cents": 1250, "currency": "USD", "country": "US", "card_brand": "visa"}


@pytest.fixture
def client(db: str) -> Iterator[TestClient]:
    engine = Engine.build(db_path=db, rng=random.Random(42), latency_scale=0)
    with TestClient(create_app(engine=engine)) as client:
        yield client


@pytest.fixture
def merchant(client: TestClient) -> dict:
    body = client.post("/v1/tenants", json={"name": "Acme", "email": "ops@acme.test"}).json()
    return {"key": body["api_key"], "auth": {"Authorization": f"Bearer {body['api_key']}"}}


def test_page_renders(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    body = response.text
    assert "PayOptimize" in body
    assert body.count("<html") == 1
    assert "{stats}" not in body  # every placeholder was substituted


def test_page_states_the_honesty_policy(client: TestClient) -> None:
    body = client.get("/").text
    assert "SIMULATED" in body
    assert "REAL" in body


@pytest.mark.parametrize("name", ["stats", "authchart", "tiles", "feed"])
def test_fragments_render(client: TestClient, name: str) -> None:
    response = client.get(f"/fragments/{name}")
    assert response.status_code == 200
    assert response.text.strip()


def test_unknown_fragment_is_a_404(client: TestClient) -> None:
    assert client.get("/fragments/nope").status_code == 404


def test_tiles_label_simulated_rails(client: TestClient) -> None:
    body = client.get("/fragments/tiles").text
    assert body.count("SIMULATED") == 3
    assert "REAL" not in body  # no prava adapter configured yet


def test_feed_shows_the_cascade_chain(client: TestClient, merchant: dict) -> None:
    for _ in range(40):
        client.post("/v1/payments", json=CARD, headers=merchant["auth"])

    body = client.get("/fragments/feed").text

    assert "<table" in body
    assert "&rarr;" in body or "✓" in body or "&check;" in body


def test_the_dashboard_never_renders_a_key_or_an_email(client: TestClient, merchant: dict) -> None:
    """It is a public page. A tenant's identity is not an aggregate."""
    for _ in range(5):
        client.post("/v1/payments", json=CARD, headers=merchant["auth"])

    surfaces = [client.get("/").text] + [
        client.get(f"/fragments/{n}").text for n in ("stats", "authchart", "tiles", "feed")
    ]
    for body in surfaces:
        assert merchant["key"] not in body
        assert "ops@acme.test" not in body
        assert "pok_" not in body


def test_chart_says_so_rather_than_drawing_a_line_through_one_point(
    client: TestClient, merchant: dict
) -> None:
    client.post("/v1/payments", json=CARD, headers=merchant["auth"])
    body = client.get("/fragments/authchart").text
    assert "Collecting traffic" in body


def test_chart_renders_two_series_with_a_legend(db: str, client: TestClient) -> None:
    series = [
        {"bucket_ts": 1_000, "routing_mode": "router", "volume": 20, "succeeded": 19},
        {"bucket_ts": 1_000, "routing_mode": "baseline", "volume": 20, "succeeded": 17},
        {"bucket_ts": 1_030, "routing_mode": "router", "volume": 20, "succeeded": 18},
        {"bucket_ts": 1_030, "routing_mode": "baseline", "volume": 20, "succeeded": 14},
    ]
    svg = dashboard.render_auth_chart(series, [])

    assert svg.count("<path") == 2  # exactly two series, one axis
    assert "Router (learning)" in svg
    assert "Baseline (round-robin)" in svg
    assert "var(--router)" in svg and "var(--baseline)" in svg
    assert "stroke-dasharray" not in svg  # gridlines are solid hairlines
    assert "<title>" in svg  # hover readout without any JavaScript


def test_chart_marks_injections(db: str) -> None:
    series = [
        {"bucket_ts": 1_000, "routing_mode": "router", "volume": 10, "succeeded": 9},
        {"bucket_ts": 1_060, "routing_mode": "router", "volume": 10, "succeeded": 4},
    ]
    events = [{"provider": "stripe_sim", "kind": "degraded_start", "epoch": 1_030}]

    svg = dashboard.render_auth_chart(series, events)
    assert "stripe_sim" in svg
    assert "degraded_start" in svg


def test_chart_endpoint_is_labeled_but_not_every_point() -> None:
    """A number beside every dot is chaos and goes unread."""
    series = [
        {"bucket_ts": 1_000 + i * 30, "routing_mode": "router", "volume": 10, "succeeded": 9}
        for i in range(8)
    ]
    svg = dashboard.render_auth_chart(series, [])

    labels = re.findall(r'font-weight="600"[^>]*>([\d.]+%)<', svg)
    assert len(labels) == 1  # one series, one endpoint label


def test_stats_tile_admits_when_the_uplift_is_not_yet_a_measurement() -> None:
    summary = {
        "volume": 60,
        "auth_rate": 0.9,
        "by_mode": {
            "router": {"volume": 30, "auth_rate": 0.87},
            "baseline": {"volume": 30, "auth_rate": 0.97},
        },
        "uplift": {
            "pts": -10.0,
            "ci95_pts": [-23.8, 3.8],
            "status": "collecting",
            "significant": False,
        },
        "recovered_cents": 0,
    }
    html = dashboard.render_stats(summary)

    assert "-10.0 pts" in html
    assert "collecting" in html


def test_feed_escapes_hostile_content(db: str) -> None:
    """Descriptions come from API callers. The feed renders them."""
    tenant = store.create_tenant_with_key("T", "t@t.test", "hash", "pok_x…", db_path=db)
    store.insert_payment(
        payment_id="pay_xss",
        tenant_id=tenant,
        amount_cents=100,
        currency="USD",
        country="US",
        card_brand="visa",
        method=PaymentMethod.CARD,
        routing_mode=Mode.ROUTER,
        segment='<script>alert("x")</script>',
        status=PaymentStatus.SUCCEEDED,
        source=PaymentSource.API,
        description="<img src=x onerror=alert(1)>",
        db_path=db,
    )
    attempt = store.insert_attempt(
        payment_id="pay_xss",
        seq=1,
        provider="<b>evil</b>",
        segment="US:USD:visa",
        status=AttemptStatus.PENDING,
        db_path=db,
    )
    store.resolve_attempt(attempt, status=AttemptStatus.FAILED, decline_code="<i>", db_path=db)

    html = dashboard.render_feed(store.recent_payments_with_attempts(limit=5, db_path=db))

    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "<b>evil</b>" not in html


def test_demo_tenant_is_not_exposed_by_name(client: TestClient, db: str) -> None:
    tenancy.ensure_demo_tenant(db_path=db)
    assert tenancy.DEMO_TENANT_EMAIL not in client.get("/").text
