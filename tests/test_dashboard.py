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
    engine = Engine.build(db_path=db, rng=random.Random(42), latency_scale=0, with_prava=False)
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


# --- panel 3: provider mix ---------------------------------------------------


def _mix(buckets: int = 6) -> list[dict]:
    rows = []
    for i in range(buckets):
        for provider, n in (("stripe_sim", 12), ("adyen_sim", 6), ("braintree_sim", 2)):
            rows.append({"bucket_ts": 1_000 + i * 30, "provider": provider, "attempts": n})
    return rows


def test_mix_chart_stacks_every_rail() -> None:
    svg = dashboard.render_mix_chart(_mix())

    assert svg.count("<path") == 3  # one band per rail
    assert "var(--rail-1)" in svg and "var(--rail-3)" in svg
    assert "stroke-dasharray" not in svg


def test_mix_bands_are_directly_labeled_not_colour_alone() -> None:
    """The validator puts the third rail hue at 2.74:1 on the light surface,
    below 3:1. The relief rule for that is visible labels — so identity must
    never rest on colour here."""
    svg = dashboard.render_mix_chart(_mix())

    for rail in ("stripe", "adyen", "braintree"):
        assert rail in svg  # in the band label and the legend
    assert svg.count("swatch") == 3


def test_mix_chart_needs_two_buckets_before_it_draws() -> None:
    single = [{"bucket_ts": 1_000, "provider": "stripe_sim", "attempts": 5}]
    assert "Collecting traffic" in dashboard.render_mix_chart(single)


def test_mix_shows_a_shift_when_one_rail_takes_over() -> None:
    """The demo beat: traffic slides off a degraded rail."""
    rows = []
    for i in range(6):
        stripe = 12 if i < 3 else 1
        adyen = 2 if i < 3 else 13
        rows.append({"bucket_ts": 1_000 + i * 30, "provider": "stripe_sim", "attempts": stripe})
        rows.append({"bucket_ts": 1_000 + i * 30, "provider": "adyen_sim", "attempts": adyen})

    svg = dashboard.render_mix_chart(rows)
    assert svg.count("<path") == 2
    assert "adyen" in svg


# --- panel 5: corridors ------------------------------------------------------


def test_corridor_table_shows_the_delta() -> None:
    html = dashboard.render_corridors(
        [
            {
                "segment": "US:USD:visa",
                "volume": 120,
                "router_auth_rate": 0.94,
                "baseline_auth_rate": 0.87,
            }
        ]
    )
    assert "US:USD:visa" in html
    assert "94.0%" in html
    assert "+7.0" in html


def test_corridor_table_admits_a_missing_arm() -> None:
    """One arm with no traffic is not a zero-point delta."""
    html = dashboard.render_corridors(
        [{"segment": "DE:EUR:visa", "volume": 10, "router_auth_rate": 0.9}]
    )
    assert "&mdash;" in html


def test_corridor_table_empty() -> None:
    assert "No corridor traffic" in dashboard.render_corridors([])


# --- panel 7: merchants ------------------------------------------------------


def test_tenant_strip_meters_volume_without_leaking_identity() -> None:
    html = dashboard.render_tenants(
        [
            {"name": "Acme Corp", "volume": 40, "succeeded": 36, "authorized_cents": 45000},
            {"name": "Rival Ltd", "volume": 10, "succeeded": 9, "authorized_cents": 9000},
        ]
    )
    assert "Acme Corp" in html
    assert "40 payments" in html
    assert "$450" in html
    assert "@" not in html  # no email ever reaches this page


def test_tenant_strip_empty() -> None:
    assert "No merchant traffic" in dashboard.render_tenants([])


# --- the assembled page ------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["stats", "authchart", "mixchart", "tiles", "corridors", "tenants", "feed"]
)
def test_every_panel_has_a_fragment(client: TestClient, name: str) -> None:
    assert client.get(f"/fragments/{name}").status_code == 200


def test_the_page_renders_all_seven_panels(client: TestClient, merchant: dict) -> None:
    for _ in range(12):
        client.post("/v1/payments", json=CARD, headers=merchant["auth"])

    body = client.get("/").text

    for anchor in ("stats", "authchart", "mixchart", "tiles", "corridors", "tenants", "feed"):
        assert f'id="{anchor}"' in body
        # Every panel placeholder was substituted — a missing one renders the
        # literal "{mixchart}" and is invisible in a screenshot review.
        assert "{" + anchor + "}" not in body


def test_hero_row_reports_throughput(client: TestClient, merchant: dict) -> None:
    client.post("/v1/payments", json=CARD, headers=merchant["auth"])
    assert "Throughput" in client.get("/fragments/stats").text


def test_a_bands_label_and_its_tooltip_agree() -> None:
    """They answer the same question, so they must give the same number. An
    earlier version labelled the peak bucket while the tooltip gave the window
    average — 80% against 46% for one band."""
    import re

    rows = []
    for i in range(6):
        stripe = 18 if i < 3 else 2
        adyen = 2 if i < 3 else 18
        rows.append({"bucket_ts": 1_000 + i * 30, "provider": "stripe_sim", "attempts": stripe})
        rows.append({"bucket_ts": 1_000 + i * 30, "provider": "adyen_sim", "attempts": adyen})

    svg = dashboard.render_mix_chart(rows)
    labels = dict(re.findall(r'font-weight="600"[^>]*>(\w+) (\d+)%<', svg))
    tooltips = dict(re.findall(r"<title>(\w+)_sim: (\d+)% of first attempts</title>", svg))

    assert labels and tooltips
    assert labels == tooltips


# --- the real rail must stay visible -----------------------------------------


def test_no_traffic_yet_is_reserved_for_rails_never_used() -> None:
    """It was being said about a rail that had settled two real payments."""
    never = {
        "display_name": "Prava",
        "real": True,
        "state": "unknown",
        "attempts": 0,
        "lifetime_attempts": 0,
        "last_attempt_ts": "",
        "auth_rate": 0.0,
        "p95_ms": 0,
        "fee": "sandbox — no processing fee",
    }
    assert "no traffic yet" in dashboard.render_tiles([never])


def test_a_rail_with_older_traffic_says_when_not_never() -> None:
    from datetime import UTC, datetime, timedelta

    used = {
        "display_name": "Prava",
        "real": True,
        "state": "down",
        "attempts": 0,
        "lifetime_attempts": 2,
        "last_attempt_ts": (datetime.now(UTC) - timedelta(minutes=41)).isoformat(
            timespec="milliseconds"
        ),
        "auth_rate": 0.0,
        "p95_ms": 0,
        "fee": "sandbox — no processing fee",
    }
    html = dashboard.render_tiles([used])

    assert "no traffic yet" not in html
    assert "2 attempts" in html
    assert "41 min ago" in html


def test_a_broken_timestamp_says_less_rather_than_something_wrong() -> None:
    tile = {
        "display_name": "Prava",
        "real": True,
        "state": "down",
        "attempts": 0,
        "lifetime_attempts": 1,
        "last_attempt_ts": "not-a-timestamp",
        "auth_rate": 0.0,
        "p95_ms": 0,
        "fee": "—",
    }
    html = dashboard.render_tiles([tile])
    assert "1 attempt" in html
    assert "no traffic yet" not in html


def test_the_real_fee_line_renders() -> None:
    tile = {
        "display_name": "Prava",
        "real": True,
        "state": "unknown",
        "attempts": 0,
        "lifetime_attempts": 0,
        "last_attempt_ts": "",
        "auth_rate": 0.0,
        "p95_ms": 0,
        "fee": "sandbox — no processing fee",
    }
    assert "no processing fee" in dashboard.render_tiles([tile])
    assert "0.00%" not in dashboard.render_tiles([tile])


def _payment(pid: str, method: str = "card", ts: str = "2026-08-01T21:44:18.000+00:00") -> dict:
    return {
        "id": pid,
        "method": method,
        "status": "succeeded",
        "amount_cents": 1250,
        "currency": "USD",
        "segment": "US:USD:visa",
        "routing_mode": "router",
        "created_ts": ts,
        "attempts": [],
    }


def test_a_real_row_survives_a_feed_full_of_generator_traffic() -> None:
    """At 2 tx/s the rolling feed spans about nine seconds, so an unpinned real
    payment is gone almost as soon as it lands."""
    rolling = [_payment(f"pay_gen{i}") for i in range(18)]
    real = [_payment("pay_real", method="prava")]

    html = dashboard.render_feed(rolling, real)

    assert html.count("badge real") == 1
    assert "pay_real" not in html  # ids are not rendered, but the row is
    assert 'class="pinned"' in html
    assert "Real rail" in html


def test_a_pinned_payment_is_not_also_listed_below() -> None:
    real = _payment("pay_real", method="prava")
    html = dashboard.render_feed([real, _payment("pay_gen")], [real])

    assert html.count("badge real") == 1  # once, not twice
    assert html.count("<tr class=") == 2  # body rows only; <thead> has its own


def test_the_feed_is_unchanged_when_nothing_is_pinned() -> None:
    html = dashboard.render_feed([_payment("pay_a")], [])
    assert "Real rail" not in html
    assert 'class="pinned"' not in html
    assert "<table" in html


def test_an_empty_feed_still_says_so() -> None:
    assert "No payments yet" in dashboard.render_feed([], [])


def test_the_live_feed_pins_prava_rows(db: str) -> None:
    """End to end through the app: a prava payment plus enough card traffic to
    push it off the rolling feed."""
    engine = Engine.build(db_path=db, rng=random.Random(42), latency_scale=0, with_prava=False)
    with TestClient(create_app(engine=engine)) as client:
        body = client.post("/v1/tenants", json={"name": "A", "email": "a@a.test"}).json()
        auth = {"Authorization": f"Bearer {body['api_key']}"}
        store.insert_payment(
            payment_id="pay_realone",
            tenant_id=body["tenant_id"],
            amount_cents=1250,
            currency="USD",
            country="",
            card_brand="",
            method="prava",
            routing_mode="router",
            segment=":USD:prava",
            status="failed",
            source="agent",
            db_path=db,
        )
        for _ in range(25):
            client.post("/v1/payments", json=CARD, headers=auth)

        feed = client.get("/fragments/feed").text

    assert feed.count("badge real") == 1
    assert "Real rail" in feed


# --- the incidents panel -----------------------------------------------------


def test_incidents_panel_is_empty_before_the_agent_speaks(client: TestClient) -> None:
    body = client.get("/fragments/incidents").text
    assert "No incidents" in body


def test_incidents_render_the_narrative(db: str) -> None:
    runs = [
        {
            "trigger_kind": "unknown_decline",
            "answer": "Visa refused to mint a cryptogram for tenant_3's card.",
            "ts": "2026-08-02T01:20:11.000+00:00",
        }
    ]
    html = dashboard.render_incidents(runs)

    assert "cryptogram" in html
    assert "decline" in html
    assert "01:20:11" in html


def test_a_run_with_no_answer_is_not_shown(db: str) -> None:
    """A run that crashed before answering still leaves an audit row. It is not
    an incident report."""
    runs = [{"trigger_kind": "stranded", "answer": "", "ts": "2026-08-02T01:00:00.000+00:00"}]
    assert "has not written yet" in dashboard.render_incidents(runs)


def test_incidents_escape_model_output(db: str) -> None:
    """The narrative is LLM-generated text on a public page. It is data."""
    runs = [
        {
            "trigger_kind": "health_event",
            "answer": "<img src=x onerror=alert(1)> and <script>alert(2)</script>",
            "ts": "2026-08-02T01:00:00.000+00:00",
        }
    ]
    html = dashboard.render_incidents(runs)

    assert "<script>" not in html
    assert "<img src=x" not in html
    assert "&lt;script&gt;" in html


def test_the_incidents_panel_is_on_the_page(client: TestClient) -> None:
    body = client.get("/").text
    assert 'id="incidents"' in body
    assert "{incidents}" not in body
