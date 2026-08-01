"""Synthetic merchant traffic, so the dashboard has a pulse.

The demo needs enough volume for an auth-rate line to mean something and for
the A/B split to be measurable within a five-minute window. This produces it.

`tick()` is one payment and is pure enough to assert on; `run()` is the loop
around it and is deliberately trivial, because a loop with logic in it is a
loop that can only be tested by waiting for wall-clock time to pass.

Every payment this creates is labeled `source='generator'` in the database, so
nothing here can ever be mistaken for a merchant's real traffic — including by
the analytics that compute the uplift.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
from dataclasses import dataclass, field

from .engine import Engine
from .models import PaymentRequest, PaymentResponse, PaymentSource

# The five corridors the sims have profiles for. Weighted so US:USD:visa
# dominates — a real processor's traffic is not evenly spread, and the corridor
# with the most volume should be the one the router learns fastest.
CORRIDOR_MIX: tuple[tuple[tuple[str, str, str], float], ...] = (
    (("US", "USD", "visa"), 0.40),
    (("DE", "EUR", "visa"), 0.20),
    (("US", "USD", "amex"), 0.15),
    (("GB", "GBP", "mastercard"), 0.15),
    (("FR", "EUR", "mastercard"), 0.10),
)

# Realistic-ish basket: mostly small, occasionally not.
AMOUNT_RANGE_CENTS = (499, 24_999)

DEFAULT_TPS = 2.0
MAX_TPS = 20.0


@dataclass
class Generator:
    engine: Engine
    tenant_id: int
    rng: random.Random
    tps: float = DEFAULT_TPS
    enabled: bool = True
    created: int = field(default=0, init=False)

    def _corridor(self) -> tuple[str, str, str]:
        target = self.rng.random()
        cumulative = 0.0
        for corridor, weight in CORRIDOR_MIX:
            cumulative += weight
            if target < cumulative:
                return corridor
        return CORRIDOR_MIX[-1][0]

    def next_request(self) -> PaymentRequest:
        """The decision half of a tick, with no I/O — this is what tests drive."""
        country, currency, brand = self._corridor()
        return PaymentRequest(
            amount_cents=self.rng.randint(*AMOUNT_RANGE_CENTS),
            currency=currency,
            country=country,
            card_brand=brand,
            description="synthetic traffic",
        )

    async def tick(self) -> PaymentResponse:
        """One synthetic payment through the same engine a merchant would hit.

        Routing mode is left unset on purpose: the engine assigns generator
        traffic by hashing the payment id, which is what makes the 50/50 split
        reproducible from the database rather than from this process's RNG.
        """
        response = await self.engine.execute(
            self.next_request(), tenant_id=self.tenant_id, source=PaymentSource.GENERATOR
        )
        self.created += 1
        return response

    def set_rate(self, tps: float) -> None:
        if not 0 < tps <= MAX_TPS:
            raise ValueError(f"tps must be between 0 and {MAX_TPS}, got {tps}")
        self.tps = tps

    async def run(self) -> None:
        """Fire at roughly `tps`, forever. Disabled means idle, not stopped, so
        the rate and the switch can both be changed from /admin mid-demo."""
        while True:
            if not self.enabled:
                await asyncio.sleep(0.25)
                continue
            started = asyncio.get_running_loop().time()
            with contextlib.suppress(Exception):
                # A generator that dies on one bad payment takes the demo's
                # pulse with it. Real failures still land in the attempts table.
                await self.tick()
            elapsed = asyncio.get_running_loop().time() - started
            await asyncio.sleep(max(0.0, 1.0 / self.tps - elapsed))
