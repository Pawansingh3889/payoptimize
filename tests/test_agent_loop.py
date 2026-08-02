"""The loop: scripted conversations, an unconditional audit, a hard cap."""

from __future__ import annotations

import json

import pytest
from agent_stub import OpenAIStub, completion

from payoptimize import store
from payoptimize.agent import llm, loop
from payoptimize.engine import Engine
from payoptimize.models import (
    PaymentMethod,
    PaymentSource,
    PaymentStatus,
    RoutingMode,
    new_payment_id,
)


@pytest.fixture
def engine(db: str) -> Engine:
    built = Engine.build(db_path=db, latency_scale=0.0, with_prava=False)
    built.boot()
    return built


def _payment(db: str, tenant_id: int) -> str:
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
        status=PaymentStatus.FAILED,
        source=PaymentSource.API,
        db_path=db,
    )
    return payment_id


def test_a_scripted_conversation_lands_in_the_audit(engine: Engine, db: str) -> None:
    tenant_id = store.create_tenant_with_key("Acme", "ops@acme.test", "hash", "pok_…", db_path=db)
    payment_id = _payment(db, tenant_id)
    stub = OpenAIStub(
        [
            completion(tool_calls=[("get_payment", {"payment_id": payment_id})]),
            completion(content=f"{payment_id} failed for Acme", tokens_in=30, tokens_out=9),
        ]
    )

    with stub.client() as http:
        result = loop.run(engine, f"why did {payment_id} fail?", tenant_id=tenant_id, http=http)

    # The model's mention of the tenant by name is redacted in the stored
    # answer and restored only for the tenant's own display.
    assert "Acme" not in result["answer"]
    assert "Acme" in result["display_answer"]
    assert [e["tool"] for e in result["evidence"]] == ["get_payment"]
    assert result["usage"]["tokens_in"] == 40  # both calls, summed

    run = store.recent_agent_runs(db_path=db)[0]
    assert run["id"] == result["run_id"]
    assert run["tools_used"] == "get_payment"
    assert "Acme" not in run["answer"]

    # The second request carried the tool result back, redacted.
    tool_message = stub.requests[1]["messages"][-1]
    assert tool_message["role"] == "tool"
    assert "ops@acme.test" not in json.dumps(stub.requests)


def test_the_turn_cap_holds(engine: Engine, db: str) -> None:
    store.init_db(db)
    stub = OpenAIStub(
        [completion(tool_calls=[("provider_health", {})]) for _ in range(loop.MAX_TURNS + 5)]
    )
    with stub.client() as http:
        result = loop.run(engine, "poke every tool forever", http=http)

    assert len(stub.requests) == loop.MAX_TURNS
    assert result["answer"] == loop.CAP_ANSWER


def test_a_dead_model_still_leaves_an_audit_row(engine: Engine, db: str) -> None:
    store.init_db(db)
    stub = OpenAIStub(status=500)
    with stub.client() as http, pytest.raises(llm.AgentError):
        loop.run(engine, "anything", http=http)

    run = store.recent_agent_runs(db_path=db)[0]
    assert run["question"] == "anything"
    assert run["answer"] == ""


def test_capture_stores_the_redacted_transcript(
    engine: Engine, db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PAYOPTIMIZE_AGENT_CAPTURE", "1")
    store.create_tenant_with_key("Acme", "ops@acme.test", "hash", "pok_…", db_path=db)
    stub = OpenAIStub([completion(content="all quiet")])
    with stub.client() as http:
        result = loop.run(engine, "status of Acme?", http=http)

    transcripts = store.agent_transcripts(db_path=db)
    assert len(transcripts) == 1
    assert transcripts[0]["run_id"] == result["run_id"]
    assert "ops@acme.test" not in transcripts[0]["messages"]
    assert "Acme" not in transcripts[0]["messages"].replace("PayOptimize", "")
