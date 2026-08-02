"""What the agent is allowed to change, and who has to say yes.

Three things constrain every mutation the agent can cause:

**A whitelist, not a capability.** The agent cannot run SQL, call arbitrary
functions, or reach the money path. It can name one of three actions; anything
else is refused by name before a guard even runs.

**Guards re-checked at execution, never at proposal.** A proposal can sit for an
hour before a human approves it, and the world moves. Every guard re-reads live
state at the moment of execution — a payment that resolved itself in the interim
is no longer reconcilable, and the approval does nothing.

**Autonomy split by blast radius.** A single global switch would force one answer
for actions with wildly different consequences. Clearing an injection or nudging
the generator is reversible in one call and touches only demo scaffolding.
Writing a payment's final state is not reversible and is the merchant's record of
what happened to their money — so it always waits for a human, whatever the
environment says.
"""

from __future__ import annotations

import json
from typing import Any

from .. import config, store
from ..engine import RAIL_UNAVAILABLE_CODE, Engine
from ..models import PaymentStatus

RECONCILE = "reconcile"
CLEAR_INJECTION = "clear_injection"
GENERATOR_RATE = "generator_rate"

# Blast radius, not a single dial. There is deliberately no environment value
# that promotes `reconcile` to auto: changing that means editing this table in a
# reviewed commit, which is the point.
ACTION_AUTONOMY = {
    CLEAR_INJECTION: "auto",
    GENERATOR_RATE: "auto",
    RECONCILE: "propose",
}

# The generator exists to make the dashboard move. Anything past this is a load
# test nobody asked for, on a single-worker process holding one SQLite file.
MAX_GENERATOR_TPS = 5.0

PROPOSED = "proposed"
EXECUTED = "executed"
FAILED = "failed"


class ActionRefused(RuntimeError):
    """A guard said no. Not an error in the agent — the answer to a question."""


def autonomy_for(kind: str) -> str:
    """`auto` only when both the action's own blast radius and the environment
    allow it. PAYOPTIMIZE_AGENT_AUTONOMY=propose is a kill switch that can
    downgrade everything; it can never upgrade anything."""
    if config.agent_autonomy() == "propose":
        return "propose"
    return ACTION_AUTONOMY.get(kind, "propose")


# --- guards ------------------------------------------------------------------
#
# Each returns a human-readable description of what it did, or raises
# ActionRefused. They read live state every time they run.


def _do_reconcile(engine: Engine, generator: Any, params: dict[str, Any]) -> str:
    payment_id = str(params.get("payment_id", ""))
    if not payment_id:
        raise ActionRefused("reconcile needs a payment_id")

    row = store.get_payment(payment_id, db_path=engine.db_path)
    if row is None:
        raise ActionRefused(f"no payment {payment_id}")
    if row["status"] != PaymentStatus.PENDING:
        raise ActionRefused(
            f"{payment_id} is {row['status']}, not pending — only an abandoned"
            " payment can be reconciled, and this one resolved itself"
        )
    # The same definition the query uses, re-checked: a pending attempt means
    # something is still in flight and will finish the job on its own.
    stranded = {p["id"] for p in store.stranded_payments(db_path=engine.db_path)}
    if payment_id not in stranded:
        raise ActionRefused(
            f"{payment_id} is pending but not stranded — an attempt is still in"
            " flight, or it is too recent to have been abandoned"
        )

    store.finalize_payment(
        payment_id,
        status=PaymentStatus.FAILED,
        decline_code=RAIL_UNAVAILABLE_CODE,
        db_path=engine.db_path,
    )
    return f"{payment_id} resolved failed/{RAIL_UNAVAILABLE_CODE}"


def _do_clear_injection(engine: Engine, generator: Any, params: dict[str, Any]) -> str:
    name = str(params.get("provider", ""))
    provider = engine.providers.get(name)
    # hasattr(inject) is what structurally excludes the real rail: PravaProvider
    # has no inject(), so the agent cannot stage or clear anything on it. Same
    # check api.admin_outage makes, for the same reason.
    if provider is None or not hasattr(provider, "inject"):
        raise ActionRefused(
            f"no injectable provider {name!r} — the real rail cannot be faked or cleared"
        )
    provider.inject("clear")
    store.insert_provider_event(name, "cleared", detail="agent", db_path=engine.db_path)
    return f"{name} injection cleared"


def _do_generator_rate(engine: Engine, generator: Any, params: dict[str, Any]) -> str:
    if generator is None:
        raise ActionRefused("no generator is running")
    try:
        tps = float(params.get("tps", 0))
    except (TypeError, ValueError) as exc:
        raise ActionRefused(f"tps must be a number: {params.get('tps')!r}") from exc
    if not 0 < tps <= MAX_GENERATOR_TPS:
        raise ActionRefused(f"tps must be between 0 and {MAX_GENERATOR_TPS}, got {tps}")
    generator.set_rate(tps)
    return f"generator rate set to {tps} tx/s"


EXECUTORS = {
    RECONCILE: _do_reconcile,
    CLEAR_INJECTION: _do_clear_injection,
    GENERATOR_RATE: _do_generator_rate,
}


# --- submit / execute --------------------------------------------------------


def submit(
    engine: Engine,
    generator: Any,
    *,
    run_id: int,
    kind: str,
    params: dict[str, Any],
    rationale: str,
) -> dict[str, Any]:
    """Record an intention, and execute it only if this kind may self-execute.

    Always writes the action row first. A proposal that was refused by its guard
    stays `proposed` rather than becoming `failed`: the world may change, and a
    human can approve it later, at which point the guard runs again.
    """
    if kind not in EXECUTORS:
        raise ActionRefused(f"unknown action {kind!r}")

    action_id = store.insert_agent_action(
        run_id=run_id,
        kind=kind,
        params=json.dumps(params, sort_keys=True),
        rationale=rationale,
        db_path=engine.db_path,
    )
    mode = autonomy_for(kind)
    if mode != "auto":
        return {
            "action_id": action_id,
            "status": PROPOSED,
            "detail": "waiting for a human to approve",
        }

    outcome = execute(engine, generator, action_id=action_id, kind=kind, params=params)
    return {"action_id": action_id, **outcome}


def execute(
    engine: Engine,
    generator: Any,
    *,
    action_id: int,
    kind: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Run one action's guard and effect, and record how it went.

    A refusal leaves the row `proposed` — it is a "not now", not a failure. Any
    other exception is a genuine fault and marks the row `failed` with the
    reason, because an action that half-happened must not look pending.
    """
    executor = EXECUTORS.get(kind)
    if executor is None:
        raise ActionRefused(f"unknown action {kind!r}")
    try:
        detail = executor(engine, generator, params)
    except ActionRefused as exc:
        store.mark_agent_action(action_id, status=PROPOSED, detail=str(exc), db_path=engine.db_path)
        return {"status": PROPOSED, "detail": str(exc), "refused": True}
    except Exception as exc:  # noqa: BLE001 — see below
        # Deliberately broad. An executor reaches SQLite, a provider adapter and
        # the generator; any of them can raise something unforeseen. Whatever it
        # was, the row must not be left looking `proposed`, because a half-run
        # action that still reads as pending is how the same effect gets applied
        # twice. Record it as failed with the reason and let the caller see it.
        store.mark_agent_action(
            action_id, status=FAILED, detail=str(exc)[:200], db_path=engine.db_path
        )
        return {"status": FAILED, "detail": str(exc)[:200]}
    store.mark_agent_action(action_id, status=EXECUTED, detail=detail, db_path=engine.db_path)
    return {"status": EXECUTED, "detail": detail}


def approve(engine: Engine, generator: Any, *, action_id: int) -> dict[str, Any]:
    """A human said yes. Transition exactly once, then run the guard afresh."""
    row = store.get_agent_action(action_id, db_path=engine.db_path)
    if row is None:
        raise ActionRefused(f"no action {action_id}")
    # Exactly-once lives in the SQL: two concurrent approvals race on one UPDATE
    # and the loser gets NotFoundError rather than a second execution.
    store.decide_agent_action(action_id, status="approved", db_path=engine.db_path)
    return execute(
        engine,
        generator,
        action_id=action_id,
        kind=str(row["kind"]),
        params=json.loads(str(row["params"]) or "{}"),
    )
