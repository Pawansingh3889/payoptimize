#!/usr/bin/env python3
"""An agent buying something with a real Prava payment, scripted.

Same public API the MCP server calls, driven from a terminal: create the
payment, print the approval URL as a QR code you can photograph off the screen,
wait for the human to tap their passkey, print how it settled.

Two jobs. It is the rehearsal harness for the live-transaction beat of the demo,
and it is the fallback if the MCP client misbehaves on stage — the flow is
identical, so what it proves about the rail is the same thing.

    uv run python scripts/agent_demo.py --url https://payoptimize.fly.dev --key pok_…

Every run spends one Prava sandbox transaction. The budget is finite; see
docs/PLAN.md §11.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import segno

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from payoptimize.sdk import PayOptimizeClient, PayOptimizeError

DIM = "\033[2m"
BOLD = "\033[1m"
GREEN = "\033[32m"
RED = "\033[31m"
RESET = "\033[0m"


def _say(message: str, colour: str = "") -> None:
    print(f"{colour}{message}{RESET}", flush=True)


def _qr(url: str) -> None:
    """A QR on the terminal beats reading a session URL aloud, and it is what
    lets the phone that owns the passkey reach the approval page in one move."""
    segno.make(url, error="m").terminal(compact=True)


def run(url: str, key: str, amount_cents: int, description: str, timeout_s: float) -> int:
    client = PayOptimizeClient(url, key)

    _say(f"\n{BOLD}1. Agent creates a payment{RESET}")
    _say(f"   {description} — ${amount_cents / 100:.2f} via the REAL Prava sandbox rail", DIM)
    payment = client.create_payment(amount_cents, "USD", method="prava", description=description)
    payment_id = payment["id"]
    approval_url = payment.get("iframe_url", "")
    _say(f"   → {payment_id}  status={payment['status']}")

    if payment["status"] != "pending_approval" or not approval_url:
        _say(f"   unexpected: {payment}", RED)
        return 1

    _say(f"\n{BOLD}2. A human has to approve this{RESET}")
    _say("   Scan with the phone holding the enrolled passkey:", DIM)
    print()
    _qr(approval_url)
    print()
    _say(f"   {approval_url}", DIM)

    _say(f"\n{BOLD}3. Waiting for the passkey tap…{RESET}")
    started = time.monotonic()
    settled = client.wait_for_payment(payment_id, timeout_s=timeout_s)
    elapsed = time.monotonic() - started

    print()
    if settled["status"] == "succeeded":
        _say(f"{BOLD}{GREEN}   ✓ settled in {elapsed:.0f}s{RESET}")
        _say(f"   provider: {settled['provider']}  (REAL · Prava sandbox)")
        for attempt in settled["attempts"]:
            _say(f"   txn: {attempt['prava_txn_id'] or '—'}", DIM)
        _say("   report-status APPROVED was sent — the session is settled, not hanging.", DIM)
        return 0

    if settled["status"] == "pending_approval":
        _say(f"{RED}   still waiting after {elapsed:.0f}s — nobody approved it.{RESET}")
        return 2

    _say(f"{RED}   ✗ {settled['status']}: {settled.get('decline_code') or 'no code'}{RESET}")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("PAYOPTIMIZE_API_URL", ""))
    parser.add_argument("--key", default=os.environ.get("PAYOPTIMIZE_API_KEY", ""))
    parser.add_argument("--amount-cents", type=int, default=1250)
    parser.add_argument("--description", default="Pro plan")
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()

    if not args.url or not args.key:
        parser.error("--url and --key are required (or set PAYOPTIMIZE_API_URL/_API_KEY)")

    try:
        return run(args.url, args.key, args.amount_cents, args.description, args.timeout)
    except PayOptimizeError as exc:
        _say(f"\nAPI error ({exc.status_code}): {exc}", RED)
        return 2
    except KeyboardInterrupt:
        _say("\ninterrupted — the payment may still settle on its own.", DIM)
        return 130


if __name__ == "__main__":
    sys.exit(main())
