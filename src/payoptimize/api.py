"""The single Starlette app: REST, and later the dashboard and admin routes.

Everything a merchant, an agent, or a judge touches goes through here. The MCP
server and the SDK are thin clients over these same routes — one implementation,
three surfaces, so a bug cannot be fixed on one and left standing on another.

Errors are typed and boring on purpose: a JSON body with an `error` key and a
status code that means what it says, because the caller on the other end may
well be an agent that has to decide what to do about it.
"""

from __future__ import annotations

import contextlib
import math
from collections.abc import AsyncIterator
from typing import Any

from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import store, tenancy
from .engine import Engine, RailUnavailable, to_response
from .models import (
    PaymentMethod,
    PaymentRequest,
    PaymentStatus,
    RoutingMode,
    SignupRequest,
    SignupResponse,
    iso_since,
)

DEFAULT_WINDOW_SECONDS = 15 * 60
MAX_PAGE = 200
# Per arm, below which an uplift figure is reported but never called measured.
# At 30/30 the 95% interval on the difference is roughly ±16 pts.
MIN_ARM_VOLUME = 100


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _engine(request: Request) -> Engine:
    engine: Engine = request.app.state.engine
    return engine


def _tenant(request: Request) -> dict[str, Any]:
    """Resolve the bearer key, or raise the 401 the caller has earned."""
    try:
        return tenancy.authenticate(
            request.headers.get("authorization"), db_path=_engine(request).db_path
        )
    except tenancy.AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=422, detail="body must be JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="body must be a JSON object")
    return body


def _window_seconds(request: Request) -> int:
    """`?window=15m`. An unparseable window is a 422 rather than a silent
    fallback — a dashboard quietly showing the wrong period is worse than one
    that says it cannot."""
    raw = request.query_params.get("window", "").strip().lower()
    if not raw:
        return DEFAULT_WINDOW_SECONDS
    units = {"s": 1, "m": 60, "h": 3600}
    if raw[-1] not in units or not raw[:-1].isdigit():
        raise HTTPException(
            status_code=422, detail=f"window {raw!r} must look like 30s, 15m, or 1h"
        )
    seconds = int(raw[:-1]) * units[raw[-1]]
    if not 0 < seconds <= 24 * 3600:
        raise HTTPException(status_code=422, detail="window must be between 1s and 24h")
    return seconds


def _limit(request: Request, default: int = 50) -> int:
    raw = request.query_params.get("limit")
    if raw is None:
        return default
    if not raw.isdigit() or not 0 < int(raw) <= MAX_PAGE:
        raise HTTPException(status_code=422, detail=f"limit must be 1..{MAX_PAGE}")
    return int(raw)


# --- routes ------------------------------------------------------------------


async def index(request: Request) -> JSONResponse:
    """A self-describing index, so an agent that finds the base URL can work
    out the rest without a human fetching documentation for it."""
    return JSONResponse(
        {
            "service": "payoptimize",
            "description": (
                "Payment orchestration that routes each payment to the provider most"
                " likely to authorize it, and learns from every outcome."
            ),
            "rails": {
                "prava": "REAL · sandbox — human passkey approval, selected by method='prava'",
                "simulated": "stripe_sim, braintree_sim, adyen_sim — SIMULATED, routed by the bandit",
            },
            "auth": "Authorization: Bearer pok_… (create one at POST /v1/tenants)",
            "endpoints": {
                "POST /v1/tenants": "sign up, returns an API key once",
                "POST /v1/payments": "create a payment",
                "GET /v1/payments/{id}": "one payment and its attempt chain",
                "GET /v1/payments": "recent payments for your tenant",
                "GET /v1/analytics/summary": "auth rate, uplift, volume by provider and corridor",
            },
        }
    )


async def create_tenant(request: Request) -> JSONResponse:
    body = await _json_body(request)
    signup = SignupRequest(**body)
    tenant_id, api_key = tenancy.signup(signup.name, signup.email, db_path=_engine(request).db_path)
    response = SignupResponse(
        tenant_id=tenant_id, api_key=api_key, key_prefix=tenancy.display_prefix(api_key)
    )
    return JSONResponse(response.model_dump(), status_code=201)


async def create_payment(request: Request) -> JSONResponse:
    tenant = _tenant(request)
    body = await _json_body(request)
    payment = PaymentRequest(**body)
    engine = _engine(request)
    response = await engine.execute(payment, tenant_id=int(tenant["id"]))
    return JSONResponse(response.model_dump(), status_code=201)


async def get_payment(request: Request) -> JSONResponse:
    tenant = _tenant(request)
    engine = _engine(request)
    row = store.get_payment(request.path_params["payment_id"], db_path=engine.db_path)
    # A payment belonging to someone else is indistinguishable from one that
    # does not exist. Anything else leaks which ids are real.
    if row is None or row["tenant_id"] != tenant["id"]:
        raise HTTPException(status_code=404, detail="no such payment")
    attempts = store.attempts_for_payment(row["id"], db_path=engine.db_path)
    return JSONResponse(to_response(row, attempts).model_dump())


async def list_payments(request: Request) -> JSONResponse:
    tenant = _tenant(request)
    engine = _engine(request)
    status = request.query_params.get("status")
    if status and status not in set(PaymentStatus):
        raise HTTPException(
            status_code=422,
            detail=f"status must be one of {', '.join(sorted(PaymentStatus))}",
        )
    rows = store.list_payments(
        tenant_id=int(tenant["id"]),
        status=status,
        limit=_limit(request),
        db_path=engine.db_path,
    )
    payments = [
        to_response(row, store.attempts_for_payment(row["id"], db_path=engine.db_path))
        for row in rows
    ]
    return JSONResponse({"payments": [p.model_dump() for p in payments]})


def _rate(succeeded: float, volume: float) -> float:
    return round(succeeded / volume, 4) if volume else 0.0


def uplift_stat(router_ok: int, router_n: int, baseline_ok: int, baseline_n: int) -> dict[str, Any]:
    """The difference between the two arms, with its uncertainty attached.

    PLAN §1.4 says uplift is measured, not claimed. A bare point estimate does
    not honour that: at 30 payments per arm the 95% interval is about ±16 pts,
    so an early sample will read −10 or +12 with equal ease. Showing either as
    "the uplift" is a claim dressed as a measurement, and the number that lands
    on stage during the first minute of a demo would be pure noise.

    So the point estimate always ships alongside a Wald interval for the
    difference of two proportions, and a status that says which of three things
    is true: no data, still collecting, or an interval that excludes zero.
    """
    if not router_n or not baseline_n:
        return {"pts": None, "ci95_pts": None, "status": "no_data", "significant": False}

    router_rate = router_ok / router_n
    baseline_rate = baseline_ok / baseline_n
    pts = (router_rate - baseline_rate) * 100
    standard_error = math.sqrt(
        router_rate * (1 - router_rate) / router_n
        + baseline_rate * (1 - baseline_rate) / baseline_n
    )
    margin = 1.96 * standard_error * 100
    low, high = pts - margin, pts + margin

    # Below the floor, refuse "significant" outright: with a handful of samples
    # a run of luck can produce a zero-width interval (every payment authorized
    # in both arms) that is confident about nothing at all.
    enough = router_n >= MIN_ARM_VOLUME and baseline_n >= MIN_ARM_VOLUME
    significant = enough and (low > 0 or high < 0)
    return {
        "pts": round(pts, 2),
        "ci95_pts": [round(low, 2), round(high, 2)],
        "status": "measured" if significant else "collecting",
        "significant": significant,
        "min_arm_volume": MIN_ARM_VOLUME,
    }


async def analytics_summary(request: Request) -> JSONResponse:
    """The uplift number, computed rather than asserted.

    Card payments only: Prava is settled by a human tapping a passkey, so
    folding it into a routing auth-rate would be measuring the customer, not
    the router. Prava's own outcomes show up per-provider instead.
    """
    tenant = _tenant(request)
    engine = _engine(request)
    seconds = _window_seconds(request)
    since = iso_since(seconds)
    tenant_id = int(tenant["id"])

    by_mode = {
        row["routing_mode"]: row
        for row in store.payments_by_mode(
            since=since,
            tenant_id=tenant_id,
            method=PaymentMethod.CARD,
            db_path=engine.db_path,
        )
    }
    volume = sum(int(row["volume"]) for row in by_mode.values())
    succeeded = sum(int(row["succeeded"] or 0) for row in by_mode.values())

    modes = {}
    counts = {}
    for mode in (RoutingMode.ROUTER, RoutingMode.BASELINE):
        row = by_mode.get(mode)
        n = int(row["volume"]) if row else 0
        ok = int(row["succeeded"] or 0) if row else 0
        counts[mode.value] = (ok, n)
        modes[mode.value] = {"volume": n, "auth_rate": _rate(ok, n)}

    uplift = uplift_stat(*counts["router"], *counts["baseline"])

    providers = [
        {
            "provider": row["provider"],
            "attempts": int(row["attempts"]),
            "auth_rate": _rate(int(row["succeeded"] or 0), int(row["attempts"])),
            "avg_latency_ms": int(row["avg_latency_ms"] or 0),
        }
        for row in store.attempts_by_provider(since=since, db_path=engine.db_path)
    ]
    corridors: dict[str, dict[str, Any]] = {}
    for row in store.payments_by_corridor(
        since=since, tenant_id=tenant_id, method=PaymentMethod.CARD, db_path=engine.db_path
    ):
        entry = corridors.setdefault(
            row["segment"], {"segment": row["segment"], "volume": 0, "succeeded": 0}
        )
        entry["volume"] += int(row["volume"])
        entry["succeeded"] += int(row["succeeded"] or 0)
        entry[f"{row['routing_mode']}_auth_rate"] = _rate(
            int(row["succeeded"] or 0), int(row["volume"])
        )
    for entry in corridors.values():
        entry["auth_rate"] = _rate(entry.pop("succeeded"), entry["volume"])

    avg_latency = 0
    total_attempts = sum(p["attempts"] for p in providers)
    if total_attempts:
        avg_latency = int(
            sum(p["avg_latency_ms"] * p["attempts"] for p in providers) / total_attempts
        )

    return JSONResponse(
        {
            "window_seconds": seconds,
            "volume": volume,
            "auth_rate": _rate(succeeded, volume),
            "avg_latency_ms": avg_latency,
            # The point estimate stays at the top level for callers that just
            # want a number; `uplift` carries the interval that says whether
            # the number means anything yet.
            "uplift_pts": uplift["pts"],
            "uplift": uplift,
            "by_mode": modes,
            "by_provider": providers,
            "by_corridor": sorted(corridors.values(), key=lambda c: c["segment"]),
        }
    )


# --- app ---------------------------------------------------------------------


def _handle_http_exception(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, HTTPException)
    return _error(exc.detail, exc.status_code)


def _handle_validation_error(request: Request, exc: Exception) -> JSONResponse:
    """Pydantic's message, flattened to something an agent can act on."""
    assert isinstance(exc, ValidationError)
    problems = "; ".join(
        f"{'.'.join(str(p) for p in err['loc']) or 'body'}: {err['msg']}" for err in exc.errors()
    )
    return _error(problems or "invalid request", 422)


def _handle_rail_unavailable(request: Request, exc: Exception) -> JSONResponse:
    return _error(str(exc), 503)


def _handle_lookup_error(request: Request, exc: Exception) -> JSONResponse:
    return _error("no such payment", 404)


ROUTES = [
    Route("/v1", index),
    Route("/v1/tenants", create_tenant, methods=["POST"]),
    Route("/v1/payments", create_payment, methods=["POST"]),
    Route("/v1/payments", list_payments, methods=["GET"]),
    Route("/v1/payments/{payment_id}", get_payment),
    Route("/v1/analytics/summary", analytics_summary),
]


def create_app(
    *,
    db_path: str | None = None,
    engine: Engine | None = None,
    latency_scale: float = 1.0,
) -> Starlette:
    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        built = engine or Engine.build(db_path=db_path, latency_scale=latency_scale)
        replayed = built.boot()
        app.state.engine = built
        app.state.replayed_attempts = replayed
        yield

    return Starlette(
        routes=ROUTES,
        lifespan=lifespan,
        exception_handlers={
            HTTPException: _handle_http_exception,
            ValidationError: _handle_validation_error,
            RailUnavailable: _handle_rail_unavailable,
            LookupError: _handle_lookup_error,
        },
    )
