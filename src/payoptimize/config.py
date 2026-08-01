"""Service settings: the only module that reads os.environ, and the only place
a missing value turns into a sentence instead of a plausible default.

The .env reader is ported from canibuy's prava.py — deliberately tiny: no
dotenv dependency, no interpolation, and `setdefault` so a real environment
variable always beats the file. Anything genuinely required (the admin token)
raises here rather than defaulting; anything with a safe local default gets one.
"""

from __future__ import annotations

import os
import random
from pathlib import Path

DEFAULT_DB = "payoptimize.sqlite3"
DEFAULT_PORT = 8080
DEFAULT_GENERATOR_TPS = 2.0


class ConfigError(RuntimeError):
    """A required setting is missing or unusable."""


def _load_env() -> None:
    """Read .env into the environment for keys not already set."""
    env = Path(__file__).resolve().parents[2] / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def _str(name: str, default: str = "") -> str:
    _load_env()
    return os.environ.get(name, default).strip()


def _int(name: str, default: int) -> int:
    raw = _str(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name}={raw!r} is not an integer") from exc


def _float(name: str, default: float) -> float:
    raw = _str(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name}={raw!r} is not a number") from exc


def db_path() -> str:
    """Path to the SQLite file. One file, one process — see CLAUDE.md."""
    return _str("PAYOPTIMIZE_DB") or DEFAULT_DB


def admin_token() -> str:
    """The bearer token guarding /admin/*. No default, ever."""
    token = _str("PAYOPTIMIZE_ADMIN_TOKEN")
    if not token:
        raise ConfigError(
            "PAYOPTIMIZE_ADMIN_TOKEN is unset — /admin/* injects outages and drives the"
            " generator, so it cannot be left open. Set it to a long random string"
            " (see .env.example)."
        )
    return token


def port() -> int:
    return _int("PAYOPTIMIZE_PORT", DEFAULT_PORT)


def generator_tps() -> float:
    return _float("PAYOPTIMIZE_GENERATOR_TPS", DEFAULT_GENERATOR_TPS)


def seed() -> int | None:
    """PAYOPTIMIZE_SEED pins every RNG for a rehearsable demo; unset means entropy."""
    raw = _str("PAYOPTIMIZE_SEED")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"PAYOPTIMIZE_SEED={raw!r} is not an integer") from exc


def make_rng() -> random.Random:
    """The one RNG the sims and the router share. random.Random(None) seeds from
    OS entropy, so an unset PAYOPTIMIZE_SEED is a live demo and a set one is a
    rehearsal that replays exactly."""
    return random.Random(seed())
