"""What PayOptimize charges, and the ledger that records it.

Pricing is a fixed fee plus basis points on each authorized payment, per tenant.
Nothing is charged on a decline: a payment we failed to get through is not a
service we performed, and billing for it would invert the product's whole claim.

The README frames this as taking a slice of the declines we recover. That is the
business story; this is its mechanism. The two are reconciled on the dashboard,
which shows fees metered next to revenue recovered so the ratio is visible
rather than asserted.

Money stays integer cents throughout. The rounding rule is stated once, here,
and every fee in the system goes through this one function.
"""

from __future__ import annotations

from typing import Any

from . import store

TXN_FEE = "txn_fee"


def fee_for(amount_cents: int, *, fee_bps: int, fee_fixed_cents: int) -> int:
    """Fixed + basis points, rounded to the nearest cent.

    Banker's rounding is deliberately avoided: `round()` in Python rounds half
    to even, so a half-cent on a 45bps fee would land differently depending on
    whether the result happened to be odd. Merchants reconcile these against
    their own arithmetic, and "sometimes down, sometimes up" is a support ticket.
    Half always goes up, which is the convention every processor's statement
    uses.
    """
    if amount_cents < 0:
        raise ValueError(f"cannot charge a fee on a negative amount: {amount_cents}")
    variable = (amount_cents * fee_bps + 5_000) // 10_000
    return int(variable) + fee_fixed_cents


def record_fee(payment_id: str, *, db_path: str | None = None) -> int | None:
    """Meter one authorized payment. Returns the fee, or None if nothing was
    charged.

    Safe to call twice. Charging a merchant twice for one payment is the worst
    class of bug a payments product can ship, so the guard lives in the SQL
    rather than in the caller's discipline — the poller, the engine, and any
    future retry all reach this same path.
    """
    payment = store.get_payment(payment_id, db_path=db_path)
    if payment is None or payment["status"] != "succeeded":
        return None  # declines are not billable

    tenant = store.get_tenant(int(payment["tenant_id"]), db_path=db_path)
    if tenant is None:
        return None

    fee = fee_for(
        int(payment["amount_cents"]),
        fee_bps=int(tenant["fee_bps"]),
        fee_fixed_cents=int(tenant["fee_fixed_cents"]),
    )
    written = store.insert_ledger_once(
        tenant_id=int(payment["tenant_id"]),
        payment_id=payment_id,
        kind=TXN_FEE,
        amount_cents=fee,
        currency=str(payment["currency"]),
        db_path=db_path,
    )
    return fee if written else None


def statement(tenant_id: int, *, limit: int = 50, db_path: str | None = None) -> dict[str, Any]:
    """What GET /v1/ledger returns: the recent lines and the running total.

    The total counts every line, not just the ones on this page — a statement
    whose total only reflects the visible rows is worse than no total.
    """
    entries = store.ledger_entries(tenant_id=tenant_id, limit=limit, db_path=db_path)
    totals = store.ledger_totals(tenant_id=tenant_id, db_path=db_path)
    return {
        "entries": [
            {
                "payment_id": entry["payment_id"],
                "kind": entry["kind"],
                "amount_cents": int(entry["amount_cents"]),
                "currency": entry["currency"],
                "ts": entry["ts"],
            }
            for entry in entries
        ],
        "total_fees_cents": int(totals["total_cents"]),
        "billed_payments": int(totals["entries"]),
    }
