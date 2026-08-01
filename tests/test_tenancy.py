"""Signup, key minting, and the bearer check — including the property that
matters most: the raw key is not recoverable from the database."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from payoptimize import store, tenancy


def test_signup_mints_a_pok_key(db: str) -> None:
    tenant_id, key = tenancy.signup("Acme Corp", "ops@acme.test", db_path=db)

    assert tenant_id > 0
    assert key.startswith("pok_")
    assert len(key) == len("pok_") + 48

    tenant = store.get_tenant(tenant_id, db_path=db)
    assert tenant is not None
    assert tenant["name"] == "Acme Corp"
    assert tenant["fee_bps"] == store.DEFAULT_FEE_BPS
    assert tenant["fee_fixed_cents"] == store.DEFAULT_FEE_FIXED_CENTS


def test_keys_are_unique(db: str) -> None:
    _, first = tenancy.signup("A", "a@a.test", db_path=db)
    _, second = tenancy.signup("B", "b@b.test", db_path=db)
    assert first != second


def test_raw_key_is_never_stored(db: str) -> None:
    _, key = tenancy.signup("Acme", "ops@acme.test", db_path=db)

    # Read the raw bytes, WAL sibling included — a checkpoint has not run yet.
    blob = b""
    for path in (Path(db), Path(f"{db}-wal"), Path(f"{db}-shm")):
        if path.exists():
            blob += path.read_bytes()

    assert key.encode() not in blob
    assert tenancy.hash_key(key).encode() in blob
    assert tenancy.display_prefix(key).encode("utf-8") in blob


def test_authenticate_round_trip(db: str) -> None:
    tenant_id, key = tenancy.signup("Acme", "ops@acme.test", db_path=db)

    tenant = tenancy.authenticate(f"Bearer {key}", db_path=db)

    assert tenant["id"] == tenant_id
    assert tenant["key_prefix"] == tenancy.display_prefix(key)


@pytest.mark.parametrize(
    "header",
    [None, "", "Bearer", "Bearer   ", "Token pok_abc", f"Basic {'x' * 10}"],
)
def test_malformed_authorization_is_rejected(db: str, header: str | None) -> None:
    with pytest.raises(tenancy.AuthError):
        tenancy.authenticate(header, db_path=db)


def test_unknown_key_is_rejected(db: str) -> None:
    tenancy.signup("Acme", "ops@acme.test", db_path=db)
    with pytest.raises(tenancy.AuthError, match="unknown or revoked"):
        tenancy.authenticate(f"Bearer pok_{'0' * 48}", db_path=db)


def test_non_pok_token_is_rejected(db: str) -> None:
    with pytest.raises(tenancy.AuthError, match="pok_"):
        tenancy.authenticate("Bearer sk_live_something", db_path=db)


def test_revoked_key_stops_working(db: str) -> None:
    _, key = tenancy.signup("Acme", "ops@acme.test", db_path=db)
    tenancy.authenticate(f"Bearer {key}", db_path=db)

    tenancy.revoke(key, db_path=db)

    with pytest.raises(tenancy.AuthError, match="unknown or revoked"):
        tenancy.authenticate(f"Bearer {key}", db_path=db)
    # Revoking twice is an error, not a silent no-op.
    with pytest.raises(tenancy.AuthError):
        tenancy.revoke(key, db_path=db)


def test_one_tenants_key_does_not_resolve_to_another(db: str) -> None:
    first_id, first_key = tenancy.signup("First", "a@first.test", db_path=db)
    second_id, second_key = tenancy.signup("Second", "b@second.test", db_path=db)

    assert tenancy.resolve(first_key, db_path=db)["id"] == first_id
    assert tenancy.resolve(second_key, db_path=db)["id"] == second_id


@pytest.mark.parametrize(
    ("name", "email"),
    [("", "ops@acme.test"), ("   ", "ops@acme.test"), ("Acme", "not-an-email"), ("Acme", "a@b")],
)
def test_signup_refuses_junk(db: str, name: str, email: str) -> None:
    with pytest.raises(ValidationError):
        tenancy.signup(name, email, db_path=db)
