#!/usr/bin/env python3
"""Mint one Prava session, print a QR for it, and watch it settle.

Standalone — no server, no database, no tenant. Just the rail, so a rehearsal
or an experiment is one command and the variables are on the command line:

    uv run python scripts/prava_session.py
    uv run python scripts/prava_session.py --merchant-url https://bluebottlecoffee.com \
                                           --merchant-name "Blue Bottle Coffee"
    uv run python scripts/prava_session.py --resume ses_01K…      # poll an existing one

Minting a session does not draw down the sandbox transaction budget — only a
completed transaction does. Verified on the Prava dashboard: orders went 28→29
while "18 remaining" did not move.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import segno

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from payoptimize.providers import prava

DIM, BOLD, GREEN, RED, RESET = "\033[2m", "\033[1m", "\033[32m", "\033[31m", "\033[0m"


def _say(text: str, colour: str = "") -> None:
    print(f"{colour}{text}{RESET}", flush=True)


def mint(args: argparse.Namespace) -> str:
    name, url, country = prava.configured_merchant()
    name = args.merchant_name or name
    url = args.merchant_url or url
    country = args.merchant_country or country

    _say(f"\n{BOLD}minting{RESET}")
    _say(f"   merchant : {name}  {url}  ({country})", DIM)
    _say(f"   amount   : ${args.amount_cents / 100:.2f} {args.currency}", DIM)
    if args.card_id:
        _say(f"   card     : {args.card_id}", DIM)

    session = prava.create_session(
        name,
        url,
        country,
        {
            "name": args.description,
            "price": prava.amount_to_decimal(args.amount_cents),
            "currency": args.currency,
        },
        card_id=args.card_id,
    )
    session_id = str(session["session_id"])
    _say(f"   session  : {session_id}")
    _say(f"   order    : {session.get('order_id', '?')}", DIM)
    return session_id


def approval_url(session_id: str) -> str:
    return f"https://sandbox.collect.prava.space?session={session_id}"


def show_qr(url: str) -> None:
    _say(f"\n{BOLD}scan with the phone holding the enrolled passkey{RESET}")
    _say("   if it says 'session not found', you scanned an older code", DIM)
    _say("   open it in Safari/Chrome, not an in-app browser — passkeys need a real browser", DIM)
    print()
    segno.make(url, error="m").terminal(compact=True, border=2)
    print()
    _say(f"   {url}", DIM)


def watch(session_id: str, timeout_s: float, interval: float = 4.0) -> int:
    _say(f"\n{BOLD}watching{RESET}  (Ctrl-C to stop; the session stays open)")
    deadline = time.monotonic() + timeout_s
    seen = None
    while time.monotonic() < deadline:
        result = prava.payment_result(session_id)
        status = str(result.get("status", ""))
        if status != seen:
            _say(f"   {int(timeout_s - (deadline - time.monotonic())):>4}s  {status}")
            seen = status
        if status in ("completed", "failed"):
            print()
            for txn in result.get("transactions", []):
                error = txn.get("error") or {}
                _say(f"   txn {txn.get('txn_id')}  {txn.get('status')}")
                if error:
                    _say(f"   ERROR {error.get('code')} — {error.get('message')}", RED)
                for item in txn.get("line_items", []):
                    _say(
                        f"   merchant : {item.get('merchant_name')}  {item.get('merchant_url')}",
                        DIM,
                    )
                    _say(f"   token    : {item.get('token')}")
                    _say(f"   cvv      : {item.get('dynamic_cvv')}")
                    _say(f"   txn_ref  : {item.get('txn_ref_id')}", DIM)
            print()
            if status == "completed":
                _say(f"{BOLD}{GREEN}   settled{RESET}")
                _say("   a real credential exists — report_status APPROVED is now owed", DIM)
                return 0
            _say(f"{RED}   failed{RESET}")
            return 1
        time.sleep(interval)
    _say(f"\n{DIM}   still pending after {timeout_s:.0f}s — nobody approved yet.{RESET}")
    _say(f"{DIM}   the session is still open; resume with:{RESET}")
    _say(f"     uv run python scripts/prava_session.py --resume {session_id}", DIM)
    return 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--merchant-name", default="")
    parser.add_argument("--merchant-url", default="")
    parser.add_argument("--merchant-country", default="")
    parser.add_argument("--card-id", default="", help="pin a card; blank uses the account default")
    parser.add_argument("--amount-cents", type=int, default=1250)
    parser.add_argument("--currency", default="USD")
    parser.add_argument("--description", default="Pro plan")
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--resume", default="", help="poll an existing session instead of minting")
    parser.add_argument("--cards", action="store_true", help="list enrolled cards and exit")
    args = parser.parse_args()

    try:
        if args.cards:
            for card in prava.list_cards():
                print(json.dumps(card))
            return 0
        session_id = args.resume or mint(args)
        show_qr(approval_url(session_id))
        return watch(session_id, args.timeout)
    except prava.PravaError as exc:
        _say(f"\nPrava refused: {exc}", RED)
        return 2
    except KeyboardInterrupt:
        _say("\ninterrupted — the session stays open.", DIM)
        return 130


if __name__ == "__main__":
    sys.exit(main())
