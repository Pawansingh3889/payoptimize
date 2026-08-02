"""The agent's audit tables and the queries its triggers and guards stand on."""

from __future__ import annotations

import pytest

from payoptimize import config, store
from payoptimize.models import (
    AttemptStatus,
    PaymentMethod,
    PaymentSource,
    PaymentStatus,
    RoutingMode,
    new_payment_id,
)


def _tenant(db: str, name: str = "Acme", email: str = "ops@acme.test") -> int:
    return store.create_tenant_with_key(name, email, f"hash-{name}", "pok_test1234…", db_path=db)


def _payment(db: str, tenant_id: int, *, status: str, created_ts: str | None = None) -> str:
    payment_id = new_payment_id()
    store.insert_payment(
        payment_id=payment_id,
        tenant_id=tenant_id,
        amount_cents=500,
        currency="USD",
        country="US",
        card_brand="visa",
        method=PaymentMethod.CARD,
        routing_mode=RoutingMode.ROUTER,
        segment="US:USD:visa",
        status=status,
        source=PaymentSource.API,
        db_path=db,
    )
    if created_ts is not None:
        with store.transaction(db) as conn:
            conn.execute(
                "UPDATE payments SET created_ts = ? WHERE id = ?", (created_ts, payment_id)
            )
    return payment_id


def test_agent_tables_exist(db: str) -> None:
    store.init_db(db)
    with store.transaction(db) as conn:
        names = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert {"agent_runs", "agent_actions"} <= names


def test_agent_run_round_trip(db: str) -> None:
    store.init_db(db)
    run_id = store.insert_agent_run(
        tenant_id=0, trigger_kind="stranded", question="what is stuck?", model="gpt-5", db_path=db
    )
    store.finish_agent_run(
        run_id,
        answer="two payments were stranded",
        tools_used="find_stranded_payments,reconcile_payment",
        tokens_in=900,
        tokens_out=120,
        latency_ms=1500,
        db_path=db,
    )

    runs = store.recent_agent_runs(db_path=db)
    assert len(runs) == 1
    run = runs[0]
    assert run["trigger_kind"] == "stranded"
    assert run["answer"] == "two payments were stranded"
    assert run["tokens_in"] == 900
    assert run["latency_ms"] == 1500

    with pytest.raises(store.NotFoundError):
        store.finish_agent_run(
            999, answer="", tools_used="", tokens_in=0, tokens_out=0, latency_ms=0, db_path=db
        )


def test_agent_runs_filter_by_tenant_and_kind(db: str) -> None:
    store.init_db(db)
    store.insert_agent_run(tenant_id=1, trigger_kind="ask", question="q", model="gpt-5", db_path=db)
    store.insert_agent_run(
        tenant_id=0, trigger_kind="health_event", question="", model="gpt-5", db_path=db
    )

    assert len(store.recent_agent_runs(tenant_id=1, db_path=db)) == 1
    incidents = store.recent_agent_runs(kinds=("health_event", "stranded"), db_path=db)
    assert [r["trigger_kind"] for r in incidents] == ["health_event"]


def test_agent_action_decide_is_exactly_once(db: str) -> None:
    store.init_db(db)
    run_id = store.insert_agent_run(
        tenant_id=0, trigger_kind="stranded", question="", model="gpt-5", db_path=db
    )
    action_id = store.insert_agent_action(
        run_id=run_id,
        kind="reconcile",
        params='{"payment_id": "pay_x"}',
        rationale="stranded",
        db_path=db,
    )
    assert [a["id"] for a in store.pending_agent_actions(db_path=db)] == [action_id]

    store.decide_agent_action(action_id, status="approved", db_path=db)
    # The second decision loses the race by design.
    with pytest.raises(store.NotFoundError):
        store.decide_agent_action(action_id, status="rejected", db_path=db)

    store.mark_agent_action(action_id, status="executed", detail="resolved", db_path=db)
    action = store.get_agent_action(action_id, db_path=db)
    assert action is not None
    assert action["status"] == "executed"
    assert action["detail"] == "resolved"
    assert store.pending_agent_actions(db_path=db) == []


def test_auto_executed_action_carries_its_decision_time(db: str) -> None:
    store.init_db(db)
    run_id = store.insert_agent_run(
        tenant_id=0, trigger_kind="stranded", question="", model="gpt-5", db_path=db
    )
    action_id = store.insert_agent_action(
        run_id=run_id,
        kind="clear_injection",
        params="{}",
        rationale="",
        status="executed",
        detail="cleared",
        db_path=db,
    )
    action = store.get_agent_action(action_id, db_path=db)
    assert action is not None
    assert action["status"] == "executed"
    assert action["decided_ts"] != ""


def test_stranded_payments_means_old_pending_with_nothing_in_flight(db: str) -> None:
    tenant_id = _tenant(db)
    old_ts = "2026-07-01T00:00:00.000+00:00"

    stuck = _payment(db, tenant_id, status=PaymentStatus.PENDING, created_ts=old_ts)
    fresh_pending = _payment(db, tenant_id, status=PaymentStatus.PENDING)
    resolved = _payment(db, tenant_id, status=PaymentStatus.SUCCEEDED, created_ts=old_ts)
    in_flight = _payment(db, tenant_id, status=PaymentStatus.PENDING, created_ts=old_ts)
    store.insert_attempt(
        payment_id=in_flight,
        seq=1,
        provider="prava",
        segment="US:USD:prava",
        status=AttemptStatus.PENDING,
        db_path=db,
    )

    rows = store.stranded_payments(db_path=db)
    assert [r["id"] for r in rows] == [stuck]
    assert fresh_pending not in {r["id"] for r in rows}
    assert resolved not in {r["id"] for r in rows}


def test_watcher_accessors_walk_forward_by_id(db: str) -> None:
    tenant_id = _tenant(db)
    payment_id = _payment(db, tenant_id, status=PaymentStatus.FAILED)
    first = store.insert_attempt(
        payment_id=payment_id,
        seq=1,
        provider="stripe_sim",
        segment="US:USD:visa",
        status=AttemptStatus.PENDING,
        db_path=db,
    )
    store.resolve_attempt(first, status=AttemptStatus.FAILED, decline_code="weird_code", db_path=db)

    assert store.latest_attempt_id(db_path=db) == first
    assert store.resolved_attempts_since(0, db_path=db)[0]["decline_code"] == "weird_code"
    assert store.resolved_attempts_since(first, db_path=db) == []

    event_id = store.insert_provider_event("stripe_sim", "health_degraded", db_path=db)
    assert store.latest_provider_event_id(db_path=db) == event_id
    assert store.provider_events_since(event_id - 1, db_path=db)[0]["kind"] == "health_degraded"


def test_list_tenants_returns_them_all(db: str) -> None:
    _tenant(db, "Acme", "ops@acme.test")
    _tenant(db, "Rival", "x@rival.test")
    rows = store.list_tenants(db_path=db)
    assert [(r["name"], r["email"]) for r in rows] == [
        ("Acme", "ops@acme.test"),
        ("Rival", "x@rival.test"),
    ]


# --- agent config -------------------------------------------------------------


def test_agent_config_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.delenv("OPENAI_API_BASE", raising=False)
    monkeypatch.delenv("PAYOPTIMIZE_AGENT_MODEL", raising=False)
    monkeypatch.delenv("PAYOPTIMIZE_AGENT_AUTONOMY", raising=False)
    monkeypatch.delenv("PAYOPTIMIZE_AGENT_TRIGGERS", raising=False)

    assert config.openai_api_key() == ""
    assert config.openai_api_base() == config.DEFAULT_OPENAI_API_BASE
    assert config.agent_model() == config.DEFAULT_AGENT_MODEL
    assert config.agent_autonomy() == "auto"
    assert config.agent_triggers_enabled() is True


def test_agent_autonomy_refuses_junk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAYOPTIMIZE_AGENT_AUTONOMY", "propose")
    assert config.agent_autonomy() == "propose"
    monkeypatch.setenv("PAYOPTIMIZE_AGENT_AUTONOMY", "yolo")
    with pytest.raises(config.ConfigError, match="PAYOPTIMIZE_AGENT_AUTONOMY"):
        config.agent_autonomy()


def test_agent_triggers_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAYOPTIMIZE_AGENT_TRIGGERS", "0")
    assert config.agent_triggers_enabled() is False


def test_secret_values_collects_configured_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRAVA_SECRET_KEY", "sk_test_abc")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-xyz")
    monkeypatch.setenv("PAYOPTIMIZE_USER_ID", "")
    values = config.secret_values()
    assert "sk_test_abc" in values
    assert "sk-openai-xyz" in values
    assert "" not in values
