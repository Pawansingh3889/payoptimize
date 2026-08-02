"""The shapes that cross module boundaries, and the enums persisted as wire
format in every SQLite row.

Two rules hold everywhere below: money is an integer count of cents (the
decimal-string conversion happens only at the Prava adapter boundary), and
timestamps are ISO-8601 UTC strings at fixed precision so SQLite's
lexicographic ordering is chronological ordering.
"""

from __future__ import annotations

import enum
import secrets
from datetime import UTC, datetime, timedelta
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class PaymentStatus(enum.StrEnum):
    PENDING = "pending"
    PENDING_APPROVAL = "pending_approval"  # prava: waiting on a human passkey tap
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class AttemptStatus(enum.StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class PaymentMethod(enum.StrEnum):
    CARD = "card"  # routed across the simulated rails by the bandit
    PRAVA = "prava"  # the real sandbox rail; bypasses the bandit entirely


class RoutingMode(enum.StrEnum):
    ROUTER = "router"  # discounted Thompson sampling
    BASELINE = "baseline"  # round-robin, same cascade rules — the A/B control


class PaymentSource(enum.StrEnum):
    API = "api"
    GENERATOR = "generator"
    AGENT = "agent"


class CardBrand(enum.StrEnum):
    VISA = "visa"
    MASTERCARD = "mastercard"
    AMEX = "amex"


class DeclineCode(enum.StrEnum):
    """Why an attempt failed. The infrastructure/terminal split that drives the
    cascade lives in providers/base.py — this enum is only the vocabulary.

    Prava returns its own failure codes, stored verbatim rather than mapped
    (it is the first thing their support asks for), so the decline_code column
    is a plain TEXT field that this enum does not exhaust.
    """

    ISSUER_UNAVAILABLE = "issuer_unavailable"
    PROVIDER_TIMEOUT = "provider_timeout"
    PROCESSING_ERROR = "processing_error"
    RATE_LIMITED = "rate_limited"
    DO_NOT_HONOR = "do_not_honor"
    INSUFFICIENT_FUNDS = "insufficient_funds"
    EXPIRED_CARD = "expired_card"
    INVALID_CARD = "invalid_card"
    FRAUD_SUSPECTED = "fraud_suspected"
    STOLEN_CARD = "stolen_card"
    APPROVAL_TIMEOUT = "approval_timeout"  # prava: nobody tapped within the deadline


# The brand slot of a prava payment's segment. Never a real card brand, so a
# prava row can never collide with a bandit arm.
PRAVA_SEGMENT_BRAND = "prava"


def utc_now_iso() -> str:
    """Every timestamp in the system. Same offset and same precision on every
    row, which is what makes `ORDER BY created_ts` and window filters correct."""
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def iso_since(seconds: float) -> str:
    """The timestamp `seconds` ago, in the same format every row is written in —
    which is what lets a rolling window be a plain text comparison in SQL."""
    return (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat(timespec="milliseconds")


def new_payment_id() -> str:
    """`pay_<16 hex>`. Also the A/B split's input — see router.assign_mode."""
    return f"pay_{secrets.token_hex(8)}"


def segment_key(country: str, currency: str, card_brand: str) -> str:
    """`US:USD:visa` — the bandit's arm dimension and the corridor label."""
    return f"{country.upper()}:{currency.upper()}:{card_brand.lower()}"


class SignupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    email: str = Field(min_length=3, max_length=254)
    # The storefront this merchant sells from. Prava records it on every session
    # as the merchant of record, so it is the merchant's own site — not ours.
    merchant_name: str = Field(default="", max_length=120)
    merchant_url: str = Field(default="", max_length=300)
    merchant_country: str = Field(default="", max_length=2)

    @field_validator("name", "email")
    @classmethod
    def _stripped(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("merchant_url")
    @classmethod
    def _merchant_url(cls, value: str) -> str:
        value = value.strip()
        if value and not value.startswith(("http://", "https://")):
            raise ValueError("must be a URL starting http:// or https://")
        return value

    @field_validator("email")
    @classmethod
    def _looks_like_email(cls, value: str) -> str:
        local, at, domain = value.partition("@")
        if not at or not local or "." not in domain:
            raise ValueError("must be an email address")
        return value


class PaymentRequest(BaseModel):
    """What POST /v1/payments accepts. `extra="forbid"` on purpose: a typo'd
    field name is a routing decision made on data we never received."""

    model_config = ConfigDict(extra="forbid")

    amount_cents: int = Field(gt=0)
    currency: str
    country: str = ""
    card_brand: CardBrand | None = None
    method: PaymentMethod = PaymentMethod.CARD
    description: str = Field(default="", max_length=200)
    routing_mode: RoutingMode | None = None  # None → the server decides

    @field_validator("currency")
    @classmethod
    def _currency(cls, value: str) -> str:
        value = value.strip().upper()
        if len(value) != 3 or not value.isalpha():
            raise ValueError("must be a 3-letter ISO-4217 code, e.g. USD")
        return value

    @field_validator("country")
    @classmethod
    def _country(cls, value: str) -> str:
        value = value.strip().upper()
        if value and (len(value) != 2 or not value.isalpha()):
            raise ValueError("must be a 2-letter ISO-3166 code, e.g. US")
        return value

    @model_validator(mode="after")
    def _card_needs_a_corridor(self) -> Self:
        """A card payment's corridor picks the bandit arm, so it cannot be
        guessed. A prava payment never reaches the bandit, so it may omit both."""
        if self.method is PaymentMethod.CARD and (not self.country or self.card_brand is None):
            raise ValueError(
                "card payments require `country` and `card_brand` — they select the"
                " routing corridor; only method='prava' may omit them"
            )
        return self

    @property
    def segment(self) -> str:
        if self.method is PaymentMethod.PRAVA:
            return segment_key(self.country, self.currency, PRAVA_SEGMENT_BRAND)
        assert self.card_brand is not None  # guaranteed by _card_needs_a_corridor
        return segment_key(self.country, self.currency, self.card_brand.value)


class AttemptView(BaseModel):
    """One row of the cascade, as the API returns it."""

    seq: int
    provider: str
    status: AttemptStatus
    decline_code: str = ""
    latency_ms: int = 0
    iframe_url: str = ""  # prava only: the approval URL


class PaymentResponse(BaseModel):
    id: str
    status: PaymentStatus
    amount_cents: int
    currency: str
    country: str = ""
    method: PaymentMethod
    routing_mode: RoutingMode
    segment: str
    provider: str = ""  # the provider that finally authorized, if any
    decline_code: str = ""
    description: str = ""
    source: PaymentSource = PaymentSource.API
    created_ts: str
    resolved_ts: str = ""
    attempts: list[AttemptView] = Field(default_factory=list)
    iframe_url: str = ""  # prava: hand this to the human to approve


class Tenant(BaseModel):
    id: int
    name: str
    email: str
    created_ts: str
    fee_bps: int
    fee_fixed_cents: int


class SignupResponse(BaseModel):
    """The api_key is returned exactly once — only its sha256 is stored."""

    tenant_id: int
    api_key: str
    key_prefix: str
