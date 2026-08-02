"""MCP server: how an agent pays for something.

A thin client over the REST API via the SDK — no business logic lives here. The
agent gets exactly the same surface a merchant's curl does, which is the point:
if the agent can complete a purchase, so can anyone's integration.

The tool docstrings are the agent's only documentation, so they say what the
tool does AND what the agent is expected to do with the result. The important
one is create_payment: a Prava payment cannot complete without a human tapping a
passkey, so the agent must hand the approval URL to its user and then wait. An
agent that treats `pending_approval` as failure will give up two seconds before
the payment succeeds.

Run:  PAYOPTIMIZE_API_URL=… PAYOPTIMIZE_API_KEY=pok_… python -m payoptimize.server
"""

from __future__ import annotations

import os
from typing import Any

from mcp.server import MCPServer

from .sdk import PayOptimizeClient, PayOptimizeError

DEFAULT_API_URL = "http://127.0.0.1:8080"

server: MCPServer = MCPServer(
    name="payoptimize",
    instructions=(
        "Create and track real payments through PayOptimize. Card payments are"
        " routed across simulated providers and resolve immediately. Payments with"
        " method='prava' go through a REAL Prava sandbox integration: they return"
        " status='pending_approval' with an approval_url that a human must open and"
        " approve with a passkey. Always show that URL to the user, then call"
        " wait_for_payment."
    ),
)


def _client() -> PayOptimizeClient:
    api_key = os.environ.get("PAYOPTIMIZE_API_KEY", "").strip()
    if not api_key:
        raise PayOptimizeError(
            "PAYOPTIMIZE_API_KEY is unset — create one with POST /v1/tenants and"
            " put the pok_ key in the environment"
        )
    return PayOptimizeClient(
        os.environ.get("PAYOPTIMIZE_API_URL", DEFAULT_API_URL).strip() or DEFAULT_API_URL,
        api_key,
    )


def _summarise(payment: dict[str, Any]) -> dict[str, Any]:
    """What an agent actually needs, rather than the whole payment record."""
    chain = [
        f"{a['provider']} {'ok' if a['status'] == 'succeeded' else a['decline_code'] or a['status']}"
        for a in payment.get("attempts", [])
    ]
    summary = {
        "payment_id": payment["id"],
        "status": payment["status"],
        "amount_cents": payment["amount_cents"],
        "currency": payment["currency"],
        "provider": payment.get("provider", ""),
        "route": " -> ".join(chain),
    }
    if payment.get("decline_code"):
        summary["decline_code"] = payment["decline_code"]
    if payment.get("iframe_url"):
        summary["approval_url"] = payment["iframe_url"]
    return summary


@server.tool()
def create_payment(
    amount_cents: int,
    currency: str = "USD",
    description: str = "",
    method: str = "prava",
    country: str = "",
    card_brand: str = "",
) -> dict[str, Any]:
    """Create a payment.

    Amounts are in integer cents: $12.50 is 1250, never 12.5.

    method='prava' (the default) uses the REAL Prava sandbox rail. It returns
    status='pending_approval' and an approval_url. This is NOT a failure — the
    payment is waiting for a human. Show the approval_url to the user, ask them
    to open it and approve with their passkey, then call wait_for_payment.

    method='card' routes across simulated providers and resolves immediately.
    It needs country (2-letter, e.g. "US") and card_brand (visa, mastercard, or
    amex), because those pick the routing corridor and cannot be guessed.
    """
    with _client() as client:
        return _summarise(
            client.create_payment(
                amount_cents,
                currency,
                method=method,
                description=description,
                country=country,
                card_brand=card_brand,
            )
        )


@server.tool()
def get_payment(payment_id: str) -> dict[str, Any]:
    """Look up one payment and the providers it was attempted against."""
    with _client() as client:
        return _summarise(client.get_payment(payment_id))


@server.tool()
def wait_for_payment(payment_id: str, timeout_s: float = 180) -> dict[str, Any]:
    """Wait for a payment to finish, then report how it went.

    Use after create_payment returns 'pending_approval'. This blocks while a
    human approves on their phone, so a couple of minutes is normal. If it
    returns still-pending, the human has not approved yet — tell them so rather
    than treating it as a decline.
    """
    with _client() as client:
        return _summarise(client.wait_for_payment(payment_id, timeout_s=timeout_s))


@server.tool()
def ask_ops(question: str) -> dict[str, Any]:
    """Ask PayOptimize's resident ops agent a question about this account's payments.

    It has read access to payments, attempts, provider health, corridor analytics
    and the fee ledger, and answers from that evidence rather than from memory.
    Use it for "why is my auth rate down", "what happened to payment X", "which
    provider is failing" — anything needing several facts joined together.

    It never sees another merchant's data, and it cannot change anything without
    a human approving first.
    """
    with _client() as client:
        result = client.ask_agent(question)
    return {
        "answer": result["answer"],
        "evidence": result.get("evidence", []),
        "actions": result.get("actions", []),
    }


@server.tool()
def diagnose_payment(payment_id: str) -> dict[str, Any]:
    """Explain what happened to one payment and what to do about it.

    Returns a narrative plus the evidence it rests on. If the payment can be
    remediated, the agent may propose an action — proposals wait for a human, so
    tell the user what was proposed rather than reporting it as done.
    """
    with _client() as client:
        result = client.diagnose_payment(payment_id)
    return {
        "answer": result["answer"],
        "evidence": result.get("evidence", []),
        "actions": result.get("actions", []),
    }


@server.tool()
def provider_health() -> list[dict[str, Any]]:
    """Current state of every payment rail.

    `real` distinguishes the one genuine integration (Prava sandbox) from the
    simulated rails. Report that distinction if you describe these to a user.
    """
    with _client() as client:
        return [
            {
                "provider": p["name"],
                "real": p["real"],
                "label": p["label"],
                "state": p["state"],
                "auth_rate": p["auth_rate"],
                "p95_ms": p["p95_ms"],
            }
            for p in client.provider_health()
        ]


@server.tool()
def list_recent_payments(limit: int = 10) -> list[dict[str, Any]]:
    """Recent payments for this API key's merchant account."""
    with _client() as client:
        return [_summarise(p) for p in client.list_payments(limit=limit)]


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
