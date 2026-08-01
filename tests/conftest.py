"""Test environment: a throwaway database and a Prava host that cannot resolve.

PRAVA_API_BASE points at an unregistrable TLD on purpose. Every Prava call in
the suite goes through an httpx.MockTransport; anything that slips past a mock
dies at DNS instead of spending one of a finite number of sandbox transactions.
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "payoptimize.sqlite3"
    monkeypatch.setenv("PAYOPTIMIZE_DB", str(db))
    monkeypatch.setenv("PRAVA_SECRET_KEY", "sk_test_unit")
    monkeypatch.setenv("PRAVA_API_BASE", "https://prava.invalid")
    monkeypatch.setenv("PAYOPTIMIZE_USER_ID", "unit-test")
    monkeypatch.setenv("PAYOPTIMIZE_USER_EMAIL", "unit-test@example.invalid")
    monkeypatch.setenv("PAYOPTIMIZE_ADMIN_TOKEN", "test-admin")
    monkeypatch.setenv("PAYOPTIMIZE_SEED", "42")
    return db


@pytest.fixture
def db(env: Path) -> str:
    """The DB path as store.* wants it. Distinct per test via tmp_path."""
    return str(env)


@pytest.fixture
def rng() -> random.Random:
    return random.Random(42)
