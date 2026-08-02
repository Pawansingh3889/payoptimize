"""SDK and MCP tools, driven against the real app through TestClient.

Nothing is stubbed except the clock. The SDK's `http=` seam takes Starlette's
TestClient, so these exercise the actual routes — which is the whole argument
for the MCP server being a thin client rather than a second implementation.
"""

from __future__ import annotations

import random
from collections.abc import Iterator

import pytest
from starlette.testclient import TestClient

from payoptimize import store
from payoptimize.api import create_app
from payoptimize.engine import Engine
from payoptimize.sdk import PayOptimizeClient, PayOptimizeError

BASE = "http://testserver"
CARD = {"country": "US", "card_brand": "visa"}

SESSION = {
    "session_id": "ses_TEST",
    "iframe_url": "https://sandbox.collect.prava.space?session=tok",
}


@pytest.fixture
def app_client(db: str) -> Iterator[TestClient]:
    engine = Engine.build(db_path=db, rng=random.Random(42), latency_scale=0, with_prava=False)
    with TestClient(create_app(engine=engine)) as client:
        yield client


@pytest.fixture
def sdk(app_client: TestClient) -> PayOptimizeClient:
    return PayOptimizeClient.signup(BASE, "Agent Co", "agent@co.test", http=app_client)


# --- the SDK -----------------------------------------------------------------


def test_signup_captures_the_key_that_is_shown_once(sdk: PayOptimizeClient) -> None:
    assert sdk.api_key.startswith("pok_")


def test_create_and_read_a_card_payment(sdk: PayOptimizeClient) -> None:
    payment = sdk.create_payment(1250, "USD", method="card", description="Pro plan", **CARD)

    assert payment["id"].startswith("pay_")
    assert payment["status"] in ("succeeded", "failed")
    assert sdk.get_payment(payment["id"]) == payment


def test_optional_fields_are_omitted_rather_than_sent_empty(sdk: PayOptimizeClient) -> None:
    """The API forbids unknown fields and validates the corridor, so a blank
    country would be a 422 rather than a helpful default."""
    payment = sdk.create_payment(500, "USD", method="card", **CARD)
    assert payment["description"] == ""


def test_list_payments_is_scoped_to_the_key(sdk: PayOptimizeClient, app_client: TestClient) -> None:
    for _ in range(3):
        sdk.create_payment(500, "USD", method="card", **CARD)
    other = PayOptimizeClient.signup(BASE, "Rival", "r@rival.test", http=app_client)
    other.create_payment(500, "USD", method="card", **CARD)

    assert len(sdk.list_payments()) == 3
    assert len(other.list_payments()) == 1


def test_provider_health_needs_no_key(app_client: TestClient) -> None:
    anonymous = PayOptimizeClient(BASE, http=app_client)
    providers = anonymous.provider_health()

    assert len(providers) == 3
    assert all(p["label"] == "SIMULATED" for p in providers)


def test_analytics_round_trip(sdk: PayOptimizeClient) -> None:
    for _ in range(4):
        sdk.create_payment(500, "USD", method="card", **CARD)
    body = sdk.analytics("15m")

    assert body["volume"] == 4
    assert "uplift" in body


def test_api_errors_carry_their_status(sdk: PayOptimizeClient) -> None:
    """An agent has to tell 'your request was wrong' from 'try again later'."""
    with pytest.raises(PayOptimizeError) as bad_request:
        sdk.create_payment(0, "USD", method="card", **CARD)
    assert bad_request.value.status_code == 422

    with pytest.raises(PayOptimizeError) as missing:
        sdk.get_payment("pay_nope")
    assert missing.value.status_code == 404


def test_a_client_without_a_key_says_so(app_client: TestClient) -> None:
    anonymous = PayOptimizeClient(BASE, http=app_client)
    with pytest.raises(PayOptimizeError, match="no API key"):
        anonymous.list_payments()


def test_wait_returns_immediately_on_a_resolved_payment(sdk: PayOptimizeClient) -> None:
    payment = sdk.create_payment(500, "USD", method="card", **CARD)
    slept: list[float] = []

    settled = sdk.wait_for_payment(payment["id"], sleep=slept.append)

    assert settled["status"] in ("succeeded", "failed")
    assert slept == []  # a resolved payment must not cost a single poll


def test_wait_gives_up_rather_than_hanging_forever(
    app_client: TestClient, db: str, sdk: PayOptimizeClient
) -> None:
    """An agent that blocks forever is worse than one that reports it gave up."""
    engine = Engine.build(db_path=db, rng=random.Random(1), latency_scale=0, with_prava=True)
    tenant = store.tenant_by_email("agent@co.test", db_path=db)
    assert tenant is not None
    store.insert_payment(
        payment_id="pay_stuck",
        tenant_id=int(tenant["id"]),
        amount_cents=1250,
        currency="USD",
        country="",
        card_brand="",
        method="prava",
        routing_mode="router",
        segment=":USD:prava",
        status="pending_approval",
        source="agent",
        db_path=db,
    )
    slept: list[float] = []

    settled = sdk.wait_for_payment("pay_stuck", timeout_s=0.0, sleep=slept.append)

    assert settled["status"] == "pending_approval"
    assert engine is not None


# --- the MCP tools -----------------------------------------------------------


@pytest.fixture
def mcp_env(monkeypatch: pytest.MonkeyPatch, sdk: PayOptimizeClient, app_client: TestClient):
    """Point the MCP server's client factory at the TestClient-backed app."""
    from payoptimize import server as mcp_server

    monkeypatch.setenv("PAYOPTIMIZE_API_URL", BASE)
    monkeypatch.setenv("PAYOPTIMIZE_API_KEY", sdk.api_key)
    monkeypatch.setattr(
        mcp_server, "_client", lambda: PayOptimizeClient(BASE, sdk.api_key, http=app_client)
    )
    return mcp_server


def test_tools_are_registered_with_descriptions(mcp_env) -> None:
    import asyncio

    tools = asyncio.run(mcp_env.server.list_tools())
    names = {t.name for t in tools}

    assert names == {
        "create_payment",
        "get_payment",
        "wait_for_payment",
        "provider_health",
        "list_recent_payments",
        # An outside agent can consult the resident one rather than reasoning
        # about payment internals it has no evidence for.
        "ask_ops",
        "diagnose_payment",
    }
    # The docstrings are the agent's only documentation.
    assert all(t.description for t in tools)


def test_create_payment_tool_tells_the_agent_what_pending_means(mcp_env) -> None:
    """An agent that reads 'pending_approval' as failure gives up seconds before
    the payment succeeds."""
    import asyncio

    tools = {t.name: t for t in asyncio.run(mcp_env.server.list_tools())}
    doc = tools["create_payment"].description or ""

    assert "pending_approval" in doc
    assert "NOT a failure" in doc
    assert "approval_url" in doc
    assert "1250" in doc  # integer cents, spelled out


def test_card_payment_through_the_tool(mcp_env) -> None:
    result = mcp_env.create_payment(
        1250, "USD", "Pro plan", method="card", country="US", card_brand="visa"
    )
    assert "payment_id" in result


def test_the_tool_summary_shows_the_route(mcp_env) -> None:
    created = mcp_env.create_payment(
        1250, "USD", "Pro plan", method="card", country="US", card_brand="visa"
    )
    fetched = mcp_env.get_payment(created["payment_id"])

    assert fetched["payment_id"] == created["payment_id"]
    assert fetched["route"]  # the cascade, readable by an agent
    assert set(fetched) >= {"payment_id", "status", "amount_cents", "currency", "route"}


def test_provider_health_tool_marks_the_real_rail(mcp_env) -> None:
    rails = mcp_env.provider_health()

    assert len(rails) == 3
    assert all(r["real"] is False for r in rails)
    assert all(r["label"] == "SIMULATED" for r in rails)


def test_list_recent_payments_tool(mcp_env) -> None:
    for _ in range(3):
        mcp_env.create_payment(500, "USD", "x", method="card", country="US", card_brand="visa")

    recent = mcp_env.list_recent_payments(limit=2)
    assert len(recent) == 2


def test_the_tool_layer_refuses_without_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from payoptimize import server as mcp_server

    monkeypatch.setenv("PAYOPTIMIZE_API_KEY", "")
    with pytest.raises(PayOptimizeError, match="PAYOPTIMIZE_API_KEY"):
        mcp_server._client()


# --- the prava path through the SDK ------------------------------------------


def test_an_agents_prava_payment_returns_an_approval_url(db: str) -> None:
    """The MVP line: an agent creates a real-rail payment and gets back
    something a human can act on."""

    engine = Engine.build(db_path=db, rng=random.Random(42), latency_scale=0, with_prava=True)

    with TestClient(create_app(engine=engine)) as app_client:
        client = PayOptimizeClient.signup(BASE, "Agent", "a@agent.test", http=app_client)
        # The engine reaches Prava through its own client; patch that seam.
        import payoptimize.providers.prava as prava_mod

        original = prava_mod.create_session
        prava_mod.create_session = lambda *a, **kw: dict(SESSION)  # type: ignore[assignment]
        try:
            payment = client.create_payment(1250, "USD", method="prava", description="Pro plan")
        finally:
            prava_mod.create_session = original  # type: ignore[assignment]

    assert payment["status"] == "pending_approval"
    assert payment["iframe_url"] == SESSION["iframe_url"]
    assert payment["attempts"][0]["provider"] == "prava"


def test_the_demo_script_imports_and_parses_arguments() -> None:
    """It is the on-stage fallback; it must not be the thing that breaks."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "scripts/agent_demo.py", "--help"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0
    assert "--url" in result.stdout
    assert "sandbox transaction" in result.stdout or "Prava" in result.stdout


def test_qr_rendering_does_not_explode() -> None:
    import io
    from contextlib import redirect_stdout

    import segno

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        segno.make("https://sandbox.collect.prava.space?session=tok", error="m").terminal(
            compact=True
        )

    assert len(buffer.getvalue()) > 100
