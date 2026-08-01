# CLAUDE.md — payoptimize

PayOptimize AI: payment orchestration that routes each payment to the provider most
likely to authorize it (discounted Thompson sampling + decline-aware cascade), with
Prava sandbox as the one REAL rail and three clearly-labeled simulated rails.
Hackathon build (Devfolio × Prava, deadline 2026-08-02 ~19:00 PT). **The build order,
schema, router math, API table, and demo script live in `docs/PLAN.md` — read it
before writing any code.**

## Architecture / layering rules

- `store.py` owns ALL SQL. Nothing else touches sqlite3. Connection rules: WAL +
  `busy_timeout=5000` + `synchronous=NORMAL` + `foreign_keys=ON`, schema-init-once per
  DB path (module-level set + Lock).
- `providers/*` implement the `ProviderAdapter` protocol from `providers/base.py` and
  return `AttemptOutcome`. The router never knows which provider is real.
- `router.py` is pure decision logic: no HTTP, no SQL. Posteriors live in memory,
  rebuilt on boot from the last 2,000 `attempts` rows. All randomness through ONE
  injected `random.Random` (seedable via `PAYOPTIMIZE_SEED`).
- Prava payments (`method: "prava"`) bypass the bandit entirely — human-approval flow,
  settled by the lifespan poller. The card bandit routes simulated rails only.
- `api.py` is the single Starlette app (dashboard + REST + admin). MCP (`server.py`)
  and the SDK are thin clients over the REST API — one implementation, three surfaces.
- **Single process, single uvicorn worker — invariant.** In-memory bandit state and
  the lifespan tasks (generator, Prava poller) depend on it.

## Critical interfaces

- Decline taxonomy (`providers/base.py`) drives the cascade: infrastructure codes
  (`issuer_unavailable`, `provider_timeout`, `processing_error`, `rate_limited`)
  cascade; `do_not_honor` and terminal/fraud codes never do. Max 3 attempts, never the
  same provider twice.
- Money is integer cents everywhere; convert to Prava's decimal-string format only at
  the adapter boundary.
- Every HTTP-touching function takes `http: httpx.Client | None = None` for test
  injection; the SDK takes Starlette's TestClient the same way.
- After receiving a Prava credential, `report_status(..., "APPROVED")` must be called
  immediately — an unreported session hangs in `awaiting_result` forever.

## Dev commands

```bash
uv sync                                  # deps (uv, never pip/poetry)
uv run pytest                            # must be green before every commit
uv run ruff check && uv run ruff format  # lint + format (ruff, not black)
uv run python -m payoptimize serve       # dashboard + API on :8080
```

## What NOT to do

- **Never call the real Prava API from tests or CI.** conftest pins
  `PRAVA_API_BASE=https://prava.invalid`; use `httpx.MockTransport` via the `http=`
  seam. The sandbox transaction budget is finite — live mints are for scripted
  rehearsals only (≤5 total, tracked in docs/PLAN.md risk register).
- Never run more than one uvicorn worker or add a second process writing the DB.
- Never put fees in the bandit reward (v1 decision — fees are UI/ledger only).
- Never present simulated providers as real: every surface labels sims SIMULATED and
  Prava REAL · sandbox.
- Never commit `.env`, a sqlite file, or anything under `data/`.
- No AI attribution in commits/PRs (no Co-Authored-By, no "Generated with").
  Author/committer: `Pawansingh3889 <pawansinghkapkoti@gmail.com>`.
- Don't edit `providers/prava.py` core functions casually — they're ported from
  canibuy where they were verified against the live sandbox (disclosed in README);
  behavior quirks (cache-buster, no-store, fail-loud identity) are load-bearing.
