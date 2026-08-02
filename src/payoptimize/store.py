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
from .models import iso_since, utc_now_iso

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
CREATE TABLE IF NOT EXISTS agent_runs (
  id INTEGER PRIMARY KEY,
  tenant_id INTEGER NOT NULL DEFAULT 0,
  trigger_kind TEXT NOT NULL,
  question TEXT NOT NULL DEFAULT '',
  answer TEXT NOT NULL DEFAULT '',
  tools_used TEXT NOT NULL DEFAULT '',
  model TEXT NOT NULL,
  tokens_in INTEGER NOT NULL DEFAULT 0,
  tokens_out INTEGER NOT NULL DEFAULT 0,
  latency_ms INTEGER NOT NULL DEFAULT 0,
  ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_actions (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES agent_runs(id),
  kind TEXT NOT NULL,
  params TEXT NOT NULL,
  rationale TEXT NOT NULL DEFAULT '',
  detail TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'proposed',
  created_ts TEXT NOT NULL,
  decided_ts TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_payments_created ON payments(created_ts);
CREATE INDEX IF NOT EXISTS idx_agent_runs_ts ON agent_runs(ts);
CREATE INDEX IF NOT EXISTS idx_agent_actions_status ON agent_actions(status);
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


def provider_attempt_summary(*, db_path: str | None = None) -> dict[str, dict[str, Any]]:
    """Lifetime attempts per provider, with the newest timestamp. No window.

    The windowed counts answer "how is this rail doing lately". This answers
    "has this rail ever been used", which is a different question and the one a
    tile needs before it is entitled to say "no traffic yet".
    """
    with transaction(db_path) as conn:
        rows = conn.execute(
            "SELECT provider, COUNT(*) AS attempts, MAX(created_ts) AS last_ts"
            " FROM attempts GROUP BY provider"
        ).fetchall()
    return {
        str(row["provider"]): {
            "attempts": int(row["attempts"]),
            "last_ts": str(row["last_ts"] or ""),
        }
        for row in rows
    }


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


# --- provider events ---------------------------------------------------------


def insert_provider_event(
    provider: str, kind: str, detail: str = "", *, db_path: str | None = None
) -> int:
    """Injections and health transitions. These become the vertical annotations
    on the auth-rate chart, which is what turns "the line moved" into "the line
    moved *because* stripe_sim was degraded at 14:02:31"."""
    with transaction(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO provider_events (provider, kind, detail, ts) VALUES (?, ?, ?, ?)",
            (provider, kind, detail, utc_now_iso()),
        )
        assert cur.lastrowid is not None
        return int(cur.lastrowid)


def recent_provider_events(
    *, since: str, limit: int = 50, db_path: str | None = None
) -> list[dict[str, Any]]:
    with transaction(db_path) as conn:
        return _rows(
            conn.execute(
                "SELECT * FROM provider_events WHERE ts >= ? ORDER BY ts ASC LIMIT ?",
                (since, limit),
            )
        )


# --- time series -------------------------------------------------------------


def auth_rate_series(
    *,
    since: str,
    bucket_seconds: int = 30,
    method: str | None = None,
    db_path: str | None = None,
) -> list[dict[str, Any]]:
    """Resolved payments bucketed by time and routing mode.

    SQLite parses our ISO timestamps natively, so the bucketing is an integer
    division in SQL rather than four thousand rows crossing into Python every
    two seconds.
    """
    clauses = ["created_ts >= ?", "status IN ('succeeded', 'failed')"]
    params: list[Any] = [bucket_seconds, bucket_seconds, since]
    if method is not None:
        clauses.append("method = ?")
        params.append(method)
    where = " AND ".join(clauses)
    with transaction(db_path) as conn:
        return _rows(
            conn.execute(
                "SELECT CAST(unixepoch(created_ts) / ? AS INTEGER) * ? AS bucket_ts,"
                " routing_mode, COUNT(*) AS volume,"
                " SUM(CASE WHEN status = 'succeeded' THEN 1 ELSE 0 END) AS succeeded"
                f" FROM payments WHERE {where}"
                " GROUP BY bucket_ts, routing_mode ORDER BY bucket_ts ASC",
                params,
            )
        )


def provider_mix_series(
    *, since: str, bucket_seconds: int = 30, db_path: str | None = None
) -> list[dict[str, Any]]:
    """Where the router's FIRST attempts went, over time. Only seq=1 counts —
    a cascade's second try is a consequence of the routing decision, not one."""
    with transaction(db_path) as conn:
        return _rows(
            conn.execute(
                "SELECT CAST(unixepoch(a.created_ts) / ? AS INTEGER) * ? AS bucket_ts,"
                " a.provider, COUNT(*) AS attempts"
                " FROM attempts a JOIN payments p ON p.id = a.payment_id"
                " WHERE a.created_ts >= ? AND a.seq = 1 AND p.routing_mode = 'router'"
                " GROUP BY bucket_ts, a.provider ORDER BY bucket_ts ASC",
                (bucket_seconds, bucket_seconds, since),
            )
        )


def recent_payments_with_attempts(
    *, limit: int = 25, method: str | None = None, db_path: str | None = None
) -> list[dict[str, Any]]:
    """The dashboard feed. One query for the payments and one for all of their
    attempts, rather than one per row.

    `method` narrows to a single rail. The feed uses it to pin real-rail rows,
    which generator volume would otherwise bury within seconds.
    """
    where = "WHERE method = ?" if method else ""
    params: list[Any] = ([method] if method else []) + [limit]
    with transaction(db_path) as conn:
        payments = _rows(
            conn.execute(
                f"SELECT * FROM payments {where} ORDER BY created_ts DESC, rowid DESC LIMIT ?",
                params,
            )
        )
        if not payments:
            return []
        placeholders = ",".join("?" * len(payments))
        attempts = _rows(
            conn.execute(
                f"SELECT * FROM attempts WHERE payment_id IN ({placeholders})"
                " ORDER BY payment_id, seq ASC",
                [p["id"] for p in payments],
            )
        )
    chains: dict[str, list[dict[str, Any]]] = {}
    for attempt in attempts:
        chains.setdefault(attempt["payment_id"], []).append(attempt)
    for payment in payments:
        payment["attempts"] = chains.get(payment["id"], [])
    return payments


def tenant_by_email(email: str, *, db_path: str | None = None) -> dict[str, Any] | None:
    with transaction(db_path) as conn:
        return _row(conn.execute("SELECT * FROM tenants WHERE email = ?", (email,)))


def average_ticket_cents(
    *, since: str, routing_mode: str | None = None, db_path: str | None = None
) -> float:
    """Mean authorized amount, for valuing recovered authorizations."""
    clauses = ["created_ts >= ?", "status = 'succeeded'"]
    params: list[Any] = [since]
    if routing_mode is not None:
        clauses.append("routing_mode = ?")
        params.append(routing_mode)
    with transaction(db_path) as conn:
        row = conn.execute(
            f"SELECT AVG(amount_cents) AS avg FROM payments WHERE {' AND '.join(clauses)}",
            params,
        ).fetchone()
    return float(row["avg"] or 0.0)


def pending_prava_attempts(*, db_path: str | None = None) -> list[dict[str, Any]]:
    """Attempts holding a minted Prava session that nobody has settled yet.

    Keyed on the session id being present: an attempt without one is a session
    that was never minted, and polling it would ask Prava about nothing.
    """
    with transaction(db_path) as conn:
        return _rows(
            conn.execute(
                "SELECT a.* FROM attempts a JOIN payments p ON p.id = a.payment_id"
                " WHERE a.provider = 'prava' AND a.status = 'pending'"
                " AND a.prava_session_id != '' AND p.status = 'pending_approval'"
                " ORDER BY a.id ASC"
            )
        )


def tenant_volumes(*, since: str, db_path: str | None = None) -> list[dict[str, Any]]:
    """Per-merchant volume, for the tenant strip. Names only — the dashboard is
    a public page and an email address is not an aggregate."""
    with transaction(db_path) as conn:
        return _rows(
            conn.execute(
                "SELECT t.id, t.name, t.fee_bps, t.fee_fixed_cents,"
                " COUNT(p.id) AS volume,"
                " SUM(CASE WHEN p.status = 'succeeded' THEN 1 ELSE 0 END) AS succeeded,"
                " COALESCE(SUM(CASE WHEN p.status = 'succeeded' THEN p.amount_cents END), 0)"
                " AS authorized_cents"
                " FROM tenants t LEFT JOIN payments p"
                " ON p.tenant_id = t.id AND p.created_ts >= ?"
                " GROUP BY t.id HAVING volume > 0"
                " ORDER BY volume DESC",
                (since,),
            )
        )


# --- ledger ------------------------------------------------------------------


def insert_ledger_once(
    *,
    tenant_id: int,
    payment_id: str,
    kind: str,
    amount_cents: int,
    currency: str,
    db_path: str | None = None,
) -> bool:
    """Write a ledger line unless one already exists for this payment and kind.

    The guard is in the SQL rather than in the caller. Both the cascade and the
    Prava poller settle payments, a restart can replay either, and charging a
    merchant twice for one payment is the worst class of bug this product could
    ship. Returns True if a line was written.
    """
    with transaction(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO ledger (tenant_id, payment_id, kind, amount_cents, currency, ts)"
            " SELECT ?, ?, ?, ?, ?, ?"
            " WHERE NOT EXISTS ("
            "   SELECT 1 FROM ledger WHERE payment_id = ? AND kind = ?"
            " )",
            (
                tenant_id,
                payment_id,
                kind,
                amount_cents,
                currency,
                utc_now_iso(),
                payment_id,
                kind,
            ),
        )
        return cur.rowcount > 0


def ledger_entries(
    *, tenant_id: int, limit: int = 50, db_path: str | None = None
) -> list[dict[str, Any]]:
    with transaction(db_path) as conn:
        return _rows(
            conn.execute(
                "SELECT * FROM ledger WHERE tenant_id = ? ORDER BY id DESC LIMIT ?",
                (tenant_id, limit),
            )
        )


def ledger_totals(*, tenant_id: int, db_path: str | None = None) -> dict[str, Any]:
    with transaction(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS entries, COALESCE(SUM(amount_cents), 0) AS total_cents"
            " FROM ledger WHERE tenant_id = ?",
            (tenant_id,),
        ).fetchone()
    return dict(row)


def ledger_by_tenant(*, since: str, db_path: str | None = None) -> dict[int, int]:
    """Fees metered per tenant in a window — the merchant strip's number."""
    with transaction(db_path) as conn:
        rows = conn.execute(
            "SELECT tenant_id, COALESCE(SUM(amount_cents), 0) AS fees"
            " FROM ledger WHERE ts >= ? GROUP BY tenant_id",
            (since,),
        ).fetchall()
    return {int(row["tenant_id"]): int(row["fees"]) for row in rows}


# --- ops agent ---------------------------------------------------------------
#
# Two tables, both append-mostly audit logs. agent_runs is every conversation
# the resident LLM agent had (redacted text only — the redaction happens before
# storage, so a leaked database cannot leak what the model never saw either).
# agent_actions is every mutation it wanted, with the status transitions that
# say whether a guard, a human, or an error had the last word.


def list_tenants(*, db_path: str | None = None) -> list[dict[str, Any]]:
    """Every tenant row. The redactor builds its pseudonym table from this —
    it has to know every name and email that must never reach the model."""
    with transaction(db_path) as conn:
        return _rows(conn.execute("SELECT * FROM tenants ORDER BY id ASC"))


def stranded_payments(
    *, older_than_seconds: float = 900, db_path: str | None = None
) -> list[dict[str, Any]]:
    """Payments stuck in `pending` with nothing in flight to resolve them.

    A healthy payment leaves `pending` inside its own request; one that is
    still there after fifteen minutes with no pending attempt has been
    abandoned by a crash or a startup failure, and nothing will ever come back
    for it. This is what the agent's reconcile action is allowed to touch —
    the query *is* the guard's definition of stranded.
    """
    cutoff = iso_since(older_than_seconds)
    with transaction(db_path) as conn:
        return _rows(
            conn.execute(
                "SELECT p.* FROM payments p WHERE p.status = 'pending'"
                " AND p.created_ts < ? AND NOT EXISTS ("
                "   SELECT 1 FROM attempts a"
                "   WHERE a.payment_id = p.id AND a.status = 'pending'"
                " ) ORDER BY p.created_ts ASC",
                (cutoff,),
            )
        )


def insert_agent_run(
    *,
    tenant_id: int,
    trigger_kind: str,
    question: str,
    model: str,
    db_path: str | None = None,
) -> int:
    """Open a run before the first model call, so a crash mid-run still leaves
    an audit row saying the agent was invoked."""
    with transaction(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO agent_runs (tenant_id, trigger_kind, question, model, ts)"
            " VALUES (?, ?, ?, ?, ?)",
            (tenant_id, trigger_kind, question, model, utc_now_iso()),
        )
        assert cur.lastrowid is not None
        return int(cur.lastrowid)


def finish_agent_run(
    run_id: int,
    *,
    answer: str,
    tools_used: str,
    tokens_in: int,
    tokens_out: int,
    latency_ms: int,
    db_path: str | None = None,
) -> None:
    with transaction(db_path) as conn:
        cur = conn.execute(
            "UPDATE agent_runs SET answer = ?, tools_used = ?, tokens_in = ?,"
            " tokens_out = ?, latency_ms = ? WHERE id = ?",
            (answer, tools_used, tokens_in, tokens_out, latency_ms, run_id),
        )
        if cur.rowcount == 0:
            raise NotFoundError(f"no agent run {run_id} to finish")


def recent_agent_runs(
    *,
    tenant_id: int | None = None,
    kinds: tuple[str, ...] | None = None,
    limit: int = 20,
    db_path: str | None = None,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if tenant_id is not None:
        clauses.append("tenant_id = ?")
        params.append(tenant_id)
    if kinds:
        clauses.append(f"trigger_kind IN ({','.join('?' * len(kinds))})")
        params.extend(kinds)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    with transaction(db_path) as conn:
        return _rows(
            conn.execute(
                f"SELECT * FROM agent_runs{where} ORDER BY id DESC LIMIT ?",
                params,
            )
        )


def insert_agent_action(
    *,
    run_id: int,
    kind: str,
    params: str,
    rationale: str,
    status: str = "proposed",
    detail: str = "",
    db_path: str | None = None,
) -> int:
    now = utc_now_iso()
    decided = now if status != "proposed" else ""
    with transaction(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO agent_actions (run_id, kind, params, rationale, detail,"
            " status, created_ts, decided_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, kind, params, rationale, detail, status, now, decided),
        )
        assert cur.lastrowid is not None
        return int(cur.lastrowid)


def get_agent_action(action_id: int, *, db_path: str | None = None) -> dict[str, Any] | None:
    with transaction(db_path) as conn:
        return _row(conn.execute("SELECT * FROM agent_actions WHERE id = ?", (action_id,)))


def pending_agent_actions(*, db_path: str | None = None) -> list[dict[str, Any]]:
    with transaction(db_path) as conn:
        return _rows(
            conn.execute("SELECT * FROM agent_actions WHERE status = 'proposed' ORDER BY id ASC")
        )


def recent_agent_actions(*, limit: int = 50, db_path: str | None = None) -> list[dict[str, Any]]:
    with transaction(db_path) as conn:
        return _rows(conn.execute("SELECT * FROM agent_actions ORDER BY id DESC LIMIT ?", (limit,)))


def decide_agent_action(
    action_id: int, *, status: str, detail: str = "", db_path: str | None = None
) -> None:
    """Move a proposal to approved/rejected — exactly once.

    The `status = 'proposed'` clause is the whole exactly-once guarantee: two
    concurrent approvals race on one UPDATE, and the loser's rowcount is 0.
    """
    with transaction(db_path) as conn:
        cur = conn.execute(
            "UPDATE agent_actions SET status = ?, detail = ?, decided_ts = ?"
            " WHERE id = ? AND status = 'proposed'",
            (status, detail, utc_now_iso(), action_id),
        )
        if cur.rowcount == 0:
            raise NotFoundError(
                f"no undecided agent action {action_id} — already decided or unknown"
            )


def mark_agent_action(
    action_id: int, *, status: str, detail: str = "", db_path: str | None = None
) -> None:
    """Record how an approved action's execution went (executed | failed)."""
    with transaction(db_path) as conn:
        cur = conn.execute(
            "UPDATE agent_actions SET status = ?, detail = ?, decided_ts = ? WHERE id = ?",
            (status, detail, utc_now_iso(), action_id),
        )
        if cur.rowcount == 0:
            raise NotFoundError(f"no agent action {action_id} to mark")


def latest_attempt_id(*, db_path: str | None = None) -> int:
    with transaction(db_path) as conn:
        row = conn.execute("SELECT COALESCE(MAX(id), 0) AS n FROM attempts").fetchone()
    return int(row["n"])


def latest_provider_event_id(*, db_path: str | None = None) -> int:
    with transaction(db_path) as conn:
        row = conn.execute("SELECT COALESCE(MAX(id), 0) AS n FROM provider_events").fetchone()
    return int(row["n"])


def resolved_attempts_since(after_id: int, *, db_path: str | None = None) -> list[dict[str, Any]]:
    """Resolved attempts newer than a high-water mark — the trigger watcher's
    feed. By id, not timestamp: ids never collide inside one millisecond."""
    with transaction(db_path) as conn:
        return _rows(
            conn.execute(
                "SELECT * FROM attempts WHERE id > ?"
                " AND status IN ('succeeded', 'failed') ORDER BY id ASC",
                (after_id,),
            )
        )


def provider_events_since(after_id: int, *, db_path: str | None = None) -> list[dict[str, Any]]:
    with transaction(db_path) as conn:
        return _rows(
            conn.execute("SELECT * FROM provider_events WHERE id > ? ORDER BY id ASC", (after_id,))
        )
