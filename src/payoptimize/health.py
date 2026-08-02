"""Per-provider health, derived from what actually happened.

There is no health_snapshots table and no prober pinging the rails. Health is
computed from the attempts we already made, over a rolling window — a rail's
health *is* how it has been answering us, and a synthetic ping would only tell
us about a code path no customer's money travels down.

Two thresholds, doing two different jobs:

  down     — the router refuses to send first attempts here at all. This is the
             circuit breaker, and it only trips on something close to a total
             failure, because the cost of being wrong is declining payments a
             working rail would have authorized.
  degraded — displayed, annotated, and otherwise left alone. The bandit already
             routes around a rail returning 45%, faster and with better judgment
             than a threshold could, because it knows what the alternatives are
             worth in this corridor. A circuit breaker here would fight it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import store
from .models import iso_since
from .providers import ProviderAdapter

WINDOW_SECONDS = 300  # what the tile DISPLAYS: a stable 5-minute auth rate
# What the tile is CLASSIFIED on. These are deliberately different windows.
# Measured during a live injection: a rail degraded to 45% kept reading
# `healthy` for the whole demo, because ninety seconds of bad traffic barely
# moves a five-minute average. The state is the part an operator reacts to, so
# it watches a window short enough to react within — while the displayed rate
# stays the stable one, because a number that jumps every poll is unreadable.
STATE_WINDOW_SECONDS = 90
MIN_ATTEMPTS = 8  # below this there is no opinion to have

# The real rail keeps different company. Its traffic is paced by a human tapping
# a passkey, so it will never satisfy thresholds written for 2 tx/s of synthetic
# traffic: it would sit at `unknown` forever and its tile would claim "no traffic
# yet" five minutes after a real payment settled. Rare traffic needs a long
# window and a low bar.
REAL_WINDOW_SECONDS = 86_400
REAL_MIN_ATTEMPTS = 2
DOWN_BELOW = 0.15
DEGRADED_BELOW = 0.65  # a 0.45 injection lands here; a healthy 0.80 corridor does not


class HealthState:
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DOWN = "down"
    UNKNOWN = "unknown"  # not enough traffic to say


def classify(auth_rate: float, attempts: int, *, min_attempts: int = MIN_ATTEMPTS) -> str:
    if attempts < min_attempts:
        return HealthState.UNKNOWN
    if auth_rate < DOWN_BELOW:
        return HealthState.DOWN
    if auth_rate < DEGRADED_BELOW:
        return HealthState.DEGRADED
    return HealthState.HEALTHY


def percentile(values: list[int], fraction: float) -> int:
    """Nearest-rank percentile on a pre-sorted list. p95 rather than a mean,
    because a rail that is starting to fail shows it in the tail first."""
    if not values:
        return 0
    index = min(len(values) - 1, max(0, round(fraction * len(values)) - 1))
    return values[index]


def _fee_text(adapter: ProviderAdapter) -> str:
    """A fee line that says something. The real rail carries 0bps + 0c because
    Prava's sandbox does not charge us — rendering that as "0.00% + 0¢" reads as
    either a bug or a claim that the rail is free, and it is neither."""
    if adapter.real and not adapter.fee_bps and not adapter.fee_fixed_cents:
        return "sandbox — no processing fee"
    return f"{adapter.fee_bps / 100:.2f}% + {adapter.fee_fixed_cents}¢"


@dataclass
class HealthMonitor:
    """Computes health and remembers it, so a transition is logged exactly once."""

    providers: dict[str, ProviderAdapter]
    db_path: str | None = None
    window_seconds: int = WINDOW_SECONDS
    state_window_seconds: int = STATE_WINDOW_SECONDS
    real_window_seconds: int = REAL_WINDOW_SECONDS
    _last: dict[str, str] = field(default_factory=dict)

    def _windowed(self, seconds: int) -> dict[str, Any]:
        return {
            row["provider"]: row
            for row in store.attempts_by_provider(since=iso_since(seconds), db_path=self.db_path)
        }

    def snapshot(self, *, log_transitions: bool = True) -> list[dict[str, Any]]:
        rows = self._windowed(self.window_seconds)
        recent = self._windowed(self.state_window_seconds)
        # The real rail gets its own, much longer windows — see the constants.
        real_rows = self._windowed(self.real_window_seconds)
        lifetime = store.provider_attempt_summary(db_path=self.db_path)

        tiles = []
        for name, adapter in self.providers.items():
            if adapter.real:
                row = real_rows.get(name)
                fresh = real_rows.get(name)
                min_attempts = REAL_MIN_ATTEMPTS
                window = self.real_window_seconds
            else:
                row = rows.get(name)
                fresh = recent.get(name)
                min_attempts = MIN_ATTEMPTS
                window = self.window_seconds
            attempts = int(row["attempts"]) if row else 0
            succeeded = int(row["succeeded"] or 0) if row else 0
            auth_rate = succeeded / attempts if attempts else 0.0

            # Classified on the short window, displayed on the long one. For the
            # real rail both are the same long window: there is no such thing as
            # a ninety-second sample of human-paced traffic.
            fresh_n = int(fresh["attempts"]) if fresh else 0
            fresh_ok = int(fresh["succeeded"] or 0) if fresh else 0
            state = classify(
                fresh_ok / fresh_n if fresh_n else 0.0, fresh_n, min_attempts=min_attempts
            )
            latencies = (
                store.attempt_latencies(
                    provider=name, since=iso_since(window), db_path=self.db_path
                )
                if attempts
                else []
            )
            ever = lifetime.get(name, {"attempts": 0, "last_ts": ""})
            if log_transitions:
                self._note(name, state)
            tiles.append(
                {
                    "name": name,
                    "display_name": adapter.display_name,
                    "real": adapter.real,
                    "label": "REAL · sandbox" if adapter.real else "SIMULATED",
                    "state": state,
                    "attempts": attempts,
                    "recent_attempts": fresh_n,
                    "lifetime_attempts": int(ever["attempts"]),
                    "last_attempt_ts": str(ever["last_ts"]),
                    "auth_rate": round(auth_rate, 4),
                    "p95_ms": percentile(latencies, 0.95),
                    "fee": _fee_text(adapter),
                }
            )
        return sorted(tiles, key=lambda t: (not t["real"], t["name"]))

    def _note(self, provider: str, state: str) -> None:
        previous = self._last.get(provider)
        self._last[provider] = state
        if previous is None or previous == state or state == HealthState.UNKNOWN:
            return
        if previous == HealthState.UNKNOWN:
            # The first real reading after startup or a quiet spell. A rail going
            # from "no opinion" to "healthy" has not recovered from anything, and
            # narrating it buries the transition that matters under warm-up noise.
            return
        if state == HealthState.HEALTHY:
            kind = "health_recovered"
        else:
            kind = "health_degraded"
        store.insert_provider_event(
            provider, kind, detail=f"{previous} → {state}", db_path=self.db_path
        )

    def unavailable(self) -> set[str]:
        """Rails the router should not send a first attempt to. Only `down`
        counts — see the module docstring for why `degraded` does not."""
        return {name for name, state in self._last.items() if state == HealthState.DOWN}
