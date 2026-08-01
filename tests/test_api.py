"""The REST surface, driven through Starlette's TestClient.

These run the real engine against a real (temporary) database — the sims are
just wound to zero latency so a cascade costs no wall-clock. Nothing here is
mocked except the clock's cost.
"""

from __future__ import annotations

import random
from collections.abc import Iterator

import pytest
from starlette.testclient import TestClient

from payoptimize import store
from payoptimize.api import create_app, uplift_stat
from payoptimize.engine import Engine
from payoptimize.models import PaymentStatus, RoutingMode

CARD = {"amount_cents": 1250, "currency": "USD", "country": "US", "card_brand": "visa"}


@pytest.fixture
def client(db: str) -> Iterator[TestClient]:
    engine = Engine.build(db_path=db, rng=random.Random(42), latency_scale=0)
    with TestClient(create_app(engine=engine)) as client:
        client.app_engine = engine  # type: ignore[attr-defined]
        yield client


@pytest.fixture
def merchant(client: TestClient) -> dict:
    response = client.post("/v1/tenants", json={"name": "Acme", "email": "ops@acme.test"})
    assert response.status_code == 201
    body = response.json()
    return {"id": body["tenant_id"], "key": body["api_key"], "auth": _auth(body["api_key"])}


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


# --- index and signup --------------------------------------------------------


def test_index_describes_itself(client: TestClient) -> None:
    body = client.get("/v1").json()
    assert body["service"] == "payoptimize"
    assert "prava" in body["rails"]
    assert "REAL" in body["rails"]["prava"]
    assert "SIMULATED" in body["rails"]["simulated"]


def test_signup_returns_a_key_once(client: TestClient) -> None:
    response = client.post("/v1/tenants", json={"name": "Acme", "email": "ops@acme.test"})

    assert response.status_code == 201
    body = response.json()
    assert body["api_key"].startswith("pok_")
    assert body["key_prefix"].endswith("…")
    assert body["tenant_id"] > 0


@pytest.mark.parametrize(
    "payload",
    [{"name": "", "email": "a@b.test"}, {"name": "Acme", "email": "nope"}, {"name": "Acme"}],
)
def test_signup_rejects_junk(client: TestClient, payload: dict) -> None:
    assert client.post("/v1/tenants", json=payload).status_code == 422


def test_non_json_body_is_a_422(client: TestClient) -> None:
    response = client.post("/v1/tenants", content=b"not json")
    assert response.status_code == 422
    assert "JSON" in response.json()["error"]


# --- auth --------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer nope"}, {"Authorization": "Basic abc"}, {"Authorization": ""}],
)
def test_payments_require_a_key(client: TestClient, headers: dict) -> None:
    response = client.post("/v1/payments", json=CARD, headers=headers)
    assert response.status_code == 401
    assert "error" in response.json()


def test_revoked_key_is_refused(client: TestClient, merchant: dict, db: str) -> None:
    from payoptimize import tenancy

    tenancy.revoke(merchant["key"], db_path=db)
    assert client.post("/v1/payments", json=CARD, headers=merchant["auth"]).status_code == 401


# --- creating payments -------------------------------------------------------


def test_card_payment_resolves_synchronously(client: TestClient, merchant: dict) -> None:
    response = client.post("/v1/payments", json=CARD, headers=merchant["auth"])

    assert response.status_code == 201
    body = response.json()
    assert body["id"].startswith("pay_")
    assert body["status"] in (PaymentStatus.SUCCEEDED, PaymentStatus.FAILED)
    assert body["segment"] == "US:USD:visa"
    assert body["routing_mode"] == RoutingMode.ROUTER  # merchant traffic is never the control
    assert body["attempts"]
    assert body["attempts"][0]["provider"].endswith("_sim")
    assert body["resolved_ts"]


def test_the_attempt_chain_is_returned(client: TestClient, merchant: dict) -> None:
    """A cascade has to be visible in the response — it is the product."""
    chains = []
    for _ in range(60):
        body = client.post("/v1/payments", json=CARD, headers=merchant["auth"]).json()
        chains.append(body["attempts"])
    cascaded = [c for c in chains if len(c) > 1]

    assert cascaded, "60 payments produced no cascade at all"
    for chain in cascaded:
        assert [a["seq"] for a in chain] == list(range(1, len(chain) + 1))
        assert len({a["provider"] for a in chain}) == len(chain)  # never the same rail twice
        assert chain[0]["decline_code"]  # a cascade only happens after a decline


def test_routing_mode_can_be_forced(client: TestClient, merchant: dict) -> None:
    body = client.post(
        "/v1/payments", json=CARD | {"routing_mode": "baseline"}, headers=merchant["auth"]
    ).json()
    assert body["routing_mode"] == RoutingMode.BASELINE


@pytest.mark.parametrize(
    "payload",
    [
        {"amount_cents": 0, "currency": "USD", "country": "US", "card_brand": "visa"},
        {"amount_cents": 100, "currency": "US", "country": "US", "card_brand": "visa"},
        {"amount_cents": 100, "currency": "USD", "country": "US", "card_brand": "diners"},
        {"amount_cents": 100, "currency": "USD"},  # card needs a corridor
        dict(CARD, typo=1),  # extra fields are refused, not ignored
    ],
)
def test_bad_payments_are_refused(client: TestClient, merchant: dict, payload: dict) -> None:
    response = client.post("/v1/payments", json=payload, headers=merchant["auth"])
    assert response.status_code == 422
    assert response.json()["error"]


def test_prava_rail_says_so_when_it_is_not_configured(client: TestClient, merchant: dict) -> None:
    """Until the adapter lands it must fail loudly, not silently route a
    'real rail' payment through a simulated one."""
    response = client.post(
        "/v1/payments",
        json={"amount_cents": 1250, "currency": "USD", "method": "prava"},
        headers=merchant["auth"],
    )
    assert response.status_code == 503
    assert "prava" in response.json()["error"]


# --- reading payments back ---------------------------------------------------


def test_get_payment_round_trip(client: TestClient, merchant: dict) -> None:
    created = client.post("/v1/payments", json=CARD, headers=merchant["auth"]).json()

    fetched = client.get(f"/v1/payments/{created['id']}", headers=merchant["auth"])

    assert fetched.status_code == 200
    assert fetched.json() == created


def test_unknown_payment_is_a_404(client: TestClient, merchant: dict) -> None:
    assert client.get("/v1/payments/pay_nope", headers=merchant["auth"]).status_code == 404


def test_tenants_cannot_see_each_others_payments(client: TestClient, merchant: dict) -> None:
    mine = client.post("/v1/payments", json=CARD, headers=merchant["auth"]).json()
    other = client.post("/v1/tenants", json={"name": "Rival", "email": "x@rival.test"}).json()

    response = client.get(f"/v1/payments/{mine['id']}", headers=_auth(other["api_key"]))

    # 404, not 403: a payment belonging to someone else must be indistinguishable
    # from one that does not exist, or the ids themselves leak.
    assert response.status_code == 404


def test_list_payments_is_scoped_and_filterable(client: TestClient, merchant: dict) -> None:
    for _ in range(6):
        client.post("/v1/payments", json=CARD, headers=merchant["auth"])
    other = client.post("/v1/tenants", json={"name": "Rival", "email": "y@rival.test"}).json()
    client.post("/v1/payments", json=CARD, headers=_auth(other["api_key"]))

    mine = client.get("/v1/payments", headers=merchant["auth"]).json()["payments"]
    assert len(mine) == 6

    theirs = client.get("/v1/payments", headers=_auth(other["api_key"])).json()["payments"]
    assert len(theirs) == 1

    limited = client.get("/v1/payments?limit=2", headers=merchant["auth"]).json()["payments"]
    assert len(limited) == 2

    succeeded = client.get("/v1/payments?status=succeeded", headers=merchant["auth"])
    assert all(p["status"] == "succeeded" for p in succeeded.json()["payments"])


@pytest.mark.parametrize("query", ["?limit=0", "?limit=9999", "?limit=abc", "?status=nonsense"])
def test_bad_query_params_are_refused(client: TestClient, merchant: dict, query: str) -> None:
    assert client.get(f"/v1/payments{query}", headers=merchant["auth"]).status_code == 422


# --- analytics ---------------------------------------------------------------


def test_analytics_summary_measures_both_arms(client: TestClient, merchant: dict) -> None:
    for i in range(80):
        mode = "router" if i % 2 else "baseline"
        client.post("/v1/payments", json=CARD | {"routing_mode": mode}, headers=merchant["auth"])

    body = client.get("/v1/analytics/summary", headers=merchant["auth"]).json()

    assert body["volume"] == 80
    assert 0.0 < body["auth_rate"] <= 1.0
    assert body["by_mode"]["router"]["volume"] == 40
    assert body["by_mode"]["baseline"]["volume"] == 40
    assert body["uplift_pts"] is not None
    # 40 payments per arm is not a measurement yet, and the API says so.
    assert body["uplift"]["status"] == "collecting"
    assert body["uplift"]["significant"] is False
    low, high = body["uplift"]["ci95_pts"]
    assert low <= body["uplift_pts"] <= high
    assert body["avg_latency_ms"] > 0
    assert {p["provider"] for p in body["by_provider"]} <= {
        "stripe_sim",
        "braintree_sim",
        "adyen_sim",
    }
    assert body["by_corridor"][0]["segment"] == "US:USD:visa"


def test_uplift_is_none_until_both_arms_have_traffic(client: TestClient, merchant: dict) -> None:
    """Reporting 0.0 would look like a measured tie. There is no measurement."""
    client.post("/v1/payments", json=CARD | {"routing_mode": "router"}, headers=merchant["auth"])

    body = client.get("/v1/analytics/summary", headers=merchant["auth"]).json()
    assert body["uplift_pts"] is None
    assert body["uplift"]["status"] == "no_data"
    assert body["uplift"]["significant"] is False


def test_analytics_is_scoped_to_the_calling_tenant(client: TestClient, merchant: dict) -> None:
    for _ in range(5):
        client.post("/v1/payments", json=CARD, headers=merchant["auth"])
    other = client.post("/v1/tenants", json={"name": "Rival", "email": "z@rival.test"}).json()

    body = client.get("/v1/analytics/summary", headers=_auth(other["api_key"])).json()
    assert body["volume"] == 0


@pytest.mark.parametrize("window", ["30s", "15m", "1h"])
def test_valid_windows_are_accepted(client: TestClient, merchant: dict, window: str) -> None:
    response = client.get(f"/v1/analytics/summary?window={window}", headers=merchant["auth"])
    assert response.status_code == 200


@pytest.mark.parametrize("window", ["15", "m15", "0m", "99h", "abc"])
def test_unparseable_windows_are_refused(client: TestClient, merchant: dict, window: str) -> None:
    """A dashboard silently showing the wrong period is worse than one that
    admits it cannot."""
    response = client.get(f"/v1/analytics/summary?window={window}", headers=merchant["auth"])
    assert response.status_code == 422


def test_a_short_window_excludes_older_payments(client: TestClient, merchant: dict) -> None:
    for _ in range(4):
        client.post("/v1/payments", json=CARD, headers=merchant["auth"])

    wide = client.get("/v1/analytics/summary?window=1h", headers=merchant["auth"]).json()
    narrow = client.get("/v1/analytics/summary?window=1s", headers=merchant["auth"]).json()

    assert wide["volume"] == 4
    assert narrow["volume"] <= wide["volume"]


# --- lifespan ----------------------------------------------------------------


def test_boot_rebuilds_posteriors_from_the_database(db: str) -> None:
    """A restart must not throw away what the router learned — that is the
    whole reason the attempts table is the source of truth."""
    first = Engine.build(db_path=db, rng=random.Random(42), latency_scale=0)
    with TestClient(create_app(engine=first)) as client:
        signup = client.post("/v1/tenants", json={"name": "A", "email": "a@a.test"}).json()
        for _ in range(40):
            client.post("/v1/payments", json=CARD, headers=_auth(signup["api_key"]))
    learned = first.router.snapshot()
    assert learned

    second = Engine.build(db_path=db, rng=random.Random(42), latency_scale=0)
    with TestClient(create_app(engine=second)) as client:
        assert client.app.state.replayed_attempts >= 40

    assert second.router.snapshot() == learned


# --- the uplift statistic ----------------------------------------------------


def test_uplift_reports_no_data_when_an_arm_is_empty() -> None:
    stat = uplift_stat(10, 10, 0, 0)
    assert stat == {"pts": None, "ci95_pts": None, "status": "no_data", "significant": False}


def test_uplift_point_estimate_is_the_difference_in_points() -> None:
    stat = uplift_stat(950, 1_000, 850, 1_000)
    assert stat["pts"] == pytest.approx(10.0, abs=0.01)


def test_a_small_sample_is_never_called_measured() -> None:
    """The failure this guards against: 30 payments an arm produced a −10 pt
    reading during a live curl run. On stage that is the hero stat."""
    stat = uplift_stat(26, 30, 29, 30)

    assert stat["pts"] is not None  # still shown — hiding it would be its own lie
    assert stat["status"] == "collecting"
    assert stat["significant"] is False
    low, high = stat["ci95_pts"]
    assert low < 0 < high  # the interval straddles zero: no conclusion available


def test_a_perfect_small_sample_is_still_not_significant() -> None:
    """Every payment authorized in both arms gives a zero-width interval that
    is confident about precisely nothing."""
    stat = uplift_stat(5, 5, 5, 5)
    assert stat["ci95_pts"] == [0.0, 0.0]
    assert stat["significant"] is False


def test_a_real_difference_at_volume_is_called_measured() -> None:
    stat = uplift_stat(940, 1_000, 870, 1_000)

    assert stat["status"] == "measured"
    assert stat["significant"] is True
    assert stat["ci95_pts"][0] > 0  # the whole interval is above zero


def test_a_genuine_tie_at_volume_is_not_called_measured() -> None:
    stat = uplift_stat(900, 1_000, 899, 1_000)
    assert stat["significant"] is False
    assert stat["status"] == "collecting"


# --- admin -------------------------------------------------------------------

ADMIN = {"Authorization": "Bearer test-admin"}


@pytest.mark.parametrize(
    "headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic test-admin"}]
)
@pytest.mark.parametrize("path", ["/admin/state"])
def test_admin_routes_are_guarded(client: TestClient, headers: dict, path: str) -> None:
    assert client.get(path, headers=headers).status_code in (401, 403)


def test_admin_outage_requires_the_token(client: TestClient) -> None:
    response = client.post("/admin/outage", json={"provider": "stripe_sim", "mode": "outage"})
    assert response.status_code == 401


def test_injecting_degradation_changes_the_rail_and_logs_an_event(
    client: TestClient, db: str
) -> None:
    response = client.post(
        "/admin/outage",
        json={"provider": "stripe_sim", "mode": "degraded", "auth_rate": 0.45},
        headers=ADMIN,
    )

    assert response.status_code == 200
    assert response.json()["state"] == "degraded"
    events = store.recent_provider_events(since="2000-01-01T00:00:00.000+00:00", db_path=db)
    assert [(e["provider"], e["kind"]) for e in events] == [("stripe_sim", "degraded_start")]


def test_clear_restores_and_is_annotated(client: TestClient, db: str) -> None:
    client.post("/admin/outage", json={"provider": "stripe_sim", "mode": "outage"}, headers=ADMIN)
    client.post("/admin/outage", json={"provider": "stripe_sim", "mode": "clear"}, headers=ADMIN)

    events = store.recent_provider_events(since="2000-01-01T00:00:00.000+00:00", db_path=db)
    assert [e["kind"] for e in events] == ["outage_start", "cleared"]


def test_the_real_rail_cannot_be_faked(client: TestClient) -> None:
    """An operator must not be able to stage an outage on the one real rail —
    that would make a labeled-REAL row on the dashboard a lie."""
    response = client.post(
        "/admin/outage", json={"provider": "prava", "mode": "outage"}, headers=ADMIN
    )
    assert response.status_code == 404


@pytest.mark.parametrize(
    "payload",
    [
        {"provider": "stripe_sim", "mode": "nonsense"},
        {"provider": "stripe_sim", "mode": "degraded", "auth_rate": 5},
        {"provider": "nope", "mode": "outage"},
    ],
)
def test_bad_injections_are_refused(client: TestClient, payload: dict) -> None:
    response = client.post("/admin/outage", json=payload, headers=ADMIN)
    assert response.status_code in (404, 422)


def test_admin_state_exposes_the_posteriors(client: TestClient, merchant: dict) -> None:
    for _ in range(30):
        client.post("/v1/payments", json=CARD, headers=merchant["auth"])

    body = client.get("/admin/state", headers=ADMIN).json()

    assert body["posteriors"]
    row = body["posteriors"][0]
    assert {"provider", "segment", "alpha", "beta", "mean"} <= set(row)
    assert body["injections"]["stripe_sim"] == "healthy"
    assert body["generator"] is None  # not started in this app


def test_generator_admin_reports_when_none_is_running(client: TestClient) -> None:
    response = client.post("/admin/generator", json={"tps": 5}, headers=ADMIN)
    assert response.status_code == 503


# --- providers ---------------------------------------------------------------


def test_provider_health_is_public_and_labeled(client: TestClient) -> None:
    body = client.get("/v1/providers").json()["providers"]

    assert len(body) == 3
    assert all(p["label"] == "SIMULATED" for p in body)
    assert all(p["real"] is False for p in body)
    assert {"name", "state", "auth_rate", "p95_ms", "fee"} <= set(body[0])
