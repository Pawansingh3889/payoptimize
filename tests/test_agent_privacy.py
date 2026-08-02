"""The redactor: tenants become pseudonyms, secrets become nothing."""

from __future__ import annotations

import pytest

from payoptimize import store
from payoptimize.agent.privacy import REDACTED, Redactor, pseudonym


def _tenant(db: str, name: str, email: str) -> int:
    return store.create_tenant_with_key(name, email, f"hash-{name}", "pok_display…", db_path=db)


@pytest.fixture
def redactor(db: str, monkeypatch: pytest.MonkeyPatch) -> Redactor:
    monkeypatch.setenv("PRAVA_SECRET_KEY", "sk_test_prava_secret_value")
    monkeypatch.setenv("PAYOPTIMIZE_USER_ID", "owner@real.example")
    monkeypatch.setenv("PAYOPTIMIZE_USER_EMAIL", "owner@real.example")
    _tenant(db, "Acme Corp GmbH", "finance@acme.example")
    _tenant(db, "Acme", "ops@acme.example")
    return Redactor.build(db_path=db)


def test_tenant_names_and_emails_become_stable_pseudonyms(redactor: Redactor) -> None:
    text = "Acme asked about finance@acme.example and Acme Corp GmbH"
    redacted = redactor.redact(text)
    assert "Acme" not in redacted
    assert "acme.example" not in redacted
    # Longest-first: the longer tenant name is one pseudonym, not a partial hit.
    assert "tenant_1" in redacted
    assert "tenant_2" in redacted
    assert redactor.redact(text) == redacted  # stable across calls


def test_secrets_and_patterns_are_removed(redactor: Redactor) -> None:
    text = (
        "key sk_test_prava_secret_value, api key pok_a1b2c3d4e5f6, openai sk-proj-abc123,"
        " identity owner@real.example, card 4242424242424242"
    )
    redacted = redactor.redact(text)
    assert "sk_test_prava_secret_value" not in redacted
    assert "pok_a1b2c3d4e5f6" not in redacted
    assert "sk-proj-abc123" not in redacted
    assert "owner@real.example" not in redacted
    assert "4242424242424242" not in redacted
    assert REDACTED in redacted


def test_redact_value_walks_nested_structures(redactor: Redactor) -> None:
    value = {
        "payment": {"description": "invoice for Acme", "amount_cents": 1250},
        "notes": ["contact finance@acme.example", 42],
    }
    redacted = redactor.redact_value(value)
    assert redacted["payment"]["description"] == f"invoice for {pseudonym(2)}"
    assert redacted["payment"]["amount_cents"] == 1250
    assert "acme.example" not in redacted["notes"][0]
    assert redacted["notes"][1] == 42


def test_restore_puts_back_only_the_callers_own_name(redactor: Redactor) -> None:
    answer = "tenant_2 had two declines; tenant_1 is unaffected"
    restored = redactor.restore_for(answer, 2)
    assert restored.startswith("Acme had two declines")
    assert "tenant_1" in restored  # the other tenant stays a pseudonym


def test_denylist_carries_every_literal_that_must_not_cross(redactor: Redactor) -> None:
    denylist = redactor.denylist()
    for value in (
        "sk_test_prava_secret_value",
        "owner@real.example",
        "Acme Corp GmbH",
        "Acme",
        "finance@acme.example",
        "ops@acme.example",
    ):
        assert value in denylist


def test_an_empty_database_still_builds(db: str, monkeypatch: pytest.MonkeyPatch) -> None:
    store.init_db(db)
    redactor = Redactor.build(db_path=db)
    assert redactor.redact("nothing sensitive here") == "nothing sensitive here"
