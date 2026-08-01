"""Every SQL statement in PayOptimize.

No other module imports sqlite3 (CLAUDE.md layering rule): the router stays
pure, the adapters stay ignorant of persistence, and the schema has exactly one
owner. Rows leave as plain dicts, so no sqlite3 object escapes this file either.

Concurrency model: one process, one uvicorn worker, WAL. Each call opens and
closes its own connection — cheap under SQLite, and it keeps the async app from
ever sharing a connection across threads.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from . import config
from .models import utc_now_iso

# Matches the tenants table defaults; passed explicitly so the fee a tenant was
# created with is visible in the code that creates it.
DEFAULT_FEE_BPS = 45
DEFAULT_FEE_FIXED_CENTS = 5

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  email TEXT NOT NULL,
  created_ts TEXT NOT NULL,
  fee_bps INTEGER NOT NULL DEFAULT 45,
  fee_fixed_cents INTEGER NOT NULL DEFAULT 5
);
CREATE TABLE IF NOT EXISTS api_keys (
  id INTEGER PRIMARY KEY,
  tenant_id INTEGER NOT NULL REFERENCES tenants(id),
  key_hash TEXT NOT NULL UNIQUE,
  display_prefix TEXT NOT NULL,
  created_ts TEXT NOT NULL,
  revoked_ts TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS payments (
  id TEXT PRIMARY KEY,
  tenant_id INTEGER NOT NULL REFERENCES tenants(id),
  amount_cents INTEGER NOT NULL,
  currency TEXT NOT NULL, country TEXT NOT NULL, card_brand TEXT NOT NULL,
  method TEXT NOT NULL DEFAULT 'card',
  routing_mode TEXT NOT NULL,
  segment TEXT NOT NULL,
  status TEXT NOT NULL,
  final_provider TEXT NOT NULL DEFAULT '',
  decline_code TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT 'api',
  description TEXT NOT NULL DEFAULT '',
  created_ts TEXT NOT NULL, resolved_ts TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS attempts (
  id INTEGER PRIMARY KEY,
  payment_id TEXT NOT NULL REFERENCES payments(id),
  seq INTEGER NOT NULL,
  provider TEXT NOT NULL,
  segment TEXT NOT NULL,
  status TEXT NOT NULL,
  decline_code TEXT NOT NULL DEFAULT '',
  latency_ms INTEGER NOT NULL DEFAULT 0,
  prava_session_id TEXT NOT NULL DEFAULT '',
  prava_txn_id TEXT NOT NULL DEFAULT '',
  iframe_url TEXT NOT NULL DEFAULT '',
  created_ts TEXT NOT NULL, resolved_ts TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS provider_events (
  id INTEGER PRIMARY KEY,
  provider TEXT NOT NULL, kind TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '', ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger (
  id INTEGER PRIMARY KEY,
  tenant_id INTEGER NOT NULL REFERENCES tenants(id),
  payment_id TEXT NOT NULL REFERENCES payments(id),
  kind TEXT NOT NULL DEFAULT 'txn_fee',
  amount_cents INTEGER NOT NULL, currency TEXT NOT NULL, ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_payments_created ON payments(created_ts);
CREATE INDEX IF NOT EXISTS idx_payments_tenant ON payments(tenant_id, created_ts);
CREATE INDEX IF NOT EXISTS idx_attempts_payment ON attempts(payment_id);
CREATE INDEX IF NOT EXISTS idx_attempts_provider_ts ON attempts(provider, created_ts);
CREATE INDEX IF NOT EXISTS idx_ledger_tenant ON ledger(tenant_id);
"""

_initialized: set[str] = set()
_init_lock = threading.Lock()


class StoreError(RuntimeError):
    """A write did not land on the row it was aimed at."""


class NotFoundError(StoreError):
    """The row a write targeted does not exist."""


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")


def _ensure_schema(conn: sqlite3.Connection, path: str) -> None:
    """Run the schema once per DB path per process."""
    if path in _initialized:
        return
    with _init_lock:
        if path in _initialized:
            return
        conn.executescript(SCHEMA)
        conn.commit()
        # ":memory:" is a brand-new database on every connection, so caching it
        # as initialized would hand the next caller an empty schema.
        if path != ":memory:":
            _initialized.add(path)


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or config.db_path()
    conn = sqlite3.connect(path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    _apply_pragmas(conn)
    _ensure_schema(conn, path)
    return conn


@contextmanager
def transaction(db_path: str | None = None) -> Iterator[sqlite3.Connection]:
    """Commit on clean exit, roll back on anything else, always close."""
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: str | None = None) -> None:
    """Create the schema if it is not there. Called once at boot."""
    connect(db_path).close()


def _row(cursor: sqlite3.Cursor) -> dict[str, Any] | None:
    row = cursor.fetchone()
    return dict(row) if row is not None else None


def _rows(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    return [dict(row) for row in cursor.fetchall()]


# --- tenants + api keys ------------------------------------------------------


def create_tenant_with_key(
    name: str,
    email: str,
    key_hash: str,
    display_prefix: str,
    *,
    fee_bps: int = DEFAULT_FEE_BPS,
    fee_fixed_cents: int = DEFAULT_FEE_FIXED_CENTS,
    db_path: str | None = None,
) -> int:
    """Signup is one transaction: a tenant with no key could never call the API,
    and a key with no tenant would resolve to nothing."""
    now = utc_now_iso()
    with transaction(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO tenants (name, email, created_ts, fee_bps, fee_fixed_cents)"
            " VALUES (?, ?, ?, ?, ?)",
            (name, email, now, fee_bps, fee_fixed_cents),
        )
        tenant_id = cur.lastrowid
        assert tenant_id is not None  # INSERT always sets lastrowid
        conn.execute(
            "INSERT INTO api_keys (tenant_id, key_hash, display_prefix, created_ts)"
            " VALUES (?, ?, ?, ?)",
            (tenant_id, key_hash, display_prefix, now),
        )
        return int(tenant_id)


def get_tenant(tenant_id: int, *, db_path: str | None = None) -> dict[str, Any] | None:
    with transaction(db_path) as conn:
        return _row(conn.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)))


def tenant_for_key_hash(key_hash: str, *, db_path: str | None = None) -> dict[str, Any] | None:
    """The auth lookup: tenant + key display, active keys only, one round-trip."""
    with transaction(db_path) as conn:
        return _row(
            conn.execute(
                "SELECT t.*, k.display_prefix AS key_prefix, k.id AS key_id"
                " FROM api_keys k JOIN tenants t ON t.id = k.tenant_id"
                " WHERE k.key_hash = ? AND k.revoked_ts = ''",
                (key_hash,),
            )
        )


def revoke_api_key_by_hash(key_hash: str, *, db_path: str | None = None) -> bool:
    """True if this call revoked a live key; False if it was unknown or already
    revoked."""
    with transaction(db_path) as conn:
        cur = conn.execute(
            "UPDATE api_keys SET revoked_ts = ? WHERE key_hash = ? AND revoked_ts = ''",
            (utc_now_iso(), key_hash),
        )
        return cur.rowcount > 0


# --- payments ----------------------------------------------------------------


def insert_payment(
    *,
    payment_id: str,
    tenant_id: int,
    amount_cents: int,
    currency: str,
    country: str,
    card_brand: str,
    method: str,
    routing_mode: str,
    segment: str,
    status: str,
    source: str,
    description: str = "",
    db_path: str | None = None,
) -> dict[str, Any]:
    with transaction(db_path) as conn:
        conn.execute(
            "INSERT INTO payments (id, tenant_id, amount_cents, currency, country,"
            " card_brand, method, routing_mode, segment, status, source, description,"
            " created_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                payment_id,
                tenant_id,
                amount_cents,
                currency,
                country,
                card_brand,
                method,
                routing_mode,
                segment,
                status,
                source,
                description,
                utc_now_iso(),
            ),
        )
        row = _row(conn.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)))
        assert row is not None  # just inserted, same transaction
        return row


def get_payment(payment_id: str, *, db_path: str | None = None) -> dict[str, Any] | None:
    with transaction(db_path) as conn:
        return _row(conn.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)))


def list_payments(
    *,
    tenant_id: int | None = None,
    status: str | None = None,
    limit: int = 50,
    db_path: str | None = None,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM payments"
    clauses: list[str] = []
    params: list[Any] = []
    if tenant_id is not None:
        clauses.append("tenant_id = ?")
        params.append(tenant_id)
    if status:
        clauses.append("status = ?")
        params.append(status)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    # rowid breaks ties inside one millisecond, so the feed never reorders.
    sql += " ORDER BY created_ts DESC, rowid DESC LIMIT ?"
    params.append(limit)
    with transaction(db_path) as conn:
        return _rows(conn.execute(sql, params))


def finalize_payment(
    payment_id: str,
    *,
    status: str,
    final_provider: str = "",
    decline_code: str = "",
    db_path: str | None = None,
) -> None:
    with transaction(db_path) as conn:
        cur = conn.execute(
            "UPDATE payments SET status = ?, final_provider = ?, decline_code = ?,"
            " resolved_ts = ? WHERE id = ?",
            (status, final_provider, decline_code, utc_now_iso(), payment_id),
        )
        if cur.rowcount == 0:
            raise NotFoundError(f"no payment {payment_id!r} to finalize")


def set_payment_status(payment_id: str, status: str, *, db_path: str | None = None) -> None:
    """Move a payment without resolving it — pending → pending_approval."""
    with transaction(db_path) as conn:
        cur = conn.execute("UPDATE payments SET status = ? WHERE id = ?", (status, payment_id))
        if cur.rowcount == 0:
            raise NotFoundError(f"no payment {payment_id!r} to update")


# --- attempts ----------------------------------------------------------------


def insert_attempt(
    *,
    payment_id: str,
    seq: int,
    provider: str,
    segment: str,
    status: str,
    prava_session_id: str = "",
    iframe_url: str = "",
    db_path: str | None = None,
) -> int:
    with transaction(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO attempts (payment_id, seq, provider, segment, status,"
            " prava_session_id, iframe_url, created_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                payment_id,
                seq,
                provider,
                segment,
                status,
                prava_session_id,
                iframe_url,
                utc_now_iso(),
            ),
        )
        attempt_id = cur.lastrowid
        assert attempt_id is not None  # INSERT always sets lastrowid
        return int(attempt_id)


def resolve_attempt(
    attempt_id: int,
    *,
    status: str,
    decline_code: str = "",
    latency_ms: int = 0,
    prava_txn_id: str = "",
    db_path: str | None = None,
) -> None:
    with transaction(db_path) as conn:
        cur = conn.execute(
            "UPDATE attempts SET status = ?, decline_code = ?, latency_ms = ?,"
            " prava_txn_id = ?, resolved_ts = ? WHERE id = ?",
            (status, decline_code, latency_ms, prava_txn_id, utc_now_iso(), attempt_id),
        )
        if cur.rowcount == 0:
            raise NotFoundError(f"no attempt {attempt_id} to resolve")


def attach_prava_session(
    attempt_id: int,
    *,
    session_id: str,
    iframe_url: str,
    db_path: str | None = None,
) -> None:
    """Record the minted session on a pending attempt. The poller finds work by
    this column, so an attempt without it is a session nobody will ever settle."""
    with transaction(db_path) as conn:
        cur = conn.execute(
            "UPDATE attempts SET prava_session_id = ?, iframe_url = ? WHERE id = ?",
            (session_id, iframe_url, attempt_id),
        )
        if cur.rowcount == 0:
            raise NotFoundError(f"no attempt {attempt_id} to attach a prava session to")


def attempts_for_payment(payment_id: str, *, db_path: str | None = None) -> list[dict[str, Any]]:
    with transaction(db_path) as conn:
        return _rows(
            conn.execute(
                "SELECT * FROM attempts WHERE payment_id = ? ORDER BY seq ASC", (payment_id,)
            )
        )


def recent_resolved_attempts(
    *, limit: int = 2_000, db_path: str | None = None
) -> list[dict[str, Any]]:
    """The last `limit` resolved attempts, oldest first — the router's boot
    rebuild replays these through the same decay math it uses live, so the
    order has to be chronological. The inner query takes the newest rows; the
    outer one puts them back in the order they happened.
    """
    with transaction(db_path) as conn:
        return _rows(
            conn.execute(
                "SELECT * FROM (SELECT id, provider, segment, status, decline_code, created_ts"
                " FROM attempts WHERE status IN ('succeeded', 'failed')"
                " ORDER BY id DESC LIMIT ?) ORDER BY id ASC",
                (limit,),
            )
        )


# --- analytics ---------------------------------------------------------------
#
# Every window query filters on created_ts, which is why timestamps are fixed
# precision UTC strings: a text comparison is a chronological comparison, and
# idx_payments_created makes it an index scan.


def _window_clause(since: str, tenant_id: int | None, method: str | None) -> tuple[str, list[Any]]:
    clauses = ["created_ts >= ?", "status IN ('succeeded', 'failed')"]
    params: list[Any] = [since]
    if tenant_id is not None:
        clauses.append("tenant_id = ?")
        params.append(tenant_id)
    if method is not None:
        clauses.append("method = ?")
        params.append(method)
    return " AND ".join(clauses), params


def payments_by_mode(
    *,
    since: str,
    tenant_id: int | None = None,
    method: str | None = None,
    db_path: str | None = None,
) -> list[dict[str, Any]]:
    """Volume and successes per routing mode — the uplift measurement itself."""
    where, params = _window_clause(since, tenant_id, method)
    with transaction(db_path) as conn:
        return _rows(
            conn.execute(
                "SELECT routing_mode, COUNT(*) AS volume,"
                " SUM(CASE WHEN status = 'succeeded' THEN 1 ELSE 0 END) AS succeeded"
                f" FROM payments WHERE {where} GROUP BY routing_mode",
                params,
            )
        )


def payments_by_corridor(
    *,
    since: str,
    tenant_id: int | None = None,
    method: str | None = None,
    db_path: str | None = None,
) -> list[dict[str, Any]]:
    where, params = _window_clause(since, tenant_id, method)
    with transaction(db_path) as conn:
        return _rows(
            conn.execute(
                "SELECT segment, routing_mode, COUNT(*) AS volume,"
                " SUM(CASE WHEN status = 'succeeded' THEN 1 ELSE 0 END) AS succeeded"
                f" FROM payments WHERE {where} GROUP BY segment, routing_mode"
                " ORDER BY segment ASC",
                params,
            )
        )


def attempts_by_provider(*, since: str, db_path: str | None = None) -> list[dict[str, Any]]:
    """Per-rail attempt counts and latency. Attempts, not payments: a cascade's
    second try is the only place a rail's real behaviour is visible."""
    with transaction(db_path) as conn:
        return _rows(
            conn.execute(
                "SELECT provider, COUNT(*) AS attempts,"
                " SUM(CASE WHEN status = 'succeeded' THEN 1 ELSE 0 END) AS succeeded,"
                " AVG(latency_ms) AS avg_latency_ms"
                " FROM attempts WHERE created_ts >= ? AND status IN ('succeeded', 'failed')"
                " GROUP BY provider ORDER BY provider ASC",
                (since,),
            )
        )


def attempt_latencies(*, provider: str, since: str, db_path: str | None = None) -> list[int]:
    """Raw latencies for a percentile that means something — AVG hides the tail
    that a degrading rail shows up in first."""
    with transaction(db_path) as conn:
        rows = conn.execute(
            "SELECT latency_ms FROM attempts WHERE provider = ? AND created_ts >= ?"
            " AND status IN ('succeeded', 'failed') ORDER BY latency_ms ASC",
            (provider, since),
        ).fetchall()
    return [int(row["latency_ms"]) for row in rows]


def payment_count(*, since: str, db_path: str | None = None) -> int:
    with transaction(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM payments WHERE created_ts >= ?", (since,)
        ).fetchone()
    return int(row["n"])
