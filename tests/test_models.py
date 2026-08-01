"""Request validation, and the segment key the bandit indexes its arms by."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from payoptimize.models import (
    CardBrand,
    PaymentMethod,
    PaymentRequest,
    RoutingMode,
    new_payment_id,
    segment_key,
    utc_now_iso,
)


def test_segment_key_normalizes_case() -> None:
    assert segment_key("us", "usd", "VISA") == "US:USD:visa"


def test_card_request_normalizes_and_builds_its_segment() -> None:
    request = PaymentRequest(
        amount_cents=1250, currency="usd", country="de", card_brand=CardBrand.VISA
    )

    assert request.currency == "USD"
    assert request.country == "DE"
    assert request.method is PaymentMethod.CARD
    assert request.routing_mode is None  # the server decides
    assert request.segment == "DE:USD:visa"


def test_prava_request_may_omit_the_corridor() -> None:
    request = PaymentRequest(amount_cents=1250, currency="USD", method=PaymentMethod.PRAVA)

    assert request.card_brand is None
    # The brand slot is never a real card brand, so a prava row cannot collide
    # with a bandit arm.
    assert request.segment == ":USD:prava"


def test_card_request_requires_a_corridor() -> None:
    with pytest.raises(ValidationError, match="card payments require"):
        PaymentRequest(amount_cents=1250, currency="USD", country="US")
    with pytest.raises(ValidationError, match="card payments require"):
        PaymentRequest(amount_cents=1250, currency="USD", card_brand=CardBrand.VISA)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"amount_cents": 0, "currency": "USD", "country": "US", "card_brand": "visa"},
        {"amount_cents": -5, "currency": "USD", "country": "US", "card_brand": "visa"},
        {"amount_cents": 100, "currency": "US", "country": "US", "card_brand": "visa"},
        {"amount_cents": 100, "currency": "US$", "country": "US", "card_brand": "visa"},
        {"amount_cents": 100, "currency": "USD", "country": "USA", "card_brand": "visa"},
        {"amount_cents": 100, "currency": "USD", "country": "US", "card_brand": "diners"},
        {"amount_cents": 100, "currency": "USD", "country": "US", "card_brand": "visa", "typo": 1},
    ],
)
def test_bad_payment_requests_are_refused(kwargs: dict) -> None:
    with pytest.raises(ValidationError):
        PaymentRequest(**kwargs)


def test_routing_mode_can_be_forced() -> None:
    request = PaymentRequest(
        amount_cents=100,
        currency="USD",
        country="US",
        card_brand=CardBrand.VISA,
        routing_mode=RoutingMode.BASELINE,
    )
    assert request.routing_mode is RoutingMode.BASELINE


def test_payment_ids_are_unique_and_prefixed() -> None:
    ids = {new_payment_id() for _ in range(100)}
    assert len(ids) == 100
    assert all(pid.startswith("pay_") and len(pid) == len("pay_") + 16 for pid in ids)


def test_timestamps_sort_chronologically_as_text() -> None:
    stamps = [utc_now_iso() for _ in range(5)]
    assert stamps == sorted(stamps)
    assert stamps[0].endswith("+00:00")
