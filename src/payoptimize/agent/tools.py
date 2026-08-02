"""What the agent can see, and the shape it sees it in.

Every tool is a thin wrapper over an accessor that already exists — the agent
gets no query the REST API could not run. Two properties are enforced by
construction rather than by policy:

  scope     — a ToolBox is built with a tenant_id and every read filters on
              it. A loop built for tenant 3 cannot fetch tenant 4's payment,
              because no code path in this file accepts a foreign tenant id.
              tenant 0 is the system (trigger runs): cross-tenant reads, same
              redaction.
  redaction — every result passes through the run's Redactor on the way out,
              so the model sees pseudonyms and never a name, an email, a key,
              or anything shaped like a card number.

This module imports no store write functions. The three action tools do not
mutate anything either — they hand the request to actions.submit(), where the
whitelist and the guards live.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .. import billing, store
from ..engine import Engine
from ..models import iso_since
from .privacy import Redactor, pseudonym

MAX_LIMIT = 50


def _spec(name: str, description: str, params: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": params,
                "additionalProperties": False,
            },
        },
    }


READ_SPECS: list[dict[str, Any]] = [
    _spec(
        "get_payment",
        "One payment and its full attempt chain (providers tried, decline codes, latency).",
        {"payment_id": {"type": "string", "description": "pay_… id"}},
    ),
    _spec(
        "list_recent_payments",
        "Recent payments, newest first. Optionally filter by status"
        " (pending, pending_approval, succeeded, failed).",
        {
            "limit": {"type": "integer", "description": "max rows, default 20"},
            "status": {"type": "string", "description": "optional status filter"},
        },
    ),
    _spec(
        "analytics_summary",
        "Auth rate, volume, and router-vs-baseline uplift (with its confidence interval)"
        " over a recent window, plus per-provider and per-corridor breakdowns.",
        {"window_minutes": {"type": "integer", "description": "window size, default 15"}},
    ),
    _spec(
        "provider_health",
        "Current health of every rail: state, auth rate, p95 latency, and which rail is"
        " REAL versus SIMULATED.",
        {},
    ),
    _spec(
        "provider_events",
        "Outage injections and health transitions in a recent window — the incident timeline.",
        {"window_minutes": {"type": "integer", "description": "window size, default 60"}},
    ),
    _spec(
        "find_stranded_payments",
        "Payments stuck in `pending` with nothing in flight to resolve them — abandoned by"
        " a crash or a startup failure. These are what the reconcile action may fix.",
        {},
    ),
    _spec(
        "ledger_statement",
        "The merchant's fee statement: recent fee lines and the running total.",
        {"limit": {"type": "integer", "description": "max entries, default 20"}},
    ),
]


def _clamp(limit: Any, default: int) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return default
    return max(1, min(MAX_LIMIT, value))


ACTION_SPECS: list[dict[str, Any]] = [
    _spec(
        "reconcile_payment",
        "Resolve a payment abandoned in `pending` with nothing in flight to finish"
        " it, marking it failed/rail_unavailable. This writes a merchant's record of"
        " what happened to their money, so it is always proposed for a human to"
        " approve — never executed directly. Explain your reasoning in `reason`.",
        {
            "payment_id": {"type": "string", "description": "the stranded payment"},
            "reason": {"type": "string", "description": "why this one is stranded"},
        },
    ),
    _spec(
        "clear_injection",
        "Clear a staged outage or degradation from a SIMULATED provider, returning it"
        " to its normal corridor behaviour. The real rail cannot be cleared or faked.",
        {
            "provider": {"type": "string", "description": "e.g. stripe_sim"},
            "reason": {"type": "string", "description": "why it should be cleared"},
        },
    ),
    _spec(
        "set_generator_rate",
        "Change the synthetic traffic rate in transactions per second (0 < tps <= 5)."
        " Affects only the demo traffic generator, never a merchant's payments.",
        {
            "tps": {"type": "number", "description": "transactions per second"},
            "reason": {"type": "string", "description": "why this rate"},
        },
    ),
]


@dataclass
class ToolBox:
    """One run's tools: engine + tenant scope + redactor, bound at construction."""

    engine: Engine
    tenant_id: int
    redactor: Redactor
    run_id: int = 0
    generator: Any = None
    # What the action tools did this run, for the caller's response.
    actions_log: list[dict[str, Any]] = field(default_factory=list)

    def specs(self) -> list[dict[str, Any]]:
        return list(READ_SPECS) + list(ACTION_SPECS)

    def dispatch(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            return {"error": f"unknown tool {name!r}"}
        try:
            result = handler(**arguments)
        except TypeError as exc:
            # The model sent arguments the tool does not take. Tell it, so the
            # next iteration can correct course instead of the run dying.
            result = {"error": f"bad arguments for {name}: {exc}"}
        return self.redactor.redact_value(result)

    # --- views ---------------------------------------------------------------

    def _payment_view(self, row: dict[str, Any]) -> dict[str, Any]:
        attempts = store.attempts_for_payment(str(row["id"]), db_path=self.engine.db_path)
        return {
            "id": row["id"],
            "tenant": pseudonym(int(row["tenant_id"])),
            "status": row["status"],
            "amount_cents": row["amount_cents"],
            "currency": row["currency"],
            "method": row["method"],
            "routing_mode": row["routing_mode"],
            "segment": row["segment"],
            "provider": row["final_provider"],
            "decline_code": row["decline_code"],
            "source": row["source"],
            "description": row["description"],
            "created_ts": row["created_ts"],
            "resolved_ts": row["resolved_ts"],
            "attempts": [
                {
                    "seq": a["seq"],
                    "provider": a["provider"],
                    "status": a["status"],
                    "decline_code": a["decline_code"],
                    "latency_ms": a["latency_ms"],
                }
                for a in attempts
            ],
        }

    # --- read tools ----------------------------------------------------------

    def _tool_get_payment(self, payment_id: str = "") -> dict[str, Any]:
        row = store.get_payment(str(payment_id), db_path=self.engine.db_path)
        # Foreign and nonexistent are indistinguishable, same as the API.
        if row is None or (self.tenant_id and int(row["tenant_id"]) != self.tenant_id):
            return {"error": f"no such payment {payment_id!r}"}
        return self._payment_view(row)

    def _tool_list_recent_payments(self, limit: int = 20, status: str = "") -> dict[str, Any]:
        rows = store.list_payments(
            tenant_id=self.tenant_id or None,
            status=status or None,
            limit=_clamp(limit, 20),
            db_path=self.engine.db_path,
        )
        return {"payments": [self._payment_view(row) for row in rows]}

    def _tool_analytics_summary(self, window_minutes: int = 15) -> dict[str, Any]:
        from ..api import uplift_stat  # runtime import: api imports this package

        since = iso_since(_clamp(window_minutes, 15) * 60)
        by_mode = {
            row["routing_mode"]: row
            for row in store.payments_by_mode(
                since=since,
                tenant_id=self.tenant_id or None,
                method="card",
                db_path=self.engine.db_path,
            )
        }
        counts = {
            mode: (
                int(by_mode[mode]["succeeded"] or 0) if mode in by_mode else 0,
                int(by_mode[mode]["volume"]) if mode in by_mode else 0,
            )
            for mode in ("router", "baseline")
        }
        providers = [
            {
                "provider": row["provider"],
                "attempts": int(row["attempts"]),
                "succeeded": int(row["succeeded"] or 0),
                "avg_latency_ms": int(row["avg_latency_ms"] or 0),
            }
            for row in store.attempts_by_provider(since=since, db_path=self.engine.db_path)
        ]
        corridors = [
            {
                "segment": row["segment"],
                "routing_mode": row["routing_mode"],
                "volume": int(row["volume"]),
                "succeeded": int(row["succeeded"] or 0),
            }
            for row in store.payments_by_corridor(
                since=since,
                tenant_id=self.tenant_id or None,
                method="card",
                db_path=self.engine.db_path,
            )
        ]
        return {
            "window_minutes": _clamp(window_minutes, 15),
            "by_mode": {mode: {"succeeded": ok, "volume": n} for mode, (ok, n) in counts.items()},
            "uplift": uplift_stat(*counts["router"], *counts["baseline"]),
            "by_provider": providers,
            "by_corridor": corridors,
        }

    def _tool_provider_health(self) -> dict[str, Any]:
        return {"providers": self.engine.health.snapshot(log_transitions=False)}

    def _tool_provider_events(self, window_minutes: int = 60) -> dict[str, Any]:
        since = iso_since(_clamp(window_minutes, 60) * 60)
        events = store.recent_provider_events(since=since, db_path=self.engine.db_path)
        return {"events": events}

    def _tool_find_stranded_payments(self) -> dict[str, Any]:
        rows = store.stranded_payments(db_path=self.engine.db_path)
        if self.tenant_id:
            rows = [row for row in rows if int(row["tenant_id"]) == self.tenant_id]
        return {"stranded": [self._payment_view(row) for row in rows]}

    def _tool_ledger_statement(self, limit: int = 20) -> dict[str, Any]:
        if not self.tenant_id:
            return {"error": "system runs have no merchant ledger — ask as a tenant"}
        return billing.statement(
            self.tenant_id, limit=_clamp(limit, 20), db_path=self.engine.db_path
        )

    # --- actions -------------------------------------------------------------
    #
    # Each records an intention and hands it to actions.submit, which decides
    # whether this kind may self-execute. The tool result tells the model what
    # actually happened, so it can say so in its answer rather than claiming a
    # fix that is still waiting for a human.

    def _submit(self, kind: str, params: dict[str, Any], reason: str) -> dict[str, Any]:
        from . import actions

        try:
            outcome = actions.submit(
                self.engine,
                self.generator,
                run_id=self.run_id,
                kind=kind,
                params=params,
                rationale=reason,
            )
        except actions.ActionRefused as exc:
            return {"status": "refused", "detail": str(exc)}
        self.actions_log.append({"kind": kind, "params": params, **outcome})
        return outcome

    def _tool_reconcile_payment(self, payment_id: str = "", reason: str = "") -> dict[str, Any]:
        return self._submit(
            "reconcile", {"payment_id": str(payment_id)}, reason or "stranded payment"
        )

    def _tool_clear_injection(self, provider: str = "", reason: str = "") -> dict[str, Any]:
        return self._submit(
            "clear_injection", {"provider": str(provider)}, reason or "injection no longer needed"
        )

    def _tool_set_generator_rate(self, tps: float = 0.0, reason: str = "") -> dict[str, Any]:
        return self._submit("generator_rate", {"tps": tps}, reason or "rate adjustment")
