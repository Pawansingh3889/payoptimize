"""A small client for the PayOptimize REST API.

The MCP server and scripts/agent_demo.py are both built on this, and so are the
tests — the `http=` seam takes Starlette's TestClient, so the SDK is exercised
against the real app without a socket. One implementation, three surfaces: a bug
fixed here cannot survive on one of them.
"""

from __future__ import annotations

import time
from typing import Any, Self

import httpx

DEFAULT_TIMEOUT = 30.0
DEFAULT_WAIT_SECONDS = 180.0
POLL_INTERVAL_SECONDS = 3.0

# Statuses that will not change without something else happening.
TERMINAL = ("succeeded", "failed")


class PayOptimizeError(RuntimeError):
    """The API answered with an error. Carries the status so a caller — often an
    agent — can tell "your request was wrong" from "try again later"."""

    def __init__(self, message: str, status_code: int = 0) -> None:
        super().__init__(message)
        self.status_code = status_code


class PayOptimizeClient:
    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        *,
        http: httpx.Client | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._http = http
        self._owned = http is None
        self._timeout = timeout

    # --- plumbing ------------------------------------------------------------

    @property
    def http(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(timeout=self._timeout)
        return self._http

    def close(self) -> None:
        if self._owned and self._http is not None:
            self._http.close()
            self._http = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _headers(self, auth: bool) -> dict[str, str]:
        if not auth:
            return {}
        if not self.api_key:
            raise PayOptimizeError("no API key — create one with PayOptimizeClient.signup()")
        return {"Authorization": f"Bearer {self.api_key}"}

    def _request(
        self,
        method: str,
        path: str,
        *,
        auth: bool = True,
        json: dict | None = None,
        params: dict | None = None,
    ) -> Any:
        response = self.http.request(
            method,
            f"{self.base_url}{path}",
            json=json,
            params=params,
            headers=self._headers(auth),
        )
        if response.status_code >= 400:
            detail = ""
            try:
                detail = str(response.json().get("error", ""))
            except (ValueError, AttributeError):
                # Not JSON, or JSON that is not an object — a proxy error page
                # rather than one of ours. The body is still the best clue.
                detail = response.text[:200]
            raise PayOptimizeError(
                detail or f"{method} {path} failed", status_code=response.status_code
            )
        return response.json()

    # --- the API -------------------------------------------------------------

    @classmethod
    def signup(
        cls,
        base_url: str,
        name: str,
        email: str,
        *,
        http: httpx.Client | None = None,
    ) -> PayOptimizeClient:
        """Create a tenant and return a client holding its key.

        The key is returned exactly once by the API, so it is captured here
        rather than fetched again later — there is no later.
        """
        client = cls(base_url, http=http)
        body = client._request(
            "POST", "/v1/tenants", auth=False, json={"name": name, "email": email}
        )
        client.api_key = str(body["api_key"])
        return client

    def create_payment(
        self,
        amount_cents: int,
        currency: str = "USD",
        *,
        country: str = "",
        card_brand: str = "",
        method: str = "card",
        description: str = "",
        routing_mode: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "amount_cents": amount_cents,
            "currency": currency,
            "method": method,
        }
        # Sent only when set: the API forbids unknown fields and requires a
        # corridor for card payments, so an empty string would be a 422.
        if country:
            body["country"] = country
        if card_brand:
            body["card_brand"] = card_brand
        if description:
            body["description"] = description
        if routing_mode:
            body["routing_mode"] = routing_mode
        result: dict[str, Any] = self._request("POST", "/v1/payments", json=body)
        return result

    def get_payment(self, payment_id: str) -> dict[str, Any]:
        result: dict[str, Any] = self._request("GET", f"/v1/payments/{payment_id}")
        return result

    def list_payments(self, limit: int = 10, status: str = "") -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        body = self._request("GET", "/v1/payments", params=params)
        payments: list[dict[str, Any]] = body["payments"]
        return payments

    def wait_for_payment(
        self,
        payment_id: str,
        *,
        timeout_s: float = DEFAULT_WAIT_SECONDS,
        interval: float = POLL_INTERVAL_SECONDS,
        sleep: Any = time.sleep,
    ) -> dict[str, Any]:
        """Block until the payment resolves or the deadline passes.

        A Prava payment is waiting on a human with a phone, so this can legally
        take minutes. `sleep` is injectable so tests do not spend that time; the
        deadline is a real deadline rather than an unbounded loop, because an
        agent hanging forever is worse than one that reports it gave up.
        """
        deadline = time.monotonic() + timeout_s
        while True:
            payment = self.get_payment(payment_id)
            if payment["status"] in TERMINAL or time.monotonic() >= deadline:
                return payment
            sleep(interval)

    def ask_agent(self, question: str) -> dict[str, Any]:
        """Ask the ops agent about this merchant's payments."""
        result: dict[str, Any] = self._request("POST", "/v1/agent/ask", json={"question": question})
        return result

    def diagnose_payment(self, payment_id: str) -> dict[str, Any]:
        """Why did this payment do what it did?"""
        result: dict[str, Any] = self._request(
            "POST", "/v1/agent/diagnose", json={"payment_id": payment_id}
        )
        return result

    def agent_runs(self, limit: int = 10) -> list[dict[str, Any]]:
        body = self._request("GET", "/v1/agent/runs", params={"limit": limit})
        runs: list[dict[str, Any]] = body["runs"]
        return runs

    def provider_health(self) -> list[dict[str, Any]]:
        body = self._request("GET", "/v1/providers", auth=False)
        providers: list[dict[str, Any]] = body["providers"]
        return providers

    def analytics(self, window: str = "15m") -> dict[str, Any]:
        result: dict[str, Any] = self._request(
            "GET", "/v1/analytics/summary", params={"window": window}
        )
        return result
