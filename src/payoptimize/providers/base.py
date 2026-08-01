"""The contract every rail implements, and the decline taxonomy that decides
whether a failure is worth retrying somewhere else.

The router never learns which adapter is real: it picks a name, the API layer
charges it, and an AttemptOutcome comes back. That is the whole reason the
Prava rail and three simulated rails can share one code path.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import httpx

from ..models import AttemptStatus, DeclineCode

# Something broke between us and the issuer; a different provider may well get
# an answer. These are the only codes that earn a retry.
INFRASTRUCTURE_DECLINES = frozenset(
    {
        DeclineCode.ISSUER_UNAVAILABLE,
        DeclineCode.PROVIDER_TIMEOUT,
        DeclineCode.PROCESSING_ERROR,
        DeclineCode.RATE_LIMITED,
    }
)

# The issuer answered "no" without saying why. Card network rules discourage an
# immediate retry, and a second rail would almost certainly hear the same thing
# — so this does not cascade. It is also the demo's degradation lever.
SOFT_DECLINES = frozenset({DeclineCode.DO_NOT_HONOR})

# The issuer answered about the card or the customer. No provider can fix that.
TERMINAL_DECLINES = frozenset(
    {
        DeclineCode.INSUFFICIENT_FUNDS,
        DeclineCode.EXPIRED_CARD,
        DeclineCode.INVALID_CARD,
        DeclineCode.FRAUD_SUSPECTED,
        DeclineCode.STOLEN_CARD,
        DeclineCode.APPROVAL_TIMEOUT,
    }
)

# A new DeclineCode has to be classified before it can be declined on. Leaving
# one out would silently make it non-retriable, which is a routing decision
# nobody wrote down.
assert set(DeclineCode) == INFRASTRUCTURE_DECLINES | SOFT_DECLINES | TERMINAL_DECLINES, (
    "every DeclineCode must be classified as infrastructure, soft, or terminal"
)


def should_cascade(decline_code: str) -> bool:
    """Whether this decline is worth re-attempting on another provider.

    Unknown codes never cascade. Prava returns its own failure strings verbatim
    and a human decline there is final by design, so "we do not recognise this"
    must resolve to "do not spend another attempt on it".
    """
    return decline_code in INFRASTRUCTURE_DECLINES


@dataclass(frozen=True, slots=True)
class ChargeRequest:
    """What a rail needs to attempt one authorization."""

    payment_id: str
    amount_cents: int
    currency: str
    country: str = ""
    card_brand: str = ""
    segment: str = ""
    description: str = ""


@dataclass(frozen=True, slots=True)
class AttemptOutcome:
    """One rail's answer to one ChargeRequest.

    `PENDING` is not a failure: the Prava rail returns it while a human decides,
    and the lifespan poller resolves it later. Card rails never return it.
    """

    provider: str
    status: AttemptStatus
    decline_code: str = ""
    latency_ms: int = 0
    prava_session_id: str = ""
    prava_txn_id: str = ""
    iframe_url: str = ""

    @property
    def approved(self) -> bool:
        return self.status is AttemptStatus.SUCCEEDED

    @property
    def cascades(self) -> bool:
        return self.status is AttemptStatus.FAILED and should_cascade(self.decline_code)


@runtime_checkable
class ProviderAdapter(Protocol):
    """A payment rail. Three of these are simulated; one is really Prava."""

    name: str
    display_name: str
    real: bool  # drives the REAL · sandbox / SIMULATED badge on every surface
    fee_bps: int
    fee_fixed_cents: int

    async def charge(
        self, request: ChargeRequest, *, http: httpx.Client | None = None
    ) -> AttemptOutcome:
        """Attempt one authorization. `http` is the test-injection seam."""
        ...


def weighted_choice(rng: random.Random, weights: tuple[tuple[str, float], ...]) -> str:
    """Pick one label, consuming exactly one random() draw.

    Written out rather than using rng.choices so the number of draws per
    decision is fixed and obvious — that is what keeps a seeded run replayable.
    """
    total = sum(weight for _, weight in weights)
    target = rng.random() * total
    cumulative = 0.0
    for label, weight in weights:
        cumulative += weight
        if target < cumulative:
            return label
    return weights[-1][0]  # float drift only
