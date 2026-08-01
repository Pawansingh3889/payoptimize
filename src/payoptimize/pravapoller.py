"""Settling Prava payments after a human approves them.

A card rail answers inside the request. Prava cannot: someone has to tap a
passkey on a phone, and no amount of waiting inside an HTTP handler makes that
happen sooner. So a Prava payment is created `pending_approval` with an approval
URL, and this task finishes the job.

The rule this file exists to enforce, learned the hard way in canibuy: **the
moment a credential appears, report_status must be called.** A session that
produced a credential sits in `awaiting_result` until someone reports what the
downstream authorization did. Skip it and the payment hangs there forever — the
customer has approved, the money is committed, and nothing ever settles.

`settle_once` is the whole state machine and takes its clients by argument, so
the tests drive it directly with a MockTransport and never touch the network.
`run` is the loop around it and holds no logic.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import datetime

import httpx
from starlette.concurrency import run_in_threadpool

from . import billing, store
from .models import AttemptStatus, DeclineCode, PaymentStatus, utc_now_iso
from .providers import PRAVA, prava

log = logging.getLogger("payoptimize.prava")

POLL_INTERVAL_SECONDS = 3.0
# How long a human gets to tap the passkey before we give up on the payment.
APPROVAL_DEADLINE_SECONDS = 600.0

# The downstream authorization PayOptimize would perform with the credential
# Prava hands over. We do not have a real acquirer, so this approves — stated
# plainly in the README rather than implied by a green tick on the dashboard.
SIMULATED_DOWNSTREAM_APPROVES = True


def _age_seconds(created_ts: str) -> float:
    started = datetime.fromisoformat(created_ts)
    return (datetime.fromisoformat(utc_now_iso()) - started).total_seconds()


@dataclass
class PravaPoller:
    db_path: str | None = None
    interval: float = POLL_INTERVAL_SECONDS
    deadline: float = APPROVAL_DEADLINE_SECONDS
    settled: int = 0

    async def settle_once(self, *, http: httpx.Client | None = None) -> int:
        """One sweep over everything awaiting a human. Returns how many resolved."""
        pending = store.pending_prava_attempts(db_path=self.db_path)
        resolved = 0
        for row in pending:
            with contextlib.suppress(Exception):
                # One unreachable session must not stall the others, and the
                # next sweep is three seconds away.
                if await self._settle(row, http=http):
                    resolved += 1
        self.settled += resolved
        return resolved

    async def _settle(self, row: dict, *, http: httpx.Client | None = None) -> bool:
        session_id = str(row["prava_session_id"])
        payment_id = str(row["payment_id"])
        attempt_id = int(row["id"])

        if _age_seconds(str(row["created_ts"])) > self.deadline:
            self._fail(attempt_id, payment_id, DeclineCode.APPROVAL_TIMEOUT)
            log.info("prava %s timed out awaiting approval", session_id)
            return True

        result = await run_in_threadpool(prava.payment_result, session_id, http=http)
        status = str(result.get("status", ""))

        found = prava.credentials(result)
        if found:
            # The credential exists, so the obligation exists. Report before
            # touching our own database: a crash between the two must leave
            # Prava settled rather than leave a session hanging in their system.
            credential = found[0]
            txn_ref_id = str(credential.get("txn_id") or credential.get("txn_ref_id") or "")
            outcome = "APPROVED" if SIMULATED_DOWNSTREAM_APPROVES else "DECLINED"
            await run_in_threadpool(prava.report_status, session_id, txn_ref_id, outcome, http=http)
            if outcome == "APPROVED":
                self._succeed(attempt_id, payment_id, txn_ref_id)
            else:
                self._fail(attempt_id, payment_id, "downstream_declined", txn_ref_id)
            log.info("prava %s settled %s (txn %s)", session_id, outcome, txn_ref_id)
            return True

        if status == "failed":
            # Stored verbatim rather than mapped onto our taxonomy: it is the
            # first thing Prava support asks for. A human declining is terminal
            # by design — no cascade, because there is nothing to retry.
            self._fail(attempt_id, payment_id, prava.failure_code(result) or "prava_failed")
            return True

        return False  # still pending or awaiting a credential

    def _succeed(self, attempt_id: int, payment_id: str, txn_ref_id: str) -> None:
        store.resolve_attempt(
            attempt_id,
            status=AttemptStatus.SUCCEEDED,
            prava_txn_id=txn_ref_id,
            db_path=self.db_path,
        )
        store.finalize_payment(
            payment_id,
            status=PaymentStatus.SUCCEEDED,
            final_provider=PRAVA,
            db_path=self.db_path,
        )
        billing.record_fee(payment_id, db_path=self.db_path)

    def _fail(self, attempt_id: int, payment_id: str, code: str, txn_ref_id: str = "") -> None:
        store.resolve_attempt(
            attempt_id,
            status=AttemptStatus.FAILED,
            decline_code=code,
            prava_txn_id=txn_ref_id,
            db_path=self.db_path,
        )
        store.finalize_payment(
            payment_id,
            status=PaymentStatus.FAILED,
            final_provider=PRAVA,
            decline_code=code,
            db_path=self.db_path,
        )

    async def run(self) -> None:
        while True:
            with contextlib.suppress(Exception):
                await self.settle_once()
            await asyncio.sleep(self.interval)
