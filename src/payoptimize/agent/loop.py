"""The agent loop: one question in, one audited answer out.

Synchronous on purpose, like every HTTP-touching module here — the async app
wraps `run` in a threadpool. The loop holds no privileges of its own: it can
only call the tools its ToolBox exposes, and the ToolBox was built with the
caller's tenant scope and this run's redactor. The system prompt teaches the
domain; the boundary does not depend on it.

The audit is unconditional. The run row opens before the first model call and
finishes in a `finally`, so a crash mid-conversation still leaves a record of
what was asked, what was spent, and how far it got.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from .. import config, store
from ..engine import Engine
from . import llm
from .privacy import Redactor
from .tools import ToolBox

MAX_TURNS = 10

CAP_ANSWER = (
    "I hit my tool-call budget before reaching a conclusion. The evidence gathered so"
    " far is recorded in this run's audit trail."
)

SYSTEM_PROMPT = """You are the PayOptimize ops agent, embedded in a payment \
orchestration service. You diagnose payment failures, explain incidents, answer \
merchant questions, and fix operational problems through your action tools.

How this system works:
- Payments with method='card' are routed by a discounted Thompson-sampling bandit \
across three SIMULATED rails (stripe_sim, braintree_sim, adyen_sim). Payments with \
method='prava' ride the one REAL rail (Prava sandbox): a session is minted, a human \
approves with a passkey, and a background poller settles it. Always keep the \
real/simulated distinction explicit when you describe rails — honest labeling is \
product policy.
- Routing corridors are country:currency:card_brand segments (e.g. US:USD:visa). \
The best rail differs per corridor; that is the point of the router.
- Generator traffic is synthetic (source='generator') and split 50/50 between \
routing_mode='router' and 'baseline' (round-robin) to measure uplift honestly. \
Merchant traffic (source='api' or 'agent') always gets the router.
- Decline taxonomy: infrastructure codes (issuer_unavailable, provider_timeout, \
processing_error, rate_limited) cascade to the next rail — up to 3 attempts, never \
the same rail twice. do_not_honor and terminal codes (insufficient_funds, \
expired_card, invalid_card, fraud_suspected, stolen_card) never cascade. Prava \
failures carry Prava's own verbatim codes (e.g. FETCH_AGENTIC_CREDS_ERROR means the \
card on the Prava account cannot mint a Visa cryptogram — in sandbox that means a \
non-test card is enrolled; approval_timeout means no human tapped the passkey in \
time). rail_unavailable/provider_error mean our own startup failed before any rail \
was asked.
- A payment stuck in status='pending' with no attempt in flight is STRANDED: a crash \
or startup failure abandoned it, nothing will ever resolve it, and the correct fix \
is your reconcile_payment action (it resolves the row failed/rail_unavailable; \
merchants are never charged for failures).
- Health states: down (circuit-broken), degraded (bandit routes around it), \
healthy, unknown (not enough traffic). Injected outages on simulated rails are \
operator demos; clearing them is your clear_injection action.

How to work:
- Gather evidence with tools before concluding. Cite payment ids and verbatim \
decline codes. Never invent a payment, a code, or a number.
- Tenants appear as pseudonyms (tenant_3). Use them as-is; never guess identities.
- If an action is warranted, call its tool and report what it did (executed, or \
proposed for human approval). If nothing is wrong, say so plainly.
- Be concise: an operator reads your answer under time pressure."""


def run(
    engine: Engine,
    question: str,
    *,
    tenant_id: int = 0,
    trigger: str = "ask",
    generator: Any = None,
    http: httpx.Client | None = None,
) -> dict[str, Any]:
    """One full agent conversation. Returns the redacted answer, the evidence
    trail, any actions, and usage — everything already safe to display."""
    started = time.monotonic()
    redactor = Redactor.build(db_path=engine.db_path)
    model = config.agent_model()
    safe_question = redactor.redact(question)
    run_id = store.insert_agent_run(
        tenant_id=tenant_id,
        trigger_kind=trigger,
        question=safe_question,
        model=model,
        db_path=engine.db_path,
    )
    box = ToolBox(
        engine=engine, tenant_id=tenant_id, redactor=redactor, run_id=run_id, generator=generator
    )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": safe_question},
    ]
    tools_used: list[str] = []
    evidence: list[dict[str, Any]] = []
    tokens_in = tokens_out = 0
    answer = ""
    try:
        for _ in range(MAX_TURNS):
            reply = llm.complete(
                messages, box.specs(), denylist=redactor.denylist(), model=model, http=http
            )
            tokens_in += reply.tokens_in
            tokens_out += reply.tokens_out
            if not reply.tool_calls:
                answer = reply.content
                break
            messages.append(reply.raw_message)
            for call in reply.tool_calls:
                result = box.dispatch(call.name, call.arguments)
                tools_used.append(call.name)
                evidence.append({"tool": call.name, "arguments": call.arguments, "result": result})
                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": json.dumps(result)}
                )
        else:
            answer = CAP_ANSWER
    finally:
        # The answer is redacted a second time on the way to disk: it is model
        # output, and belt-and-braces is the house style at this boundary.
        safe_answer = redactor.redact(answer)
        store.finish_agent_run(
            run_id,
            answer=safe_answer,
            tools_used=",".join(tools_used),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=int((time.monotonic() - started) * 1000),
            db_path=engine.db_path,
        )
        if config.agent_capture_enabled():
            store.insert_agent_transcript(run_id, json.dumps(messages), db_path=engine.db_path)
    return {
        "run_id": run_id,
        "answer": safe_answer,
        "display_answer": redactor.restore_for(safe_answer, tenant_id)
        if tenant_id
        else safe_answer,
        "evidence": evidence,
        "actions": box.actions_log,
        "usage": {"model": model, "tokens_in": tokens_in, "tokens_out": tokens_out},
    }
