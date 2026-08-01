"""The provider registry: name → adapter.

The bandit's arm pool is CARD_PROVIDERS — the simulated rails only. Prava is a
first-class provider selected explicitly by `method: "prava"`; it needs a human
passkey approval and draws on a finite sandbox budget, so it is never something
a routing algorithm can decide to spend (PLAN §1.2). It joins this registry in
Phase 7.
"""

from __future__ import annotations

import random

from .base import (
    INFRASTRUCTURE_DECLINES,
    SOFT_DECLINES,
    TERMINAL_DECLINES,
    AttemptOutcome,
    ChargeRequest,
    ProviderAdapter,
    should_cascade,
    weighted_choice,
)
from .simulated import (
    CORRIDOR_AUTH,
    SIM_SPECS,
    InjectionMode,
    SimSpec,
    SimState,
    SimulatedProvider,
    build_simulated,
)

# The arms the card bandit may play, in a fixed order so round-robin baseline
# assignment is deterministic.
CARD_PROVIDERS: tuple[str, ...] = tuple(spec.name for spec in SIM_SPECS)

PRAVA = "prava"

__all__ = [
    "CARD_PROVIDERS",
    "CORRIDOR_AUTH",
    "INFRASTRUCTURE_DECLINES",
    "PRAVA",
    "SIM_SPECS",
    "SOFT_DECLINES",
    "TERMINAL_DECLINES",
    "AttemptOutcome",
    "ChargeRequest",
    "InjectionMode",
    "ProviderAdapter",
    "SimSpec",
    "SimState",
    "SimulatedProvider",
    "build_registry",
    "build_simulated",
    "should_cascade",
    "weighted_choice",
]


def build_registry(rng: random.Random, *, latency_scale: float = 1.0) -> dict[str, ProviderAdapter]:
    """Every rail the service can charge, sharing one seeded RNG."""
    registry: dict[str, ProviderAdapter] = dict(build_simulated(rng, latency_scale=latency_scale))
    return registry
