"""Test environment: a throwaway database and a Prava host that cannot resolve.

PRAVA_API_BASE points at an unregistrable TLD on purpose. Every Prava call in
the suite goes through an httpx.MockTransport; anything that slips past a mock
dies at DNS instead of spending one of a finite number of sandbox transactions.
"""

from __future__ import annotations

import random
from pathlib import Path

import httpx
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


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make a real socket impossible, rather than merely unlikely.

    The unregistrable host above stops a leak from reaching Prava, but it would
    still fail as a DNS error somewhere confusing. This turns any un-mocked
    request into an immediate, obvious failure naming the test that made it —
    and guarantees the suite can never spend one of a finite number of sandbox
    transactions, which PLAN §11 lists as the project's live risk.

    Starlette's TestClient uses ASGITransport and httpx.MockTransport is its own
    class, so neither is affected.
    """

    def deny(self: object, request: object, *args: object, **kwargs: object) -> None:
        raise RuntimeError(
            f"a test tried to open a real network connection to {request} — "
            "use httpx.MockTransport via the http= seam"
        )

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", deny)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", deny)


@pytest.fixture
def db(env: Path) -> str:
    """The DB path as store.* wants it. Distinct per test via tmp_path."""
    return str(env)


@pytest.fixture
def rng() -> random.Random:
    return random.Random(42)
