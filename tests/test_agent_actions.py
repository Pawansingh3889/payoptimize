"""What the agent may change, and what it must ask permission for.

These are the tests that matter most in the whole agent package: everything else
is the model reading. This is the model writing.
"""

from __future__ import annotations

import json
import random
from collections.abc import Iterator

import pytest
from starlette.testclient import TestClient

from payoptimize import store
from payoptimize.agent import actions
from payoptimize.api import create_app
from payoptimize.engine import Engine
from payoptimize.models import PaymentStatus, utc_now_iso

ADMIN = {"Authorization": "Bearer test-admin"}


@pytest.fixture
def engine(db: str) -> Engine:
    built = Engine.build(db_path=db, rng=random.Random(42), latency_scale=0, with_prava=True)
    built.boot()
    return built


@pytest.fixture
def client(db: str) -> Iterator[TestClient]:
    built = Engine.build(db_path=db, rng=random.Random(42), latency_scale=0, with_prava=True)
    with TestClient(create_app(engine=built)) as client:
        yield client


def _tenant(db: str) -> int:
    existing = store.tenant_by_email("a@acme.test", db_path=db)
    if existing:
        return int(existing["id"])
    return store.create_tenant_with_key("Acme", "a@acme.test", "hash", "pok_x…", db_path=db)


def _stranded(db: str, payment_id: str = "pay_stranded", *, age_seconds: int = 3600) -> str:
    """A payment abandoned in `pending` with nothing in flight — exactly what
    happened twice on production when the Prava rail raised at startup."""
    from datetime import UTC, datetime, timedelta

    store.insert_payment(
        payment_id=payment_id,
        tenant_id=_tenant(db),
        amount_cents=1250,
        currency="USD",
        country="",
        card_brand="",
        method="prava",
        routing_mode="router",
        segment=":USD:prava",
        status=PaymentStatus.PENDING,
        source="agent",
        db_path=db,
    )
    old = (datetime.now(UTC) - timedelta(seconds=age_seconds)).isoformat(timespec="milliseconds")
    with store.transaction(db) as conn:
        conn.execute("UPDATE payments SET created_ts = ? WHERE id = ?", (old, payment_id))
    return payment_id


def _run(db: str) -> int:
    return store.insert_agent_run(
        tenant_id=0, trigger_kind="test", question="", model="test", db_path=db
    )


# --- the autonomy split ------------------------------------------------------


def test_money_actions_never_self_execute(engine: Engine, db: str) -> None:
    """The one action that writes a payment's final state always waits for a
    human — whatever the environment says."""
    assert actions.ACTION_AUTONOMY["reconcile"] == "propose"

    payment_id = _stranded(db)
    result = actions.submit(
        engine,
        None,
        run_id=_run(db),
        kind="reconcile",
        params={"payment_id": payment_id},
        rationale="stranded",
    )

    assert result["status"] == "proposed"
    assert store.get_payment(payment_id, db_path=db)["status"] == PaymentStatus.PENDING


def test_reversible_actions_execute_immediately(engine: Engine, db: str) -> None:
    engine.providers["stripe_sim"].inject("degraded", auth_rate=0.45)

    result = actions.submit(
        engine,
        None,
        run_id=_run(db),
        kind="clear_injection",
        params={"provider": "stripe_sim"},
        rationale="demo over",
    )

    assert result["status"] == "executed"
    assert engine.providers["stripe_sim"].state == "healthy"


def test_the_env_switch_downgrades_everything_but_upgrades_nothing(
    engine: Engine, db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PAYOPTIMIZE_AGENT_AUTONOMY", "propose")
    assert actions.autonomy_for("clear_injection") == "propose"
    assert actions.autonomy_for("reconcile") == "propose"

    monkeypatch.setenv("PAYOPTIMIZE_AGENT_AUTONOMY", "auto")
    assert actions.autonomy_for("clear_injection") == "auto"
    # There is deliberately no environment value that promotes this one.
    assert actions.autonomy_for("reconcile") == "propose"


# --- guards ------------------------------------------------------------------


def test_a_resolved_payment_cannot_be_reconciled(engine: Engine, db: str) -> None:
    """A proposal can sit for an hour. If the payment resolved itself in the
    meantime, approving must do nothing."""
    payment_id = _stranded(db)
    action_id = store.insert_agent_action(
        run_id=_run(db),
        kind="reconcile",
        params=json.dumps({"payment_id": payment_id}),
        rationale="x",
        db_path=db,
    )
    store.finalize_payment(payment_id, status=PaymentStatus.SUCCEEDED, db_path=db)

    outcome = actions.execute(
        engine, None, action_id=action_id, kind="reconcile", params={"payment_id": payment_id}
    )

    assert outcome["refused"] is True
    assert "not pending" in outcome["detail"]
    assert store.get_payment(payment_id, db_path=db)["status"] == PaymentStatus.SUCCEEDED


def test_a_recent_pending_payment_is_not_stranded(engine: Engine, db: str) -> None:
    """Something may still be in flight. Only an abandoned payment qualifies."""
    payment_id = _stranded(db, "pay_fresh", age_seconds=1)
    action_id = store.insert_agent_action(
        run_id=_run(db),
        kind="reconcile",
        params=json.dumps({"payment_id": payment_id}),
        rationale="x",
        db_path=db,
    )

    outcome = actions.execute(
        engine, None, action_id=action_id, kind="reconcile", params={"payment_id": payment_id}
    )

    assert outcome["refused"] is True
    assert store.get_payment(payment_id, db_path=db)["status"] == PaymentStatus.PENDING


def test_the_real_rail_cannot_be_touched(engine: Engine, db: str) -> None:
    """PravaProvider has no inject(), so the agent physically cannot stage or
    clear anything on the one rail where a badge claims REAL."""
    assert "prava" in engine.providers

    result = actions.submit(
        engine,
        None,
        run_id=_run(db),
        kind="clear_injection",
        params={"provider": "prava"},
        rationale="try it",
    )

    assert result["status"] == "proposed"  # refused by the guard, left for a human
    assert "cannot be faked" in result["detail"]


@pytest.mark.parametrize("tps", [0, -1, 50, 5.1, "fast"])
def test_generator_rate_is_bounded(engine: Engine, db: str, tps: object) -> None:
    class _Gen:
        def set_rate(self, value: float) -> None:
            raise AssertionError("guard should have refused before this")

    result = actions.submit(
        engine,
        _Gen(),
        run_id=_run(db),
        kind="generator_rate",
        params={"tps": tps},
        rationale="x",
    )
    assert result["status"] == "proposed"


def test_an_unknown_action_is_refused_by_name(engine: Engine, db: str) -> None:
    with pytest.raises(actions.ActionRefused, match="unknown action"):
        actions.submit(
            engine, None, run_id=_run(db), kind="drop_database", params={}, rationale="x"
        )


def test_an_executor_fault_marks_the_row_failed_not_pending(engine: Engine, db: str) -> None:
    """A half-run action that still reads as pending is how the same effect gets
    applied twice."""

    class _Gen:
        def set_rate(self, value: float) -> None:
            raise RuntimeError("generator exploded")

    action_id = store.insert_agent_action(
        run_id=_run(db),
        kind="generator_rate",
        params=json.dumps({"tps": 3}),
        rationale="x",
        db_path=db,
    )
    outcome = actions.execute(
        engine, _Gen(), action_id=action_id, kind="generator_rate", params={"tps": 3}
    )

    assert outcome["status"] == "failed"
    assert "exploded" in outcome["detail"]
    assert store.get_agent_action(action_id, db_path=db)["status"] == "failed"


# --- approval ----------------------------------------------------------------


def test_approving_runs_the_guard_and_resolves_the_payment(engine: Engine, db: str) -> None:
    payment_id = _stranded(db)
    submitted = actions.submit(
        engine,
        None,
        run_id=_run(db),
        kind="reconcile",
        params={"payment_id": payment_id},
        rationale="stranded",
    )

    outcome = actions.approve(engine, None, action_id=submitted["action_id"])

    assert outcome["status"] == "executed"
    row = store.get_payment(payment_id, db_path=db)
    assert row["status"] == PaymentStatus.FAILED
    assert row["decline_code"] == "rail_unavailable"


def test_a_proposal_can_only_be_approved_once(engine: Engine, db: str) -> None:
    """Two operators clicking at once must not reconcile twice."""
    payment_id = _stranded(db)
    submitted = actions.submit(
        engine,
        None,
        run_id=_run(db),
        kind="reconcile",
        params={"payment_id": payment_id},
        rationale="stranded",
    )
    actions.approve(engine, None, action_id=submitted["action_id"])

    with pytest.raises(store.NotFoundError):
        actions.approve(engine, None, action_id=submitted["action_id"])


# --- the admin surface -------------------------------------------------------


def test_admin_endpoints_are_guarded(client: TestClient) -> None:
    assert client.get("/admin/agent/actions").status_code == 401
    assert client.post("/admin/agent/actions/1", json={"decision": "approved"}).status_code == 401


def test_approve_over_http_resolves_a_stranded_payment(client: TestClient, db: str) -> None:
    payment_id = _stranded(db)
    engine = client.app.state.engine
    submitted = actions.submit(
        engine,
        None,
        run_id=_run(db),
        kind="reconcile",
        params={"payment_id": payment_id},
        rationale="stranded on production",
    )

    listed = client.get("/admin/agent/actions", headers=ADMIN).json()["actions"]
    assert any(a["id"] == submitted["action_id"] and a["status"] == "proposed" for a in listed)

    response = client.post(
        f"/admin/agent/actions/{submitted['action_id']}",
        json={"decision": "approved"},
        headers=ADMIN,
    )

    assert response.status_code == 200
    assert response.json()["status"] == "executed"
    assert store.get_payment(payment_id, db_path=db)["status"] == PaymentStatus.FAILED


def test_rejecting_records_the_decision_and_runs_nothing(client: TestClient, db: str) -> None:
    payment_id = _stranded(db)
    engine = client.app.state.engine
    submitted = actions.submit(
        engine,
        None,
        run_id=_run(db),
        kind="reconcile",
        params={"payment_id": payment_id},
        rationale="x",
    )

    response = client.post(
        f"/admin/agent/actions/{submitted['action_id']}",
        json={"decision": "rejected"},
        headers=ADMIN,
    )

    assert response.status_code == 200
    assert store.get_payment(payment_id, db_path=db)["status"] == PaymentStatus.PENDING
    assert store.get_agent_action(submitted["action_id"], db_path=db)["status"] == "rejected"


def test_a_second_decision_is_a_404(client: TestClient, db: str) -> None:
    engine = client.app.state.engine
    submitted = actions.submit(
        engine,
        None,
        run_id=_run(db),
        kind="reconcile",
        params={"payment_id": _stranded(db)},
        rationale="x",
    )
    path = f"/admin/agent/actions/{submitted['action_id']}"
    assert client.post(path, json={"decision": "approved"}, headers=ADMIN).status_code == 200
    assert client.post(path, json={"decision": "rejected"}, headers=ADMIN).status_code == 404


@pytest.mark.parametrize("decision", ["maybe", "", "APPROVED "])
def test_a_nonsense_decision_is_refused(client: TestClient, decision: str) -> None:
    response = client.post("/admin/agent/actions/1", json={"decision": decision}, headers=ADMIN)
    assert response.status_code == 422


def test_the_ledger_is_untouched_by_a_reconcile(engine: Engine, db: str) -> None:
    """Reconciling marks a payment failed. Declines are never billed, so no fee
    line may appear — the invariant billing.py exists to protect."""
    payment_id = _stranded(db)
    before = store.ledger_totals(tenant_id=_tenant(db), db_path=db)

    submitted = actions.submit(
        engine,
        None,
        run_id=_run(db),
        kind="reconcile",
        params={"payment_id": payment_id},
        rationale="x",
    )
    actions.approve(engine, None, action_id=submitted["action_id"])

    assert store.ledger_totals(tenant_id=_tenant(db), db_path=db) == before
    assert utc_now_iso()  # timestamps still sane


def test_the_agent_package_has_exactly_one_write_path() -> None:
    """The plan promised a grep-level guarantee, so it is asserted rather than
    trusted: the agent may mutate payment state in exactly one place, inside a
    guarded executor that never self-executes.

    If this fails, someone gave the LLM a second way to change the database and
    the whole propose-and-approve story is no longer true.
    """
    import pathlib

    writes = (
        "finalize_payment",
        "insert_payment",
        "resolve_attempt",
        "insert_attempt",
        "record_fee",
        "set_payment_status",
        "attach_prava_session",
    )
    package = pathlib.Path("src/payoptimize/agent")
    found: list[str] = []
    for path in sorted(package.glob("*.py")):
        for number, line in enumerate(path.read_text().splitlines(), 1):
            code = line.split("#", 1)[0]
            if any(f"store.{w}(" in code for w in writes):
                found.append(f"{path.name}:{number}")

    assert found == ["actions.py:94"] or len(found) == 1, (
        f"expected exactly one write path in the agent package, found: {found}"
    )
    assert found[0].startswith("actions.py"), "the write must live behind the action guards"
