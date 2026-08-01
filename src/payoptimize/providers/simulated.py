"""Three simulated card rails: stripe_sim, braintree_sim, adyen_sim.

These are not pretending to be Stripe, Braintree, or Adyen — they are named
after them because the corridor story is the familiar one (an acquirer that is
strong in its home market and weaker abroad), and every surface in the product
labels them SIMULATED. The one real rail is Prava.

What makes them useful is that no provider wins everywhere: the router has to
learn a different answer per corridor, which is exactly what a single global
auth-rate average would hide.

Determinism: one injected random.Random drives everything, and a decision
consumes a fixed number of draws — auth, then a decline code only if the auth
failed, then latency. Seed it and a rehearsal replays exactly.
"""

from __future__ import annotations

import asyncio
import enum
import math
import random
from dataclasses import dataclass, field

import httpx

from ..models import AttemptStatus, DeclineCode
from .base import AttemptOutcome, ChargeRequest, weighted_choice


class InjectionMode(enum.StrEnum):
    """What an operator can do to a rail from /admin/outage."""

    OUTAGE = "outage"
    DEGRADED = "degraded"
    CLEAR = "clear"


class SimState(enum.StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DOWN = "down"


@dataclass(frozen=True, slots=True)
class SimSpec:
    name: str
    display_name: str
    median_latency_ms: int
    fee_bps: int
    fee_fixed_cents: int
    baseline_auth: float  # corridors outside CORRIDOR_AUTH


SIM_SPECS: tuple[SimSpec, ...] = (
    SimSpec("stripe_sim", "Stripe (sim)", 140, 290, 30, 0.90),
    SimSpec("braintree_sim", "Braintree (sim)", 220, 275, 30, 0.87),
    SimSpec("adyen_sim", "Adyen (sim)", 190, 260, 22, 0.88),
)

# The corridor deltas ARE the story: stripe owns US visa, adyen owns the euro
# corridors, braintree owns US amex. A global average would route everything to
# one provider and lose several points of auth rate per corridor.
CORRIDOR_AUTH: dict[str, dict[str, float]] = {
    "US:USD:visa": {"stripe_sim": 0.94, "braintree_sim": 0.88, "adyen_sim": 0.85},
    "DE:EUR:visa": {"adyen_sim": 0.95, "stripe_sim": 0.87, "braintree_sim": 0.83},
    "US:USD:amex": {"braintree_sim": 0.93, "stripe_sim": 0.90, "adyen_sim": 0.80},
    "GB:GBP:mastercard": {"stripe_sim": 0.92, "adyen_sim": 0.90, "braintree_sim": 0.86},
    "FR:EUR:mastercard": {"adyen_sim": 0.93, "braintree_sim": 0.88, "stripe_sim": 0.85},
}

# Healthy traffic: mostly the issuer saying no about the card or the customer,
# with ~15% infrastructure noise so the cascade has something to do.
HEALTHY_DECLINES: tuple[tuple[str, float], ...] = (
    (DeclineCode.INSUFFICIENT_FUNDS, 0.35),
    (DeclineCode.DO_NOT_HONOR, 0.25),
    (DeclineCode.EXPIRED_CARD, 0.10),
    (DeclineCode.INVALID_CARD, 0.08),
    (DeclineCode.FRAUD_SUSPECTED, 0.05),
    (DeclineCode.STOLEN_CARD, 0.02),
    (DeclineCode.ISSUER_UNAVAILABLE, 0.08),
    (DeclineCode.PROVIDER_TIMEOUT, 0.04),
    (DeclineCode.PROCESSING_ERROR, 0.02),
    (DeclineCode.RATE_LIMITED, 0.01),
)

# A hard outage is honest about being infrastructure, which means the BASELINE
# can cascade its way out of it too — that is why the outage is the demo's
# secondary beat and degradation is the headline (PLAN §1.5).
OUTAGE_DECLINES: tuple[tuple[str, float], ...] = (
    (DeclineCode.ISSUER_UNAVAILABLE, 0.55),
    (DeclineCode.PROVIDER_TIMEOUT, 0.30),
    (DeclineCode.PROCESSING_ERROR, 0.10),
    (DeclineCode.RATE_LIMITED, 0.05),
)

# Degradation is the lever: the rail still answers fast and looks alive, it just
# says no. do_not_honor does not cascade, so the baseline eats every one of
# these while the router learns to route around them.
DEGRADED_DECLINES: tuple[tuple[str, float], ...] = (
    (DeclineCode.DO_NOT_HONOR, 0.85),
    (DeclineCode.INSUFFICIENT_FUNDS, 0.10),
    (DeclineCode.ISSUER_UNAVAILABLE, 0.05),
)

DEFAULT_DEGRADED_AUTH = 0.45
LATENCY_SIGMA = 0.45  # lognormal spread; ~2.1x between p50 and p95
OUTAGE_LATENCY_MULTIPLIER = 4.0  # a dying rail times out before it answers
MIN_LATENCY_MS = 15
MAX_LATENCY_MS = 8_000


@dataclass
class SimulatedProvider:
    """One simulated rail. `draw` is the pure, testable core; `charge` is the
    async wrapper that also spends the latency it drew."""

    spec: SimSpec
    rng: random.Random
    latency_scale: float = 1.0  # 0 in tests: report the latency, never sleep it
    state: SimState = SimState.HEALTHY
    injected_auth_rate: float | None = None
    real: bool = field(default=False, init=False)

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def display_name(self) -> str:
        return self.spec.display_name

    @property
    def fee_bps(self) -> int:
        return self.spec.fee_bps

    @property
    def fee_fixed_cents(self) -> int:
        return self.spec.fee_fixed_cents

    # --- injection (POST /admin/outage) --------------------------------------

    def inject(self, mode: str, auth_rate: float | None = None) -> None:
        """Flip this rail's behaviour. Unknown modes raise rather than no-op —
        a demo lever that silently does nothing is worse than one that errors."""
        match mode:
            case InjectionMode.OUTAGE:
                self.state = SimState.DOWN
                self.injected_auth_rate = 0.0
            case InjectionMode.DEGRADED:
                rate = DEFAULT_DEGRADED_AUTH if auth_rate is None else auth_rate
                if not 0.0 <= rate <= 1.0:
                    raise ValueError(f"auth_rate must be between 0 and 1, got {rate}")
                self.state = SimState.DEGRADED
                self.injected_auth_rate = rate
            case InjectionMode.CLEAR:
                self.state = SimState.HEALTHY
                self.injected_auth_rate = None
            case _:
                raise ValueError(
                    f"unknown injection mode {mode!r} — expected one of"
                    f" {', '.join(m.value for m in InjectionMode)}"
                )

    # --- the pure core -------------------------------------------------------

    def auth_rate_for(self, segment: str) -> float:
        """This rail's true probability of authorizing in this corridor."""
        if self.injected_auth_rate is not None:
            return self.injected_auth_rate
        return CORRIDOR_AUTH.get(segment, {}).get(self.name, self.spec.baseline_auth)

    def _decline_mix(self) -> tuple[tuple[str, float], ...]:
        match self.state:
            case SimState.DOWN:
                return OUTAGE_DECLINES
            case SimState.DEGRADED:
                return DEGRADED_DECLINES
            case _:
                return HEALTHY_DECLINES

    def _latency_ms(self) -> int:
        median = self.spec.median_latency_ms
        if self.state is SimState.DOWN:
            median = int(median * OUTAGE_LATENCY_MULTIPLIER)
        drawn = self.rng.lognormvariate(math.log(median), LATENCY_SIGMA)
        return max(MIN_LATENCY_MS, min(int(drawn), MAX_LATENCY_MS))

    def draw(self, request: ChargeRequest) -> AttemptOutcome:
        """One authorization decision, no clock and no I/O. Everything the
        statistical tests care about happens here."""
        approved = self.rng.random() < self.auth_rate_for(request.segment)
        decline_code = "" if approved else weighted_choice(self.rng, self._decline_mix())
        return AttemptOutcome(
            provider=self.name,
            status=AttemptStatus.SUCCEEDED if approved else AttemptStatus.FAILED,
            decline_code=decline_code,
            latency_ms=self._latency_ms(),
        )

    # --- ProviderAdapter -----------------------------------------------------

    async def charge(
        self, request: ChargeRequest, *, http: httpx.Client | None = None
    ) -> AttemptOutcome:
        """`http` is unused here — a simulated rail makes no calls — but the
        signature is the adapter contract the real Prava rail also implements."""
        outcome = self.draw(request)
        if self.latency_scale:
            await asyncio.sleep(outcome.latency_ms / 1000 * self.latency_scale)
        return outcome


def build_simulated(
    rng: random.Random, *, latency_scale: float = 1.0
) -> dict[str, SimulatedProvider]:
    """All three sims sharing one RNG, so one seed replays the whole demo."""
    return {spec.name: SimulatedProvider(spec, rng, latency_scale) for spec in SIM_SPECS}
