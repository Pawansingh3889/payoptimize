"""The real rail, mocked at the transport — no network, no key, no sandbox spend.

conftest pins PRAVA_API_BASE to an unregistrable host, so anything that escapes
a MockTransport dies at DNS rather than quietly costing one of a finite number
of sandbox transactions.
"""

from __future__ import annotations

import asyncio
import json
import random

import httpx
import pytest

from payoptimize import store
from payoptimize.engine import Engine, prava_configured
from payoptimize.models import AttemptStatus, PaymentStatus
from payoptimize.pravapoller import PravaPoller
from payoptimize.providers import ChargeRequest, PravaProvider, prava

SESSION = {
    "session_id": "ses_TEST",
    "session_token": "tok",
    "iframe_url": "https://sandbox.collect.prava.space?session=tok",
    "order_id": "ord_TEST",
}


@pytest.fixture(autouse=True)
def sandbox_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guards compare the key prefix against the host, so the host has to
    look like the sandbox for anything but the guard tests."""
    monkeypatch.setenv("PRAVA_API_BASE", "https://sandbox.api.prava.space")


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _created(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    assert body["total_amount"] == "12.50"
    assert request.headers["Authorization"].startswith("Bearer sk_test_")
    return httpx.Response(201, json=SESSION)


def _charge(amount_cents: int = 1250) -> ChargeRequest:
    return ChargeRequest(
        payment_id="pay_test",
        amount_cents=amount_cents,
        currency="USD",
        segment=":USD:prava",
        description="Pro plan",
    )


# --- money at the boundary ---------------------------------------------------


@pytest.mark.parametrize(
    ("cents", "expected"),
    [(1250, "12.50"), (5, "0.05"), (100, "1.00"), (99, "0.99"), (123456, "1234.56")],
)
def test_cents_become_a_decimal_string_only_here(cents: int, expected: str) -> None:
    """The one place money changes representation in the whole system."""
    assert prava.amount_to_decimal(cents) == expected


# --- create_session ----------------------------------------------------------


def test_create_session_mints() -> None:
    with _client(_created) as http:
        session = prava.create_session(
            "PayOptimize", "https://x.test", "US", {"price": "12.50"}, http=http
        )
    assert session["session_id"] == "ses_TEST"


def test_create_session_401_raises() -> None:
    def denied(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"code": "AUTH_1001"}})

    with _client(denied) as http, pytest.raises(prava.PravaError, match="401"):
        prava.create_session("X", "https://x.test", "US", {"price": "1.00"}, http=http)


def test_a_synthetic_identity_is_refused_before_spending_a_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A made-up user mints a session fine and then fails passkey verification
    hundreds of milliseconds later, far from the cause. Refuse first."""
    monkeypatch.setenv("PAYOPTIMIZE_USER_ID", "")
    monkeypatch.setenv("PAYOPTIMIZE_USER_EMAIL", "")

    with _client(_created) as http, pytest.raises(prava.PravaError, match="PAYOPTIMIZE_USER_ID"):
        prava.create_session("X", "https://x.test", "US", {"price": "1.00"}, http=http)


def test_the_configured_identity_is_what_gets_sent() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(201, json=SESSION)

    with _client(handler) as http:
        prava.create_session("X", "https://x.test", "US", {"price": "1.00"}, http=http)

    assert seen["user_id"] == "unit-test"
    assert seen["user_email"] == "unit-test@example.invalid"


# --- key-mode guards ---------------------------------------------------------


def test_a_live_key_needs_explicit_permission(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRAVA_SECRET_KEY", "sk_live_something")
    monkeypatch.setenv("PRAVA_API_BASE", "https://api.prava.space")
    monkeypatch.delenv("PAYOPTIMIZE_ALLOW_LIVE", raising=False)

    assert prava.key_mode() == "live"
    with pytest.raises(prava.PravaError, match="PAYOPTIMIZE_ALLOW_LIVE"):
        prava._secret()


def test_a_live_key_pointed_at_the_sandbox_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRAVA_SECRET_KEY", "sk_live_something")
    monkeypatch.setenv("PAYOPTIMIZE_ALLOW_LIVE", "1")
    with pytest.raises(prava.PravaError, match="sandbox host"):
        prava._secret()


def test_a_sandbox_key_pointed_at_the_live_host_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This answers a bare 401 at the API. Fail with a sentence instead."""
    monkeypatch.setenv("PRAVA_API_BASE", "https://api.prava.space")
    with pytest.raises(prava.PravaError, match="live host"):
        prava._secret()


def test_a_missing_key_is_not_a_prava_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRAVA_SECRET_KEY", "")
    with pytest.raises(prava.PravaError, match="PRAVA_SECRET_KEY"):
        prava.key_mode()


def test_the_rail_is_only_offered_when_it_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert prava_configured() is True
    monkeypatch.setenv("PAYOPTIMIZE_USER_ID", "")
    assert prava_configured() is False
    monkeypatch.setenv("PRAVA_SECRET_KEY", "not-a-key")
    assert prava_configured() is False


# --- payment_result ----------------------------------------------------------


def test_payment_result_sends_the_cache_busters() -> None:
    """Without these a caching layer replays a stale `pending` forever, and the
    payment never settles no matter how many times it is polled."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        seen["cache_control"] = request.headers.get("Cache-Control")
        return httpx.Response(200, json={"status": "pending"})

    with _client(handler) as http:
        prava.payment_result("ses_TEST", http=http)

    assert "_t" in seen["params"]
    assert seen["cache_control"] == "no-store"


def test_credentials_read_the_nested_line_items() -> None:
    """They live one level deeper than the transactions entry, which carries
    only txn_id and status."""
    result = {
        "transactions": [
            {
                "txn_id": "txn_1",
                "status": "ok",
                "line_items": [{"token": "tok_abc", "last4": "4242"}],
            }
        ]
    }
    found = prava.credentials(result)

    assert len(found) == 1
    assert found[0]["token"] == "tok_abc"
    assert found[0]["txn_id"] == "txn_1"


def test_credentials_empty_without_line_items() -> None:
    assert prava.credentials({"transactions": [{"txn_id": "t", "status": "pending"}]}) == []
    assert prava.credentials({}) == []


def test_failure_code_is_read_verbatim() -> None:
    """Stored as-is: it is the first thing Prava support asks for."""
    result = {"transactions": [{"error": {"code": "PASSKEY_REG_FAILED"}}]}
    assert prava.failure_code(result) == "PASSKEY_REG_FAILED"
    assert prava.failure_code({"transactions": [{}]}) == ""


# --- report_status -----------------------------------------------------------


def test_report_status_posts_the_outcome() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        seen["path"] = request.url.path
        return httpx.Response(200, json={"ok": True})

    with _client(handler) as http:
        prava.report_status("ses_TEST", "txn_1", "APPROVED", http=http)

    assert seen["txn_ref_id"] == "txn_1"
    assert seen["txn_status"] == "APPROVED"
    assert seen["path"].endswith("/v1/sessions/ses_TEST/report-status")


def test_report_status_rejects_an_invalid_outcome() -> None:
    with pytest.raises(prava.PravaError, match="APPROVED or DECLINED"):
        prava.report_status("ses", "txn", "MAYBE")


# --- the adapter -------------------------------------------------------------


def test_charge_returns_pending_with_an_approval_url() -> None:
    """It cannot authorize inside the request — a human has to tap a passkey."""
    provider = PravaProvider()
    with _client(_created) as http:
        outcome = asyncio.run(provider.charge(_charge(), http=http))

    assert outcome.provider == "prava"
    assert outcome.status is AttemptStatus.PENDING
    assert outcome.prava_session_id == "ses_TEST"
    assert outcome.iframe_url.startswith("https://")
    assert provider.real is True


def test_the_adapter_satisfies_the_same_contract_as_the_sims() -> None:
    from payoptimize.providers import ProviderAdapter

    assert isinstance(PravaProvider(), ProviderAdapter)


# --- end to end through the engine and poller --------------------------------


@pytest.fixture
def engine(db: str) -> Engine:
    built = Engine.build(db_path=db, rng=random.Random(42), latency_scale=0, with_prava=True)
    built.boot()
    return built


def _tenant(db: str) -> int:
    """Find-or-create: a test that creates two payments must not try to mint a
    second key with the same hash."""
    existing = store.tenant_by_email("a@acme.test", db_path=db)
    if existing is not None:
        return int(existing["id"])
    return store.create_tenant_with_key("Acme", "a@acme.test", "hash", "pok_x…", db_path=db)


def _create_prava_payment(engine: Engine, db: str) -> str:
    from payoptimize.models import PaymentMethod, PaymentRequest

    request = PaymentRequest(amount_cents=1250, currency="USD", method=PaymentMethod.PRAVA)
    with _client(_created) as http:
        response = asyncio.run(engine.execute(request, tenant_id=_tenant(db), http=http))
    return response.id


def test_a_prava_payment_waits_for_a_human(engine: Engine, db: str) -> None:
    from payoptimize.models import PaymentMethod, PaymentRequest

    request = PaymentRequest(amount_cents=1250, currency="USD", method=PaymentMethod.PRAVA)
    with _client(_created) as http:
        response = asyncio.run(engine.execute(request, tenant_id=_tenant(db), http=http))

    assert response.status is PaymentStatus.PENDING_APPROVAL
    assert response.iframe_url.startswith("https://")
    assert len(response.attempts) == 1
    assert response.attempts[0].provider == "prava"
    assert response.attempts[0].status is AttemptStatus.PENDING


def test_prava_never_cascades_and_never_becomes_a_bandit_arm(engine: Engine, db: str) -> None:
    """It needs a human and draws on a finite sandbox budget — it is a rail you
    ask for by name, never one an algorithm may decide to spend."""
    _create_prava_payment(engine, db)

    assert engine.router.snapshot() == []
    attempts = store.pending_prava_attempts(db_path=db)
    assert len(attempts) == 1


def test_an_approved_session_settles_with_exactly_one_report_status(
    engine: Engine, db: str
) -> None:
    """The rule this whole file exists for: an unreported session hangs in
    `awaiting_result` forever."""
    payment_id = _create_prava_payment(engine, db)
    reports: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/report-status"):
            reports.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(
            200,
            json={
                "status": "awaiting_result",
                "transactions": [
                    {"txn_id": "txn_9", "line_items": [{"token": "tok_abc", "last4": "4242"}]}
                ],
            },
        )

    poller = PravaPoller(db_path=db)
    with _client(handler) as http:
        assert asyncio.run(poller.settle_once(http=http)) == 1
        # A second sweep must find nothing left to do — reporting twice would be
        # a second settlement of the same transaction.
        assert asyncio.run(poller.settle_once(http=http)) == 0

    assert len(reports) == 1
    assert reports[0] == {"txn_ref_id": "txn_9", "txn_status": "APPROVED"}

    payment = store.get_payment(payment_id, db_path=db)
    assert payment is not None
    assert payment["status"] == PaymentStatus.SUCCEEDED
    assert payment["final_provider"] == "prava"
    attempts = store.attempts_for_payment(payment_id, db_path=db)
    assert attempts[0]["status"] == AttemptStatus.SUCCEEDED
    assert attempts[0]["prava_txn_id"] == "txn_9"


def test_a_pending_session_is_left_alone(engine: Engine, db: str) -> None:
    payment_id = _create_prava_payment(engine, db)

    def handler(request: httpx.Request) -> httpx.Response:
        assert not request.url.path.endswith("/report-status"), "nothing to report yet"
        return httpx.Response(200, json={"status": "pending"})

    poller = PravaPoller(db_path=db)
    with _client(handler) as http:
        assert asyncio.run(poller.settle_once(http=http)) == 0

    payment = store.get_payment(payment_id, db_path=db)
    assert payment is not None
    assert payment["status"] == PaymentStatus.PENDING_APPROVAL


def test_a_failed_session_stores_the_code_verbatim_and_does_not_cascade(
    engine: Engine, db: str
) -> None:
    payment_id = _create_prava_payment(engine, db)

    def handler(request: httpx.Request) -> httpx.Response:
        assert not request.url.path.endswith("/report-status"), "no credential, nothing to report"
        return httpx.Response(
            200,
            json={
                "status": "failed",
                "transactions": [{"error": {"code": "PASSKEY_REG_FAILED"}}],
            },
        )

    poller = PravaPoller(db_path=db)
    with _client(handler) as http:
        assert asyncio.run(poller.settle_once(http=http)) == 1

    payment = store.get_payment(payment_id, db_path=db)
    assert payment is not None
    assert payment["status"] == PaymentStatus.FAILED
    assert payment["decline_code"] == "PASSKEY_REG_FAILED"
    # A human declining is terminal by design: exactly one attempt, no retry.
    assert len(store.attempts_for_payment(payment_id, db_path=db)) == 1


def test_an_unapproved_session_times_out(engine: Engine, db: str) -> None:
    payment_id = _create_prava_payment(engine, db)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "pending"})

    poller = PravaPoller(db_path=db, deadline=-1.0)  # everything is already late
    with _client(handler) as http:
        assert asyncio.run(poller.settle_once(http=http)) == 1

    payment = store.get_payment(payment_id, db_path=db)
    assert payment is not None
    assert payment["status"] == PaymentStatus.FAILED
    assert payment["decline_code"] == "approval_timeout"


def test_one_unreachable_session_does_not_stall_the_others(engine: Engine, db: str) -> None:
    first = _create_prava_payment(engine, db)
    second = _create_prava_payment(engine, db)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/report-status"):
            return httpx.Response(200, json={"ok": True})
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("boom")
        return httpx.Response(
            200,
            json={
                "status": "awaiting_result",
                "transactions": [{"txn_id": "txn_ok", "line_items": [{"token": "t"}]}],
            },
        )

    poller = PravaPoller(db_path=db)
    with _client(handler) as http:
        assert asyncio.run(poller.settle_once(http=http)) == 1

    statuses = {pid: store.get_payment(pid, db_path=db)["status"] for pid in (first, second)}
    assert sorted(statuses.values()) == ["pending_approval", "succeeded"]
