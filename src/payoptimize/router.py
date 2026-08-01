"""The routing brain: which rail to try, and whether a failure earns another.

Pure decision logic — no HTTP, no SQL, no clock. It is handed a segment and a
set of exclusions and it returns a provider name; api.py does the charging and
the writing. That separation is what makes 2,000-payment convergence tests run
in milliseconds.

Why discounted Thompson sampling rather than plain Beta posteriors: after a few
thousand successes, Beta(1800, 200) needs hundreds of consecutive failures to
move. A rail that degrades mid-demo would keep receiving traffic long past the
point a human could see the problem. Discounting holds a finite memory instead,
so a degradation shows up in seconds rather than hours.

Every arm in a segment is discounted on each outcome, and only the played arm
takes the reward — the standard formulation for a non-stationary bandit. An
earlier draft of this module discounted the played arm alone, which measured
better on convergence and then failed the thing that actually matters: once an
arm is starved its counts FREEZE at whatever beat-down state they were in.
Nothing pulls them back. Recovery then depends entirely on the 2% probe, which
reaches a given arm on 0.67% of decisions, and each probe success adds ~1 to an
alpha sitting against a beta of ~27. Measured over five seeds, a recovered rail
was rediscovered in 1 of 5 runs within 4,000 decisions; raising the probe to
10% fixed one more seed and cost a point of steady-state auth rate.

Discounting every arm makes a starved arm drift back to Beta(1,1) — uncertainty
rather than a verdict — and Thompson sampling reaches for it on its own. The
floor at the prior matters: an arm forgets its evidence, never the fact that
"no opinion" is where it started.

γ=0.99 is where the trade-off sits. Measured over 5 seeds × 4,000 payments on
the 0.95/0.85/0.80 corridor: 80% share on the best arm, +5.3 pts uplift over
the round-robin control, +14 pts while a rail is degraded, a degraded arm
pushed under 10% share within ~130 decisions (~50 s at the demo's 2.5 tx/s),
and recovery after `clear` in ~280 decisions (~110 s). γ=0.995 converges harder
but takes 3.4 minutes to forgive, which is longer than the entire demo.

Posteriors live in memory and are rebuilt at boot from the attempts table. That
is the reason the service runs as a single process with a single worker.
"""

from __future__ import annotations

import hashlib
import random
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from .models import AttemptStatus, RoutingMode

GAMMA = 0.99  # discount applied to every arm in a segment, each outcome
EXPLORE_P = 0.02  # forced-exploration probe: a floor on how starved an arm can get
PRIOR_ALPHA = 1.0  # Beta(1,1): uniform. A new corridor has no opinion.
PRIOR_BETA = 1.0
MAX_ATTEMPTS = 3  # cascade budget per payment
BASELINE_SPLIT_PCT = 50  # share of generator traffic sent to the control arm
REBUILD_LIMIT = 2_000  # attempts replayed at boot


class NoProviderAvailable(RuntimeError):
    """The cascade has run out of rails it has not already tried."""


@dataclass
class Posterior:
    """One arm: (provider, segment). Counts are floats because they decay."""

    alpha: float = PRIOR_ALPHA
    beta: float = PRIOR_BETA

    @property
    def mean(self) -> float:
        return self.alpha / (self.alpha + self.beta)

    @property
    def observations(self) -> float:
        """Decayed count. Caps out near 1/(1-γ) — that is the memory horizon."""
        return self.alpha + self.beta - PRIOR_ALPHA - PRIOR_BETA


def assign_mode(payment_id: str, *, split_pct: int = BASELINE_SPLIT_PCT) -> RoutingMode:
    """Deterministic 50/50 A/B split on the payment id.

    Hashing the id rather than flipping a coin means the assignment survives a
    restart and can be recomputed from the database months later — the uplift
    number is auditable, not just asserted.
    """
    digest = hashlib.sha256(payment_id.encode("utf-8")).hexdigest()
    return RoutingMode.BASELINE if int(digest, 16) % 100 < split_pct else RoutingMode.ROUTER


class Router:
    def __init__(
        self,
        rng: random.Random,
        providers: Sequence[str],
        *,
        gamma: float = GAMMA,
        explore_p: float = EXPLORE_P,
    ) -> None:
        if not providers:
            raise ValueError("a router needs at least one provider to route to")
        self.rng = rng
        self.providers = tuple(providers)
        self.gamma = gamma
        self.explore_p = explore_p
        self._posteriors: dict[tuple[str, str], Posterior] = {}
        self._baseline_cursor = 0
        self.explore_count = 0  # observability: how often the probe fired

    # --- posteriors ----------------------------------------------------------

    def posterior(self, provider: str, segment: str) -> Posterior:
        """The arm, created at the uniform prior on first sight."""
        return self._posteriors.setdefault((provider, segment), Posterior())

    def update(self, provider: str, segment: str, approved: bool) -> None:
        """Discount every arm in this segment; give the reward to the played one.

        Discounting the whole segment rather than just the played arm is what
        lets a starved rail be reconsidered — see the module docstring for the
        measurements behind that choice. The floor at the prior is load-bearing:
        an arm decays back to Beta(1,1), which is uncertainty, not a verdict.

        Baseline traffic is fed through here too. Those attempts are real
        observations of a real rail, so ignoring them would throw away half the
        data for nothing. It is the *decisions* that differ between the two arms
        of the A/B test, not the learning.
        """
        if provider not in self.providers:
            return  # prava and anything else outside the bandit's arm pool
        for name in self.providers:
            arm = self.posterior(name, segment)
            arm.alpha = max(self.gamma * arm.alpha, PRIOR_ALPHA)
            arm.beta = max(self.gamma * arm.beta, PRIOR_BETA)
        played = self.posterior(provider, segment)
        if approved:
            played.alpha += 1.0
        else:
            played.beta += 1.0

    def rebuild(self, attempts: Iterable[Mapping[str, object]]) -> int:
        """Replay resolved attempts in chronological order after a restart.

        Same decay math as live updates, so a restarted process lands on the
        posteriors it would have had if it had never stopped.
        """
        replayed = 0
        for row in attempts:
            status = str(row["status"])
            if status not in (AttemptStatus.SUCCEEDED, AttemptStatus.FAILED):
                continue  # still pending: no outcome to learn from
            self.update(
                str(row["provider"]), str(row["segment"]), status == AttemptStatus.SUCCEEDED
            )
            replayed += 1
        return replayed

    def snapshot(self) -> list[dict[str, object]]:
        """Posterior means per arm, for GET /admin/state — this is what makes
        the learning legible on stage while an injection is in flight."""
        return sorted(
            (
                {
                    "provider": provider,
                    "segment": segment,
                    "alpha": round(arm.alpha, 4),
                    "beta": round(arm.beta, 4),
                    "mean": round(arm.mean, 4),
                    "observations": round(arm.observations, 2),
                }
                for (provider, segment), arm in self._posteriors.items()
            ),
            key=lambda row: (str(row["segment"]), str(row["provider"])),
        )

    # --- decisions -----------------------------------------------------------

    def _eligible(self, exclude: Iterable[str], unavailable: Iterable[str]) -> tuple[str, ...]:
        """Provider order is fixed, so a seeded run is reproducible."""
        tried = set(exclude)
        remaining = tuple(name for name in self.providers if name not in tried)
        if not remaining:
            raise NoProviderAvailable(
                f"every provider has been attempted already ({', '.join(sorted(tried))})"
            )
        down = set(unavailable)
        healthy = tuple(name for name in remaining if name not in down)
        # If health says every remaining rail is sick, try a sick one anyway.
        # Declining a payment because our own monitoring is pessimistic would be
        # a worse outcome than an attempt that might still authorize.
        return healthy or remaining

    def choose(
        self,
        segment: str,
        *,
        mode: RoutingMode = RoutingMode.ROUTER,
        exclude: Iterable[str] = (),
        unavailable: Iterable[str] = (),
    ) -> str:
        eligible = self._eligible(exclude, unavailable)
        if mode is RoutingMode.BASELINE:
            return self._round_robin(eligible)
        return self._thompson(segment, eligible)

    def _round_robin(self, eligible: tuple[str, ...]) -> str:
        """The control arm: no learning, no health, same cascade rules. The only
        difference between the two arms is routing intelligence, which is what
        makes the uplift number mean something."""
        choice = eligible[self._baseline_cursor % len(eligible)]
        self._baseline_cursor += 1
        return choice

    def _thompson(self, segment: str, eligible: tuple[str, ...]) -> str:
        if self.rng.random() < self.explore_p:
            self.explore_count += 1
            return self.rng.choice(eligible)
        best = eligible[0]
        best_theta = -1.0
        for name in eligible:
            arm = self.posterior(name, segment)
            theta = self.rng.betavariate(arm.alpha, arm.beta)
            if theta > best_theta:
                best, best_theta = name, theta
        return best

    # --- cascade -------------------------------------------------------------

    @staticmethod
    def should_retry(*, cascades: bool, attempts_made: int) -> bool:
        """Both arms obey this identically — the baseline is a routing control,
        not a retry control."""
        return cascades and attempts_made < MAX_ATTEMPTS
