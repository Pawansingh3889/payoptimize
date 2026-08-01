"""The one real rail: Prava sandbox.

Ported near-verbatim from canibuy's prava.py, where every function below was
verified against the live sandbox before this project existed (disclosed in the
README). The quirks are load-bearing and none of them are style: the cache-buster
on payment-result, the `Cache-Control: no-store`, the fail-loud user identity,
and the absolute requirement to call report_status. Each one cost time to find.

The module stays synchronous. The async app wraps these calls in a threadpool
rather than rewriting them, because a rewrite is where a verified integration
quietly stops being verified.

Changes from the original, all deliberate:
  * env prefix CANIBUY_ → PAYOPTIMIZE_
  * the merchant-localhost guard is gone — canibuy pointed sessions at a local
    fixture storefront and had to refuse spending real money on it. Here the
    merchant is the API caller, so there is no local target to guard against.
  * a PravaProvider adapter on the end, so the router-facing contract is the
    same ProviderAdapter every simulated rail implements.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from ..models import AttemptStatus
from .base import AttemptOutcome, ChargeRequest

DEFAULT_BASE = "https://sandbox.api.prava.space"


class PravaError(RuntimeError):
    pass


def _load_env() -> None:
    """Read .env into the environment for keys not already set. Tiny and strict
    on purpose — no dotenv dependency, no interpolation, fail loudly later if
    the key is still missing."""
    env = Path(__file__).resolve().parents[3] / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


KEY_MODES = {"sk_test_": "sandbox", "sk_live_": "live"}


def key_mode() -> str:
    """`sandbox` or `live`, read off the key prefix."""
    _load_env()
    secret = os.environ.get("PRAVA_SECRET_KEY", "")
    for prefix, mode in KEY_MODES.items():
        if secret.startswith(prefix):
            return mode
    raise PravaError(
        "PRAVA_SECRET_KEY is missing or is not a Prava key (sk_test_ / sk_live_)"
        " — copy .env.example to .env"
    )


def base_url() -> str:
    _load_env()
    return os.environ.get("PRAVA_API_BASE", DEFAULT_BASE).rstrip("/")


def _secret() -> str:
    """The key, once the key and the host agree about which world we are in.

    A live key is never picked up by accident: PAYOPTIMIZE_ALLOW_LIVE=1 has to
    say so out loud. And a key pointed at the wrong host fails here with a
    sentence rather than at the API with a bare 401 — which is exactly the hour
    the sandbox host cost at the start of the original project.
    """
    mode = key_mode()
    sandbox_host = "sandbox." in base_url()
    if mode == "live":
        if os.environ.get("PAYOPTIMIZE_ALLOW_LIVE", "") != "1":
            raise PravaError(
                "PRAVA_SECRET_KEY is a live key. Set PAYOPTIMIZE_ALLOW_LIVE=1 to spend real"
                " money, or use your sk_test_ key."
            )
        if sandbox_host:
            raise PravaError(
                f"live key pointed at the sandbox host ({base_url()}) — set PRAVA_API_BASE"
                " to the production host or use your sk_test_ key"
            )
    elif not sandbox_host:
        raise PravaError(
            f"sandbox key pointed at the live host ({base_url()}) — this answers 401;"
            f" unset PRAVA_API_BASE to use {DEFAULT_BASE}"
        )
    return os.environ["PRAVA_SECRET_KEY"]


def _user_identity() -> tuple[str, str]:
    """The Prava account a session is minted against.

    No defaults on purpose. Passkeys enrol against this identity, so a made-up
    one mints sessions fine and then fails at verification — FIDO_START_FAILED
    or PASSKEY_REG_FAILED, hundreds of milliseconds after the card is typed and
    far from the cause. Better to refuse before spending a sandbox transaction.
    """
    _load_env()
    user_id = os.environ.get("PAYOPTIMIZE_USER_ID", "").strip()
    user_email = os.environ.get("PAYOPTIMIZE_USER_EMAIL", "").strip()
    if not user_id or not user_email:
        raise PravaError(
            "PAYOPTIMIZE_USER_ID and PAYOPTIMIZE_USER_EMAIL must be set to the Prava account "
            "that owns the passkey — see .env.example. A synthetic user mints sessions "
            "but cannot complete passkey verification."
        )
    return user_id, user_email


def amount_to_decimal(amount_cents: int) -> str:
    """Integer cents everywhere in this system; a decimal string only here.

    This is the single boundary where money changes representation, which is why
    it is one function with one test rather than an f-string at each call site.
    """
    return f"{amount_cents // 100}.{amount_cents % 100:02d}"


def create_session(
    merchant_name: str,
    merchant_url: str,
    merchant_country: str,
    product: dict,
    *,
    http: httpx.Client | None = None,
) -> dict:
    """POST /v1/sessions — returns {session_id, iframe_url, order_id, ...}."""
    user_id, user_email = _user_identity()
    if key_mode() == "live":
        # Nothing else in this codebase spends real money. Say so on the way past.
        print(
            f"!! LIVE Prava session — {product['price']} "
            f"{product.get('currency', 'USD')} at {merchant_name} ({merchant_url})",
            file=sys.stderr,
        )
    body = {
        "user_id": user_id,
        "user_email": user_email,
        "total_amount": str(product["price"]),
        "currency": product.get("currency", "USD"),
        "description": f"PayOptimize: {product.get('name', 'item')}",
        "purchase_context": [
            {
                "merchant_details": {
                    "name": merchant_name,
                    "url": merchant_url,
                    "country_code_iso2": merchant_country,
                },
                "product_details": [
                    {
                        "description": product.get("name", "item"),
                        "unit_price": str(product["price"]),
                        "quantity": 1,
                    }
                ],
            }
        ],
    }
    client = http or httpx.Client(timeout=30)
    try:
        resp = client.post(
            f"{base_url()}/v1/sessions",
            json=body,
            headers={"Authorization": f"Bearer {_secret()}"},
        )
    finally:
        if http is None:
            client.close()
    if resp.status_code != 201:
        raise PravaError(f"sessions API returned {resp.status_code}: {resp.text[:200]}")
    return resp.json()


# The lifecycle has four states, not three: a session that has produced a
# credential sits in `awaiting_result` until the caller reports what the
# downstream authorisation did. Treating that as terminal-but-unreported is
# what keeps orders from hanging.
PENDING_STATES = ("pending",)
TERMINAL_STATES = ("completed", "failed")


def payment_result(session_id: str, *, http: httpx.Client | None = None) -> dict:
    """GET /v1/sessions/{id}/payment-result.

    status is pending | awaiting_result | completed | failed. The cache-buster
    and no-store are required: without them a caching layer happily replays a
    stale `pending` forever.
    """
    client = http or httpx.Client(timeout=30)
    try:
        resp = client.get(
            f"{base_url()}/v1/sessions/{session_id}/payment-result",
            params={"_t": int(time.time() * 1000)},
            headers={"Authorization": f"Bearer {_secret()}", "Cache-Control": "no-store"},
        )
    finally:
        if http is None:
            client.close()
    if resp.status_code != 200:
        raise PravaError(f"payment-result returned {resp.status_code}: {resp.text[:200]}")
    return resp.json()


def credentials(result: dict) -> list[dict]:
    """The payment credentials inside a result.

    They live at transactions[].line_items[] — one level deeper than the
    top-level `transactions` entry, which carries only txn_id and status.
    """
    out = []
    for txn in result.get("transactions") or []:
        for item in txn.get("line_items") or []:
            if item.get("token"):
                out.append({**item, "txn_id": txn.get("txn_id", "")})
    return out


def failure_code(result: dict) -> str:
    """The rail's own code for a failed payment — PASSKEY_REG_FAILED, CARD_DECLINED.

    Lives at transactions[].error.code. Worth storing verbatim: it is the first
    thing Prava support asks for, and it distinguishes failures that look
    identical from the outside.
    """
    for txn in result.get("transactions") or []:
        code = (txn.get("error") or {}).get("code", "")
        if code:
            return code
    return ""


def report_status(
    session_id: str,
    txn_ref_id: str,
    txn_status: str,
    *,
    authorization_code: str = "",
    http: httpx.Client | None = None,
) -> dict:
    """POST /v1/sessions/{id}/report-status — tell Prava how the authorisation went.

    **Required, not optional.** A session whose outcome is never reported stays
    in `awaiting_result` forever and the order never settles. Anything that
    mints a credential owes Prava this call.
    """
    if txn_status not in ("APPROVED", "DECLINED"):
        raise PravaError(f"txn_status must be APPROVED or DECLINED, got {txn_status!r}")
    body: dict = {"txn_ref_id": txn_ref_id, "txn_status": txn_status}
    if authorization_code:
        body["authorization_code"] = authorization_code
    client = http or httpx.Client(timeout=30)
    try:
        resp = client.post(
            f"{base_url()}/v1/sessions/{session_id}/report-status",
            json=body,
            headers={"Authorization": f"Bearer {_secret()}"},
        )
    finally:
        if http is None:
            client.close()
    if resp.status_code >= 400:
        raise PravaError(f"report-status returned {resp.status_code}: {resp.text[:200]}")
    return resp.json() if resp.content else {"reported": txn_status}


def poll_payment(
    session_id: str,
    *,
    interval: float = 3.0,
    deadline: float = 300.0,
    http: httpx.Client | None = None,
) -> dict:
    """Poll until the payment leaves `pending` or the deadline passes.

    The service does not use this — its poller is an asyncio task that must not
    block. Kept because scripts/agent_demo.py and a human at a terminal do.
    """
    end = time.monotonic() + deadline
    while True:
        result = payment_result(session_id, http=http)
        if result.get("status") not in PENDING_STATES or time.monotonic() >= end:
            return result
        time.sleep(interval)


# --- the adapter -------------------------------------------------------------

MERCHANT_NAME = "PayOptimize"
MERCHANT_URL = "https://payoptimize.fly.dev"
MERCHANT_COUNTRY = "US"


@dataclass
class PravaProvider:
    """The real rail, wearing the same contract as the simulated ones.

    `charge` does not authorize anything — it mints a session and returns
    PENDING. A human has to tap a passkey on their phone, which no amount of
    waiting inside a request will make happen. The lifespan poller settles it.
    """

    name: str = "prava"
    display_name: str = "Prava"
    real: bool = True
    # Prava's sandbox does not charge us; these exist so every rail answers the
    # same questions and the fee column is not blank for the one real row.
    fee_bps: int = 0
    fee_fixed_cents: int = 0
    merchant_name: str = MERCHANT_NAME
    merchant_url: str = MERCHANT_URL
    merchant_country: str = MERCHANT_COUNTRY

    async def charge(
        self, request: ChargeRequest, *, http: httpx.Client | None = None
    ) -> AttemptOutcome:
        from starlette.concurrency import run_in_threadpool

        product = {
            "name": request.description or "PayOptimize payment",
            "price": amount_to_decimal(request.amount_cents),
            "currency": request.currency,
        }
        session = await run_in_threadpool(
            create_session,
            self.merchant_name,
            self.merchant_url,
            self.merchant_country,
            product,
            http=http,
        )
        return AttemptOutcome(
            provider=self.name,
            status=AttemptStatus.PENDING,
            prava_session_id=str(session.get("session_id", "")),
            iframe_url=str(session.get("iframe_url", "")),
        )
