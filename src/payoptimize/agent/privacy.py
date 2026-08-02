"""The privacy boundary: what the model may never see.

Built fresh per run from the live environment and the tenants table, so it can
never go stale against either. Two kinds of protection, doing different jobs:

  pseudonyms — tenant names and emails become `tenant_<id>`. Deterministic on
               purpose: the same tenant reads the same across runs (the model
               can reason about "tenant_3 again"), and restoring a name for the
               tenant's own display needs no stored mapping.
  denylist   — literal secret values (API keys, the Prava identity, the admin
               token) plus pattern classes (pok_ keys, sk keys, card-like digit
               runs). These are removed, never pseudonymised: there is nothing
               to reason about, only something to leak.

`llm.complete` asserts the denylist against every outgoing body, so redaction
here is enforced there — a bug in this file fails loudly at the send, not
silently at OpenAI's door.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .. import config, store

REDACTED = "[redacted]"

# Anything that looks like one of our API keys or a secret key from any
# provider, and any bare 13-19 digit run (a PAN until proven otherwise —
# a false positive costs nothing, a false negative is the whole project).
_PATTERNS = (
    re.compile(r"pok_\w+"),
    re.compile(r"\bsk[-_][\w-]+"),
    re.compile(r"\b\d{13,19}\b"),
)


def pseudonym(tenant_id: int) -> str:
    return f"tenant_{tenant_id}"


@dataclass
class Redactor:
    """One run's view of what must not cross, and how to talk around it."""

    secrets: tuple[str, ...]
    # tenant name/email → pseudonym, longest names first so "Acme Corp"
    # cannot be half-replaced via "Acme".
    aliases: tuple[tuple[str, str], ...]
    by_tenant: dict[int, str] = field(default_factory=dict)

    @classmethod
    def build(cls, *, db_path: str | None = None) -> Redactor:
        aliases: list[tuple[str, str]] = []
        by_tenant: dict[int, str] = {}
        for tenant in store.list_tenants(db_path=db_path):
            tenant_id = int(tenant["id"])
            alias = pseudonym(tenant_id)
            by_tenant[tenant_id] = str(tenant["name"])
            for value in (str(tenant["name"]), str(tenant["email"])):
                if value:
                    aliases.append((value, alias))
        aliases.sort(key=lambda pair: -len(pair[0]))
        return cls(secrets=config.secret_values(), aliases=tuple(aliases), by_tenant=by_tenant)

    def redact(self, text: str) -> str:
        for value, alias in self.aliases:
            text = text.replace(value, alias)
        for secret in self.secrets:
            text = text.replace(secret, REDACTED)
        for pattern in _PATTERNS:
            text = pattern.sub(REDACTED, text)
        return text

    def redact_value(self, value: Any) -> Any:
        """Recursively redact every string inside a tool result."""
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, dict):
            return {key: self.redact_value(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.redact_value(item) for item in value]
        return value

    def restore_for(self, text: str, tenant_id: int) -> str:
        """Put the caller's OWN name back into an answer. Only theirs: every
        other tenant stays a pseudonym even in the displayed text."""
        name = self.by_tenant.get(tenant_id)
        if name:
            text = text.replace(pseudonym(tenant_id), name)
        return text

    def denylist(self) -> tuple[str, ...]:
        """The literal strings that must never appear in an outgoing request."""
        return self.secrets + tuple(value for value, _ in self.aliases)
