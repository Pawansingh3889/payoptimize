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


# --- ops agent ---------------------------------------------------------------

DEFAULT_OPENAI_API_BASE = "https://api.openai.com"
DEFAULT_AGENT_MODEL = "gpt-5"
AUTONOMY_MODES = ("auto", "propose")

# Environment values that must never appear in a request to the model. The
# redactor reads them through here so config stays the one module that knows
# what os.environ holds.
_SECRET_ENV_VARS = (
    "PRAVA_SECRET_KEY",
    "OPENAI_API_KEY",
    "PAYOPTIMIZE_ADMIN_TOKEN",
    "PAYOPTIMIZE_USER_ID",
    "PAYOPTIMIZE_USER_EMAIL",
)


def openai_api_key() -> str:
    """Empty means the agent is unconfigured — a normal deployment state. The
    routes answer 503 with a sentence, the same contract as the Prava rail."""
    return _str("OPENAI_API_KEY")


def openai_api_base() -> str:
    return (_str("OPENAI_API_BASE") or DEFAULT_OPENAI_API_BASE).rstrip("/")


def agent_model() -> str:
    return _str("PAYOPTIMIZE_AGENT_MODEL") or DEFAULT_AGENT_MODEL


def agent_autonomy() -> str:
    """`auto` executes guarded actions immediately; `propose` parks every one
    for a human. An unrecognised value refuses rather than guessing, because
    guessing here decides whether an LLM may mutate production state."""
    raw = _str("PAYOPTIMIZE_AGENT_AUTONOMY") or "auto"
    if raw not in AUTONOMY_MODES:
        raise ConfigError(
            f"PAYOPTIMIZE_AGENT_AUTONOMY={raw!r} must be one of {', '.join(AUTONOMY_MODES)}"
        )
    return raw


def agent_triggers_enabled() -> bool:
    """The kill-switch for event-driven agent runs. Anything other than the
    literal '0' means on — the switch exists to turn the agent OFF in one move,
    not to make enabling it a puzzle."""
    return _str("PAYOPTIMIZE_AGENT_TRIGGERS") != "0"


def agent_capture_enabled() -> bool:
    """Whether runs store their full redacted transcript for the fine-tuning
    corpus (docs/FINETUNE.md). Off by default: the audit tables always record
    what happened; the transcript is training data and opt-in."""
    return _str("PAYOPTIMIZE_AGENT_CAPTURE") == "1"


def secret_values() -> tuple[str, ...]:
    """Every configured secret, for the redaction denylist."""
    return tuple(value for name in _SECRET_ENV_VARS if (value := _str(name)))
