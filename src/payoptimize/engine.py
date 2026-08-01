"""Payment execution: the cascade loop, the writes, and the learning.

This module exists because two callers need the same code path — the REST API
and the traffic generator — and importing the Starlette app from the generator
that the app itself starts would be a cycle. PLAN §2's layout does not name it;
the alternative was duplicating the cascade in two places, which is how the two
arms of an A/B test quietly stop being comparable.

The split of responsibilities is the one CLAUDE.md sets out: router.py decides,
store.py persists, providers charge, and this file is the only thing that knows
the order those happen in.

It also holds the service's mutable state — the bandit posteriors and the
provider registry — which is the concrete reason the process may never run more
than one uvicorn worker.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

import httpx

from . import billing, config, store
from .health import HealthMonitor
from .models import (
    AttemptStatus,
    AttemptView,
    PaymentMethod,
    PaymentRequest,
    PaymentResponse,
    PaymentSource,
    PaymentStatus,
    RoutingMode,
    new_payment_id,
)
from .providers import PRAVA, ChargeRequest, ProviderAdapter, build_registry, prava
from .router import REBUILD_LIMIT, Router, assign_mode


class RailUnavailable(RuntimeError):
    """A payment asked for a rail this deployment has not configured."""


def prava_configured() -> bool:
    """Whether the real rail can be offered at all.

    A deployment without a Prava key is a normal, working deployment — the card
    rails do not need one. It just cannot honour method="prava", and saying so
    with a 503 beats registering an adapter that fails on first use.
    """
    try:
        prava.key_mode()
        prava._user_identity()
    except prava.PravaError:
        return False
    return True


@dataclass
class Engine:
    """Everything the service knows that is not in the database."""

    rng: random.Random
    providers: dict[str, ProviderAdapter]
    router: Router
    health: HealthMonitor
    db_path: str | None = None

    @classmethod
    def build(
        cls,
        *,
        db_path: str | None = None,
        rng: random.Random | None = None,
        latency_scale: float = 1.0,
        with_prava: bool | None = None,
    ) -> Engine:
        shared_rng = rng if rng is not None else config.make_rng()
        providers = build_registry(
            shared_rng,
            latency_scale=latency_scale,
            with_prava=prava_configured() if with_prava is None else with_prava,
        )
        card_arms = tuple(name for name, p in providers.items() if not p.real)
        return cls(
            rng=shared_rng,
            providers=providers,
            router=Router(shared_rng, card_arms),
            health=HealthMonitor(providers=providers, db_path=db_path),
            db_path=db_path,
        )

    def boot(self) -> int:
        """Rebuild posteriors from the attempts table. Without this a restart
        mid-demo would throw away everything the router had learned."""
        store.init_db(self.db_path)
        history = store.recent_resolved_attempts(limit=REBUILD_LIMIT, db_path=self.db_path)
        return self.router.rebuild(history)

    # --- execution -----------------------------------------------------------

    def _mode_for(self, request: PaymentRequest, payment_id: str, source: PaymentSource):
        """Explicit beats assigned; generator traffic splits 50/50 for the A/B
        measurement; anything a merchant or an agent sends gets the real router,
        because deliberately routing a customer's payment through the control
        arm is not an experiment we are entitled to run on them."""
        if request.routing_mode is not None:
            return request.routing_mode
        if source is PaymentSource.GENERATOR:
            return assign_mode(payment_id)
        return RoutingMode.ROUTER

    async def execute(
        self,
        request: PaymentRequest,
        *,
        tenant_id: int,
        source: PaymentSource = PaymentSource.API,
        http: httpx.Client | None = None,
    ) -> PaymentResponse:
        payment_id = new_payment_id()
        mode = self._mode_for(request, payment_id, source)
        segment = request.segment
        store.insert_payment(
            payment_id=payment_id,
            tenant_id=tenant_id,
            amount_cents=request.amount_cents,
            currency=request.currency,
            country=request.country,
            card_brand=request.card_brand.value if request.card_brand else "",
            method=request.method,
            routing_mode=mode,
            segment=segment,
            status=PaymentStatus.PENDING,
            source=source,
            description=request.description,
            db_path=self.db_path,
        )

        if request.method is PaymentMethod.PRAVA:
            await self._start_prava(payment_id, request, segment, http=http)
        else:
            await self._run_cascade(payment_id, request, segment, mode)
        return self.load(payment_id)

    async def _run_cascade(
        self, payment_id: str, request: PaymentRequest, segment: str, mode: RoutingMode
    ) -> None:
        charge = ChargeRequest(
            payment_id=payment_id,
            amount_cents=request.amount_cents,
            currency=request.currency,
            country=request.country,
            card_brand=request.card_brand.value if request.card_brand else "",
            segment=segment,
            description=request.description,
        )
        tried: list[str] = []
        decline_code = ""
        while True:
            provider = self.router.choose(
                segment, mode=mode, exclude=tried, unavailable=self.health.unavailable()
            )
            attempt_id = store.insert_attempt(
                payment_id=payment_id,
                seq=len(tried) + 1,
                provider=provider,
                segment=segment,
                status=AttemptStatus.PENDING,
                db_path=self.db_path,
            )
            outcome = await self.providers[provider].charge(charge)
            store.resolve_attempt(
                attempt_id,
                status=outcome.status,
                decline_code=outcome.decline_code,
                latency_ms=outcome.latency_ms,
                db_path=self.db_path,
            )
            # Both A/B arms feed the posteriors: the baseline's attempts are
            # real observations of real rails, and its routing is what differs.
            self.router.update(provider, segment, outcome.approved)
            tried.append(provider)
            decline_code = outcome.decline_code

            if outcome.approved:
                store.finalize_payment(
                    payment_id,
                    status=PaymentStatus.SUCCEEDED,
                    final_provider=provider,
                    db_path=self.db_path,
                )
                # Metered only on an authorization. A payment we failed to get
                # through is not a service we performed.
                billing.record_fee(payment_id, db_path=self.db_path)
                return
            if not Router.should_retry(cascades=outcome.cascades, attempts_made=len(tried)):
                break

        store.finalize_payment(
            payment_id,
            status=PaymentStatus.FAILED,
            final_provider=tried[-1],
            decline_code=decline_code,
            db_path=self.db_path,
        )

    async def _start_prava(
        self,
        payment_id: str,
        request: PaymentRequest,
        segment: str,
        *,
        http: httpx.Client | None = None,
    ) -> None:
        """Mint a Prava session and hand back an approval URL.

        Prava never enters the cascade and never touches the bandit: it needs a
        human to tap a passkey, and the sandbox transaction budget is finite, so
        it is a rail a caller asks for by name rather than one an algorithm may
        decide to spend. The lifespan poller settles it later.
        """
        adapter = self.providers.get(PRAVA)
        if adapter is None:
            raise RailUnavailable(
                "the prava rail is not configured on this deployment —"
                " set PRAVA_SECRET_KEY and the user identity (see .env.example)"
            )
        charge = ChargeRequest(
            payment_id=payment_id,
            amount_cents=request.amount_cents,
            currency=request.currency,
            country=request.country,
            segment=segment,
            description=request.description,
        )
        attempt_id = store.insert_attempt(
            payment_id=payment_id,
            seq=1,
            provider=PRAVA,
            segment=segment,
            status=AttemptStatus.PENDING,
            db_path=self.db_path,
        )
        outcome = await adapter.charge(charge, http=http)
        store.attach_prava_session(
            attempt_id,
            session_id=outcome.prava_session_id,
            iframe_url=outcome.iframe_url,
            db_path=self.db_path,
        )
        store.set_payment_status(payment_id, PaymentStatus.PENDING_APPROVAL, db_path=self.db_path)

    # --- reading back --------------------------------------------------------

    def load(self, payment_id: str) -> PaymentResponse:
        row = store.get_payment(payment_id, db_path=self.db_path)
        if row is None:
            raise LookupError(payment_id)
        attempts = store.attempts_for_payment(payment_id, db_path=self.db_path)
        return to_response(row, attempts)


def to_response(row: dict, attempts: list[dict]) -> PaymentResponse:
    approval_url = next((a["iframe_url"] for a in attempts if a["iframe_url"]), "")
    return PaymentResponse(
        id=row["id"],
        status=PaymentStatus(row["status"]),
        amount_cents=row["amount_cents"],
        currency=row["currency"],
        country=row["country"],
        method=PaymentMethod(row["method"]),
        routing_mode=RoutingMode(row["routing_mode"]),
        segment=row["segment"],
        provider=row["final_provider"],
        decline_code=row["decline_code"],
        description=row["description"],
        source=PaymentSource(row["source"]),
        created_ts=row["created_ts"],
        resolved_ts=row["resolved_ts"],
        iframe_url=approval_url,
        attempts=[
            AttemptView(
                seq=a["seq"],
                provider=a["provider"],
                status=AttemptStatus(a["status"]),
                decline_code=a["decline_code"],
                latency_ms=a["latency_ms"],
                iframe_url=a["iframe_url"],
            )
            for a in attempts
        ],
    )
