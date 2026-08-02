"""The agent's public surface: scoping, failure modes, and the audit trail.

The LLM is stubbed at the transport (`tests/agent_stub.py`), so these assert what
the *service* does around the model — which is the part that has to be right
whether the model is brilliant, broken, or absent.
"""

from __future__ import annotations

import random
from collections.abc import Iterator

import pytest
from agent_stub import OpenAIStub, completion
from starlette.testclient import TestClient

from payoptimize import store
from payoptimize.agent import loop as agent_loop
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


@pytest.fixture
def answering(monkeypatch: pytest.MonkeyPatch):
    """Point the loop's HTTP client at a scripted model.

    The route builds its own client, so the seam is `loop.run` itself — patched
    to inject the stub's transport while leaving every other argument alone.
    """

    def _install(*replies: dict) -> OpenAIStub:
        stub = OpenAIStub(list(replies))
        original = agent_loop.run

        def patched(engine, question, **kwargs):
            kwargs.setdefault("http", stub.client())
            return original(engine, question, **kwargs)

        monkeypatch.setattr(agent_loop, "run", patched)
        return stub

    return _install


def _answer(text: str) -> dict:
    return completion(content=text)


# --- auth and scoping --------------------------------------------------------


@pytest.mark.parametrize("path", ["/v1/agent/ask", "/v1/agent/diagnose"])
def test_the_agent_needs_a_key(client: TestClient, path: str) -> None:
    assert client.post(path, json={"question": "hi", "payment_id": "x"}).status_code == 401


def test_runs_need_a_key(client: TestClient) -> None:
    assert client.get("/v1/agent/runs").status_code == 401


def test_diagnosing_someone_elses_payment_is_a_404(
    client: TestClient, merchant: dict, answering
) -> None:
    """Same contract as GET /v1/payments/{id}: 404 rather than 403, so the ids
    themselves do not leak — and the model is never even invoked."""
    answering(_answer("should not be reached"))
    mine = client.post("/v1/payments", json=CARD, headers=merchant["auth"]).json()
    other = client.post("/v1/tenants", json={"name": "Rival", "email": "r@rival.test"}).json()

    response = client.post(
        "/v1/agent/diagnose",
        json={"payment_id": mine["id"]},
        headers={"Authorization": f"Bearer {other['api_key']}"},
    )

    assert response.status_code == 404


@pytest.mark.parametrize("question", ["", "   ", "x" * 501])
def test_junk_questions_are_refused(client: TestClient, merchant: dict, question: str) -> None:
    response = client.post("/v1/agent/ask", json={"question": question}, headers=merchant["auth"])
    assert response.status_code == 422


# --- the unconfigured deployment ---------------------------------------------


def test_an_unconfigured_agent_says_so(
    client: TestClient, merchant: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deployment with no OpenAI key is a normal, working deployment — the
    payments half does not need one. Same 503 contract as the Prava rail."""
    monkeypatch.setenv("OPENAI_API_KEY", "")

    response = client.post(
        "/v1/agent/ask", json={"question": "how are we doing?"}, headers=merchant["auth"]
    )

    assert response.status_code == 503
    assert "OPENAI_API_KEY" in response.json()["error"]


def test_payments_are_unaffected_when_the_agent_is_dead(
    client: TestClient, merchant: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression that matters most: an LLM outage must never become a
    payments outage."""
    monkeypatch.setenv("OPENAI_API_KEY", "")

    created = client.post("/v1/payments", json=CARD, headers=merchant["auth"])
    assert created.status_code == 201
    assert created.json()["status"] in ("succeeded", "failed")
    assert client.get("/").status_code == 200
    assert client.get("/v1/providers").status_code == 200


# --- answering ---------------------------------------------------------------


def test_asking_returns_an_answer_and_an_audit_row(
    client: TestClient, merchant: dict, answering, db: str
) -> None:
    answering(_answer("Your auth rate is 91% and stripe_sim is leading."))

    response = client.post(
        "/v1/agent/ask", json={"question": "how are we doing?"}, headers=merchant["auth"]
    )

    assert response.status_code == 200
    body = response.json()
    assert "91%" in body["answer"]
    assert body["run_id"] > 0
    assert body["usage"]["model"]

    runs = store.recent_agent_runs(tenant_id=merchant["id"], db_path=db)
    assert len(runs) == 1
    assert runs[0]["trigger_kind"] == "ask"


def test_diagnose_reaches_the_model_with_the_payment_in_hand(
    client: TestClient, merchant: dict, answering
) -> None:
    payment = client.post("/v1/payments", json=CARD, headers=merchant["auth"]).json()
    stub = answering(
        completion(tool_calls=[("get_payment", {"payment_id": payment["id"]})]),
        _answer("It failed on the first attempt with a terminal decline."),
    )

    response = client.post(
        "/v1/agent/diagnose", json={"payment_id": payment["id"]}, headers=merchant["auth"]
    )

    assert response.status_code == 200
    assert response.json()["answer"]
    assert stub.requests, "the model was never called"
    # The tool actually ran: the second request carries the tool result back.
    assert any(m.get("role") == "tool" for m in stub.requests[-1]["messages"])


def test_the_run_log_is_scoped_to_the_caller(client: TestClient, merchant: dict, answering) -> None:
    answering(_answer("ok"), _answer("ok"))
    client.post("/v1/agent/ask", json={"question": "mine"}, headers=merchant["auth"])
    other = client.post("/v1/tenants", json={"name": "Rival", "email": "z@rival.test"}).json()

    theirs = client.get(
        "/v1/agent/runs", headers={"Authorization": f"Bearer {other['api_key']}"}
    ).json()["runs"]

    assert theirs == []
    mine = client.get("/v1/agent/runs", headers=merchant["auth"]).json()["runs"]
    assert len(mine) == 1


def test_the_index_advertises_the_agent(client: TestClient) -> None:
    endpoints = client.get("/v1").json()["endpoints"]
    assert any("agent" in path for path in endpoints)
