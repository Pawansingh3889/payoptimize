"""Config reads the environment once and refuses to invent values."""

from __future__ import annotations

import pytest

from payoptimize import config


def test_admin_token_has_no_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAYOPTIMIZE_ADMIN_TOKEN", "")
    with pytest.raises(config.ConfigError, match="PAYOPTIMIZE_ADMIN_TOKEN"):
        config.admin_token()


def test_admin_token_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAYOPTIMIZE_ADMIN_TOKEN", "  long-random  ")
    assert config.admin_token() == "long-random"


def test_numeric_settings_fall_back_then_fail_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PAYOPTIMIZE_PORT", raising=False)
    assert config.port() == config.DEFAULT_PORT

    monkeypatch.setenv("PAYOPTIMIZE_PORT", "9999")
    assert config.port() == 9999

    monkeypatch.setenv("PAYOPTIMIZE_PORT", "eighty-eighty")
    with pytest.raises(config.ConfigError, match="PAYOPTIMIZE_PORT"):
        config.port()


def test_generator_tps_accepts_fractions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAYOPTIMIZE_GENERATOR_TPS", "2.5")
    assert config.generator_tps() == 2.5


def test_seed_pins_the_rng(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAYOPTIMIZE_SEED", "42")
    assert config.seed() == 42
    assert [config.make_rng().random() for _ in range(3)] == [
        config.make_rng().random() for _ in range(3)
    ]

    monkeypatch.setenv("PAYOPTIMIZE_SEED", "")
    assert config.seed() is None

    monkeypatch.setenv("PAYOPTIMIZE_SEED", "nope")
    with pytest.raises(config.ConfigError, match="PAYOPTIMIZE_SEED"):
        config.seed()


def test_db_path_comes_from_the_environment(db: str) -> None:
    assert config.db_path() == db
