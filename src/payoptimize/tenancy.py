"""Merchants, their API keys, and the bearer check in front of /v1.

A key is shown exactly once, at signup. Only its sha256 is stored, so a leaked
database cannot be replayed against the API — `display_prefix` is what the UI
gets to show. Every failure here is one typed error the API maps to 401.
"""

from __future__ import annotations

import hashlib
import secrets
from typing import Any

from . import store
from .models import SignupRequest

KEY_PREFIX = "pok_"
_KEY_BYTES = 24  # 48 hex chars after the prefix
_DISPLAY_CHARS = 12  # "pok_a1b2c3d4…"


class AuthError(RuntimeError):
    """The caller's credential is missing, malformed, unknown, or revoked."""


def mint_key() -> str:
    return f"{KEY_PREFIX}{secrets.token_hex(_KEY_BYTES)}"


def hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def display_prefix(raw_key: str) -> str:
    return f"{raw_key[:_DISPLAY_CHARS]}…"


def signup(name: str, email: str, *, db_path: str | None = None) -> tuple[int, str]:
    """Create a tenant and its first key. Returns (tenant_id, raw key) — the
    only moment the raw key exists outside the caller's process."""
    request = SignupRequest(name=name, email=email)
    raw_key = mint_key()
    tenant_id = store.create_tenant_with_key(
        request.name,
        request.email,
        hash_key(raw_key),
        display_prefix(raw_key),
        db_path=db_path,
    )
    return tenant_id, raw_key


def bearer_token(authorization: str | None) -> str:
    """Pull the token out of an Authorization header, or say what was wrong."""
    if not authorization:
        raise AuthError("missing Authorization header — send `Authorization: Bearer pok_…`")
    scheme, _, token = authorization.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        raise AuthError("Authorization must be `Bearer <token>`")
    return token


def resolve(raw_key: str, *, db_path: str | None = None) -> dict[str, Any]:
    """The tenant behind a key. Raises AuthError for anything that is not a
    live key — unknown and revoked are deliberately indistinguishable."""
    if not raw_key.startswith(KEY_PREFIX):
        raise AuthError(f"not a PayOptimize API key — keys start with {KEY_PREFIX}")
    tenant = store.tenant_for_key_hash(hash_key(raw_key), db_path=db_path)
    if tenant is None:
        raise AuthError("unknown or revoked API key")
    return tenant


def authenticate(authorization: str | None, *, db_path: str | None = None) -> dict[str, Any]:
    """Header in, tenant row out. What every /v1 route calls."""
    return resolve(bearer_token(authorization), db_path=db_path)


def revoke(raw_key: str, *, db_path: str | None = None) -> None:
    if not store.revoke_api_key_by_hash(hash_key(raw_key), db_path=db_path):
        raise AuthError("unknown or already-revoked API key")
