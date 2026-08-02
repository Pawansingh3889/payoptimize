"""Calling the agent when nobody asked it to.

An ops agent that only answers questions is a chatbot. This is the part that
makes it an operator: it watches the same evidence a human would — decline codes
it does not recognise, health transitions, payments abandoned mid-flight — and
writes up what happened before anyone thinks to look.

Two rules shape everything here.

**The money path must not notice.** The watcher is a lifespan task like the
Prava poller: every sweep is wrapped, every failure is swallowed and logged, and
the next one is fifteen seconds away. An OpenAI outage, a rate limit, or a bug
in this file has exactly one consequence — no narrative gets written. Payments
route, cascade, settle and bill identically.

**No backfill, no repetition.** High-water marks start at whatever exists on the
first sweep, so deploying this does not diagnose a month of history in one
burst. Stranded payments carry a seen-set because in propose mode the same
payment stays stranded until a human approves, and re-proposing it every fifteen
seconds would bury the operator it is meant to help.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from typing import Any

from starlette.concurrency import run_in_threadpool

from .. import config, store
from ..engine import RAIL_UNAVAILABLE_CODE, STARTUP_ERROR_CODE, Engine
from ..models import DeclineCode
from . import loop

log = logging.getLogger("payoptimize.agent.triggers")

SWEEP_SECONDS = 15.0
# A sweep that fires ten diagnoses is a sweep that hides the one that mattered,
# and a burst of model calls behind a payments incident is the wrong instinct.
MAX_RUNS_PER_SWEEP = 3

# Codes the system already understands. Anything else is worth a human-readable
# explanation, because it means a rail told us something we have no vocabulary
# for — exactly how FETCH_AGENTIC_CREDS_ERROR arrived.
KNOWN_CODES = (
    set(DeclineCode)
    | {RAIL_UNAVAILABLE_CODE, STARTUP_ERROR_CODE}
    | {"prava_failed", "downstream_declined", ""}
)

HEALTH_KINDS = ("health_degraded", "health_recovered")

UNKNOWN_DECLINE = "unknown_decline"
HEALTH_EVENT = "health_event"
STRANDED = "stranded"


@dataclass
class TriggerWatcher:
    """Watches for things worth explaining. Holds all logic in `watch_once`."""

    engine: Engine
    generator: Any = None
    interval: float = SWEEP_SECONDS
    max_runs: int = MAX_RUNS_PER_SWEEP
    attempt_mark: int = 0
    event_mark: int = 0
    seen_stranded: set[str] = field(default_factory=set)
    runs: int = 0
    started: bool = False

    def prime(self) -> None:
        """Start from now. Deploying the agent must not diagnose history."""
        self.attempt_mark = store.latest_attempt_id(db_path=self.engine.db_path)
        self.event_mark = store.latest_provider_event_id(db_path=self.engine.db_path)
        self.started = True

    async def watch_once(self, *, http: Any = None) -> list[int]:
        """One sweep. Returns the run ids it created, for tests and logging."""
        if not self.started:
            self.prime()
            return []

        created: list[int] = []
        for question, trigger in self._pending()[: self.max_runs]:
            run_id = await self._explain(question, trigger, http=http)
            if run_id:
                created.append(run_id)
        self.runs += len(created)
        return created

    def _pending(self) -> list[tuple[str, str]]:
        """Everything worth a narrative this sweep, newest evidence first.

        Reads only — deciding what to explain must never be able to change
        anything, so the high-water marks advance here whether or not the model
        is reachable. A model outage costs a narrative, not a repeated sweep.
        """
        db = self.engine.db_path
        work: list[tuple[str, str]] = []

        attempts = store.resolved_attempts_since(self.attempt_mark, db_path=db)
        if attempts:
            self.attempt_mark = max(int(a["id"]) for a in attempts)
        for attempt in attempts:
            code = str(attempt["decline_code"])
            if attempt["status"] == "failed" and code not in KNOWN_CODES:
                question = (
                    f"Payment {attempt['payment_id']} failed on {attempt['provider']} with"
                    f" the decline code {code!r}, which this system does not recognise."
                    " Investigate what happened and explain it for an operator."
                )
                work.append((question, UNKNOWN_DECLINE))

        events = store.provider_events_since(self.event_mark, db_path=db)
        if events:
            self.event_mark = max(int(e["id"]) for e in events)
        for event in events:
            if str(event["kind"]) in HEALTH_KINDS:
                question = (
                    f"Provider {event['provider']} changed health state"
                    f" ({event['kind']}, {event['detail']}). Explain what happened and"
                    " whether anything should be done."
                )
                work.append((question, HEALTH_EVENT))

        stranded = [
            p for p in store.stranded_payments(db_path=db) if str(p["id"]) not in self.seen_stranded
        ]
        if stranded:
            # Marked seen on sight, not on resolution: in propose mode these stay
            # stranded until a human approves, and re-proposing every fifteen
            # seconds would bury the operator this is meant to help.
            self.seen_stranded.update(str(p["id"]) for p in stranded)
            ids = ", ".join(str(p["id"]) for p in stranded[:5])
            question = (
                f"These payments are stuck in `pending` with nothing in flight to"
                f" finish them: {ids}. Work out why, and reconcile them if they are"
                " genuinely abandoned."
            )
            work.append((question, STRANDED))
        return work

    async def _explain(self, question: str, trigger: str, *, http: Any = None) -> int:
        """One agent run. A failure here is logged and dropped — see the module
        docstring: the money path must not notice."""
        try:
            result = await run_in_threadpool(
                loop.run,
                self.engine,
                question,
                tenant_id=0,
                trigger=trigger,
                generator=self.generator,
                http=http,
            )
        except Exception as exc:  # noqa: BLE001 — deliberate, see below
            # The agent is an observer. Anything it can raise — a model outage, a
            # rate limit, a bug in a tool — must cost a narrative and nothing
            # else. Never let it reach the caller or the lifespan task.
            log.warning("agent trigger %s failed: %s", trigger, exc)
            return 0
        return int(result["run_id"])

    async def run(self) -> None:
        while True:
            with contextlib.suppress(Exception):
                await self.watch_once()
            await asyncio.sleep(self.interval)


def enabled() -> bool:
    """Triggers need both a switch and a key. Either missing is a normal,
    working deployment that simply does not narrate itself."""
    return bool(config.agent_triggers_enabled() and config.openai_api_key())
