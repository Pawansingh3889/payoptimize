"""Command line: serve, sign up a tenant, send a payment.

`serve` is the one that matters — it is what runs on Fly and what a judge sees.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from . import config, tenancy
from .engine import Engine
from .models import PaymentRequest


def _bind_host() -> str:
    """0.0.0.0 where the port would otherwise be unreachable, loopback elsewhere.

    Binding a laptop's dev server to every interface is how a demo ends up
    reachable from the conference wifi, so this is detected rather than assumed.

    The file checks alone are not enough, which the first Fly deploy proved:
    a Fly machine is a Firecracker microVM, not a container, and has neither
    /.dockerenv nor /run/.containerenv. It bound loopback and fly-proxy could
    not reach it — "the app is not listening on the expected address". Hence the
    FLY_* check, and hence PAYOPTIMIZE_HOST for the next platform whose shape I
    have guessed wrong.
    """
    explicit = os.environ.get("PAYOPTIMIZE_HOST", "").strip()
    if explicit:
        return explicit
    in_container = Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()
    on_fly = bool(os.environ.get("FLY_APP_NAME") or os.environ.get("FLY_MACHINE_ID"))
    return "0.0.0.0" if in_container or on_fly else "127.0.0.1"


def serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .api import create_app

    # Fail before binding a port rather than on the first /admin call mid-demo.
    config.admin_token()

    host = args.host or _bind_host()
    port = args.port or config.port()
    tps = args.tps or config.generator_tps()
    generator = "off" if args.no_generator else f"{tps} tx/s"
    print(
        f"payoptimize → http://{host}:{port}  (db: {config.db_path()}, generator: {generator})",
        file=sys.stderr,
    )
    app = create_app(with_generator=not args.no_generator, generator_tps=tps)
    # One worker, always: the bandit's posteriors live in this process's memory.
    uvicorn.run(app, host=host, port=port, workers=1, log_level=args.log_level)
    return 0


def signup(args: argparse.Namespace) -> int:
    tenant_id, api_key = tenancy.signup(args.name, args.email)
    print(json.dumps({"tenant_id": tenant_id, "api_key": api_key}, indent=2))
    print("\nThis key is shown once — only its hash is stored.", file=sys.stderr)
    return 0


def demo_payment(args: argparse.Namespace) -> int:
    """Run one payment through the real engine, in-process. Useful for checking
    a deployment's routing without standing up a client."""
    engine = Engine.build(latency_scale=0 if args.fast else 1.0)
    engine.boot()
    request = PaymentRequest(
        amount_cents=args.amount_cents,
        currency=args.currency,
        country=args.country,
        card_brand=args.card_brand,
        description=args.description,
    )
    response = asyncio.run(engine.execute(request, tenant_id=args.tenant_id))
    print(json.dumps(response.model_dump(), indent=2))
    return 0 if response.status == "succeeded" else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="payoptimize", description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)

    p_serve = subs.add_parser("serve", help="run the dashboard and API")
    p_serve.add_argument("--host", default="")
    p_serve.add_argument("--port", type=int, default=0)
    p_serve.add_argument("--log-level", default="info")
    p_serve.add_argument("--tps", type=float, default=0.0, help="generator rate")
    p_serve.add_argument(
        "--no-generator", action="store_true", help="serve without synthetic traffic"
    )
    p_serve.set_defaults(func=serve)

    p_signup = subs.add_parser("signup", help="create a tenant and mint an API key")
    p_signup.add_argument("name")
    p_signup.add_argument("email")
    p_signup.set_defaults(func=signup)

    p_pay = subs.add_parser("demo-payment", help="run one payment through the engine")
    p_pay.add_argument("--tenant-id", type=int, default=1)
    p_pay.add_argument("--amount-cents", type=int, default=1250)
    p_pay.add_argument("--currency", default="USD")
    p_pay.add_argument("--country", default="US")
    p_pay.add_argument("--card-brand", default="visa")
    p_pay.add_argument("--description", default="CLI demo payment")
    p_pay.add_argument("--fast", action="store_true", help="skip simulated latency")
    p_pay.set_defaults(func=demo_payment)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (config.ConfigError, tenancy.AuthError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
