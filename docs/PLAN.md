# PayOptimize AI — Implementation Plan

**Frame:** Agentic Commerce Hackathon (Devfolio × Prava). Window closes **2026-08-02
~19:00 PT**. Solo dev. ~24 working hours budgeted + buffer. Deploy happens at hour
~12.5, not last. The MVP line is explicit (end of Phase 8). Judging criteria: product
works end-to-end · Prava is a real part of the product · an agent completes/enables a
transaction · could become a real product. Explicitly rejected by the organizers:
"slide decks or demos built only for judging."

**Repo conventions:** uv + ruff + pytest · conventional commits · author/committer
`Pawansingh3889 <pawansinghkapkoti@gmail.com>`, **no AI attribution anywhere** · first
commit on `main`, all work on `feat/*`, merge to main only when
`uv run ruff check && uv run pytest` is green · `.env` gitignored, `.env.example` and
`uv.lock` committed · fail loudly: typed errors, no silent defaults for required config.

---

## 1. Locked architecture decisions

1. **One real rail + simulated rails.** Prava sandbox is the LIVE provider (real
   session mint / payment-result poll / report-status settle). Stripe/Braintree/Adyen
   are SIMULATED adapters with realistic profiles, clearly labeled in every surface.
2. **Prava does NOT sit inside the card bandit.** Its flow needs a human passkey
   approval and the sandbox transaction budget is finite. Prava is a first-class
   provider selected by `method: "prava"` (agent/API-initiated payments only); the
   bandit routes `method: "card"` across the three simulated rails. Prava outcomes
   still feed its health tile and appear (badged REAL) in the feed.
3. **The AI is a real online learner — discounted Thompson sampling.** Plain Beta
   posteriors will NOT visibly react to an outage on demo timescales (after ~2,000
   successes, Beta(1800,200) needs hundreds of failures to move). So: discount **every
   arm in the segment** by γ=0.99 per outcome, rewarding the played arm only, plus a
   **2% forced-exploration probe**.
   **Revised at implementation, on measurement — see §4.** This originally specified
   decaying the *played arm only*. Built that way, a starved arm's counts freeze at
   their beat-down state and nothing pulls them back, so recovery rests entirely on the
   probe: over 5 seeds a recovered rail was rediscovered in **1 of 5 runs** within 4,000
   decisions, which breaks the closing beat of §13. Discounting every arm lets a starved
   arm drift back to Beta(1,1) — uncertainty rather than a verdict — and Thompson
   sampling reaches for it unprompted.
4. **Uplift is measured, not claimed:** 50/50 deterministic A/B split on generator
   traffic; baseline = round-robin with the *same* cascade rules, so the only delta is
   routing intelligence.
5. **The headline demo lever is *degradation*, not hard outage.** A hard outage emits
   retriable codes, so the baseline cascades its way out and the auth-rate gap looks
   small. Degradation (auth → 45% via `do_not_honor`, which we classify non-retriable —
   card networks discourage immediate DNH retries) produces an honest ~8–10 pt gap.
   Hard outage is the secondary beat (latency + mix shift).
6. **Single process, single uvicorn worker — REQUIRED.** Bandit posteriors live in
   memory; the DB is the outcome log and analytics source of truth. Never scale
   workers.
7. **Stack:** Python 3.12, uv, Starlette + uvicorn, SQLite (WAL from day 0), httpx,
   pydantic v2, mcp, segno. **No Playwright, no browsers** — small image, fast deploy.

---

## 2. Repo layout

```
src/payoptimize/
├── config.py        # _load_env (ported idiom), typed getters, fail-loud required vars, admin token
├── models.py        # pydantic v2: PaymentRequest/Response, enums (PaymentStatus, DeclineCode, RoutingMode), segment_key()
├── store.py         # sqlite: WAL + busy_timeout + synchronous=NORMAL, schema-init-once (module set + Lock), ALL queries
├── tenancy.py       # signup, pok_ key mint, sha256-at-rest, bearer auth resolve, revocation
├── providers/
│   ├── __init__.py  # PROVIDERS registry: name → adapter
│   ├── base.py      # ProviderAdapter protocol; AttemptOutcome dataclass; RETRIABLE/TERMINAL decline taxonomy
│   ├── simulated.py # stripe_sim/braintree_sim/adyen_sim: corridor profiles, latency dists, decline mixes, injectable outage/degradation, seeded RNG
│   └── prava.py     # canibuy prava.py ported near-verbatim (env renames) + PravaProvider adapter + settle logic
├── router.py        # discounted Thompson sampling, 2% explore, cascade retry, baseline mode, boot rebuild from attempts
├── health.py        # rolling-window per-provider health (auth rate, p95 latency, state), provider_events log
├── generator.py     # asyncio traffic loop: corridor mix, deterministic A/B assignment, rate control
├── billing.py       # fee schedule (fixed + bps), ledger writes on success, balances
├── api.py           # Starlette app: /v1/*, /admin/*, lifespan (generator + prava poller tasks), container bind-host detection
├── dashboard.py     # _PAGE string template + server-rendered SVG fragments + ~15-line fetch-poll JS
├── server.py        # MCP stdio server: thin client over the REST API (via sdk)
├── sdk.py           # PayOptimizeClient(base_url, api_key, http=injectable)
└── cli.py           # serve | signup | demo-payment | inject   (+ __main__.py → cli.main())
scripts/agent_demo.py  # real-Prava agent purchase: create → QR in terminal → poll → settled
```

## 3. SQLite schema (exact)

Every connection: `PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=5000; PRAGMA foreign_keys=ON;` — schema executed once per DB path
per process via module-level `_initialized: set[str]` + `threading.Lock`.

```sql
CREATE TABLE IF NOT EXISTS tenants (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  email TEXT NOT NULL,
  created_ts TEXT NOT NULL,
  fee_bps INTEGER NOT NULL DEFAULT 45,        -- 0.45% simulated pricing
  fee_fixed_cents INTEGER NOT NULL DEFAULT 5
);
CREATE TABLE IF NOT EXISTS api_keys (
  id INTEGER PRIMARY KEY,
  tenant_id INTEGER NOT NULL REFERENCES tenants(id),
  key_hash TEXT NOT NULL UNIQUE,              -- sha256 hex of full pok_ key
  display_prefix TEXT NOT NULL,               -- "pok_a1b2c3d4…" for UI
  created_ts TEXT NOT NULL,
  revoked_ts TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS payments (
  id TEXT PRIMARY KEY,                        -- pay_<16 hex>
  tenant_id INTEGER NOT NULL REFERENCES tenants(id),
  amount_cents INTEGER NOT NULL,              -- integer money, always
  currency TEXT NOT NULL, country TEXT NOT NULL, card_brand TEXT NOT NULL,
  method TEXT NOT NULL DEFAULT 'card',        -- 'card' | 'prava'
  routing_mode TEXT NOT NULL,                 -- 'router' | 'baseline'
  segment TEXT NOT NULL,                      -- "US:USD:visa"
  status TEXT NOT NULL,                       -- pending|pending_approval|succeeded|failed
  final_provider TEXT NOT NULL DEFAULT '',
  decline_code TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT 'api',         -- 'api' | 'generator' | 'agent'
  description TEXT NOT NULL DEFAULT '',
  created_ts TEXT NOT NULL, resolved_ts TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS attempts (
  id INTEGER PRIMARY KEY,
  payment_id TEXT NOT NULL REFERENCES payments(id),
  seq INTEGER NOT NULL,                       -- 1..3 (cascade)
  provider TEXT NOT NULL,
  segment TEXT NOT NULL,                      -- denormalized: flat bandit rebuild query
  status TEXT NOT NULL,                       -- pending|succeeded|failed
  decline_code TEXT NOT NULL DEFAULT '',
  latency_ms INTEGER NOT NULL DEFAULT 0,
  prava_session_id TEXT NOT NULL DEFAULT '',
  prava_txn_id TEXT NOT NULL DEFAULT '',
  iframe_url TEXT NOT NULL DEFAULT '',
  created_ts TEXT NOT NULL, resolved_ts TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS provider_events (  -- admin injections + health transitions → chart annotations
  id INTEGER PRIMARY KEY,
  provider TEXT NOT NULL, kind TEXT NOT NULL, -- outage_start|degraded_start|cleared|health_degraded|health_recovered
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
```

**Bandit state:** in-memory `dict[(provider, segment), (alpha, beta)]`; on boot rebuild
by replaying the last 2,000 resolved attempts (reversed, same decay math). No
per-decision DB round-trip; the `attempts` table stays the analytics ground truth. No
health_snapshots table — health derives from `attempts` over a rolling window.

## 4. Router spec

- **Arms:** (provider, segment); segment = `country:currency:card_brand`. ~15 arms at
  5 generator corridors × 3 card providers. Prior **Beta(1,1)**.
- **Update on outcome r ∈ {0,1}:** discount every arm in the segment, floored at the
  prior — `α = max(0.99·α, 1)`, `β = max(0.99·β, 1)` — then add the reward to the played
  arm alone: `α += r`, `β += (1−r)`. The floor is load-bearing: an arm forgets its
  evidence, never the fact that "no opinion" is where it started.
  γ chosen by sweep (5 seeds × 4,000 payments, 0.95/0.85/0.80 corridor):

  | γ | best-arm share | uplift | degraded gap | reacts in | recovers in |
  |---|---|---|---|---|---|
  | 0.98 | 66% | +3.6 pts | +13.0 | 140 | 160 (64 s) |
  | **0.99** | **80%** | **+5.3 pts** | **+14.1** | **130** | **280 (112 s)** |
  | 0.995 | 91% | +6.8 pts | +13.9 | 180 | 510 (204 s) |

  γ=0.995 converges hardest but takes 3.4 min to forgive a recovered rail — longer than
  the whole demo. γ=0.99 meets every §9 target with the fastest usable recovery.
- **Decision:** with p=0.02 pick a uniformly random eligible provider (recovery
  probe); else sample θ ~ Beta(α,β) per eligible provider, pick argmax. Skip providers
  whose health state is `down`.
- **All RNG through one injected `random.Random(seed)`** — deterministic tests;
  `PAYOPTIMIZE_SEED` for rehearsals.
- **Reward = 1 iff authorized.** Fees NOT in reward (v1 cut, stated openly; ledger
  makes fees visible instead).
- **A/B:** generator traffic assigned by
  `int(sha256(payment_id).hexdigest(), 16) % 100 < 50` → baseline (round-robin, ignores
  learning and health, same cascade rules). Direct API/agent payments default `router`,
  overridable. Baseline outcomes ALSO update posteriors (free off-policy data).
- **Cascade taxonomy:**

  | Decline code | Class | Cascade? |
  |---|---|---|
  | `issuer_unavailable`, `provider_timeout`, `processing_error`, `rate_limited` | infrastructure | **yes** |
  | `do_not_honor` | soft decline | **no** (network rules; also the demo lever) |
  | `insufficient_funds`, `expired_card`, `invalid_card`, `fraud_suspected`, `stolen_card` | terminal | **no** |

  Max **3 attempts total**, never the same provider twice, each retry a new `attempts`
  row with `seq`.
- **Sim profiles (corridor deltas are the story):** `US:USD:visa` stripe 0.94 /
  braintree 0.88 / adyen 0.85 · `DE:EUR:visa` adyen 0.95 / stripe 0.87 / braintree
  0.83 · `US:USD:amex` braintree 0.93 / stripe 0.90 / adyen 0.80. Latency lognormal
  (median 120–350 ms via `asyncio.sleep`, scale=0 in tests). Fees: stripe 290bps+30¢ /
  adyen 260bps+22¢ / braintree 275bps+30¢ (UI only).
- **Outage-recovery timing (MEASURED, not estimated — 5 seeds, shipped router):**
  generator 5 tx/s, 50/50 split → 2.5 tx/s router arm. Degrade stripe_sim (auth → 0.45):
  its share falls under 10% within **~130 decisions (~50 s)**, and the router holds
  **+14 pts** over the baseline for as long as the degradation lasts — better than the
  ~8–10 pts originally estimated, because `do_not_honor` never cascades so the baseline
  eats every one of them. `clear` → the arm decays back to the prior and is
  rediscovered in **~280 decisions (~110 s)**, no operator action required.
  Corridor sims also cover `GB:GBP:mastercard` and `FR:EUR:mastercard` (the 4th and 5th
  generator corridors, unspecified above), plus a per-provider baseline for any corridor
  the API accepts that is not in the table.

## 5. Prava integration (the real rail)

Port from `~/projects/canibuy/src/canibuy/prava.py` **near-verbatim** (disclosed in
README): `create_session` :139-192 (incl. fail-loud `_user_identity` :119-136 →
`PAYOPTIMIZE_USER_ID`/`_USER_EMAIL`), `payment_result` :203-222 (keep the cache-buster
`_t` param + `Cache-Control: no-store` — without them a caching layer replays stale
`pending` forever), `credentials` :225-236 (token lives at
`transactions[].line_items[]`), `failure_code` :239-250 (store verbatim — first thing
Prava support asks for), `report_status` :253-284, `poll_payment` :287-304, key-mode +
live-key guards :44-90 (`CANIBUY_ALLOW_LIVE` → `PAYOPTIMIZE_ALLOW_LIVE`; keep the
stderr banner on live mints; drop the merchant-localhost guard). Keep the module
**sync**; the async app wraps calls in `anyio.to_thread` / `run_in_threadpool`.

**Attempt-model fit (async human approval):**
1. `POST /v1/payments {method:"prava"}` → bypass bandit → attempt(seq=1,
   provider="prava", pending) → `create_session` (amount_cents → `"12.50"` string at
   the adapter boundary) → store `prava_session_id` + `iframe_url` → payment
   **`pending_approval`**; response includes `iframe_url`.
2. **Lifespan poller** (asyncio task, every 3 s): scan `pending_approval` payments,
   `payment_result` in a thread. On `completed` + credential present: simulated
   downstream auth (approves; stated in README) → **`report_status(session_id,
   txn_ref_id, "APPROVED")` IMMEDIATELY** — skip this and the order hangs in
   `awaiting_result` forever (canibuy's hard-won rule) → attempt succeeded, payment
   `succeeded`, ledger fee row, `prava_txn_id` stored. On `failed`: store
   `failure_code(result)` as `decline_code`, payment failed — **no cascade** (a human
   decline is terminal by design). 10-min poll deadline → `approval_timeout`.
3. **Agent ride-through:** MCP `create_payment` → same public API with a real `pok_`
   key → `pending_approval` + iframe_url (QR) → human passkey-approves on phone →
   poller settles → agent's `wait_for_payment` returns `succeeded` — badged REAL in
   the dashboard feed.

## 6. API surface

Auth: `Authorization: Bearer pok_…` (sha256 → `api_keys.key_hash`). Admin routes:
`Bearer $PAYOPTIMIZE_ADMIN_TOKEN` (fail-loud at boot if unset). Errors: typed JSON
`{error}` with 401/403/404/422.

| Method/Path | Auth | Contract |
|---|---|---|
| `GET /v1` | none | self-describing API index |
| `POST /v1/tenants` | none | `{name, email}` → 201 `{tenant_id, api_key}` (key shown once) |
| `POST /v1/payments` | key | `{amount_cents, currency, country, card_brand, method?, description?, routing_mode?}` → 201 `{id, status, provider, decline_code?, attempts:[…], iframe_url?}`; card resolves synchronously (sims are fast), prava returns `pending_approval` |
| `GET /v1/payments/{id}` | key (own tenant) | payment + attempts |
| `GET /v1/payments?limit=&status=` | key | recent, own tenant |
| `GET /v1/analytics/summary?window=15m` | key | `{auth_rate, volume, avg_latency_ms, uplift_pts, by_provider, by_corridor}` |
| `GET /v1/ledger` | key | `{entries[], total_fees_cents}` |
| `GET /v1/providers` | none | health tiles: `{name, real, state, auth_rate_5m, p95_ms, fee}` |
| `POST /admin/outage` | admin | `{provider, mode: outage|degraded|clear, auth_rate?}` → provider_event |
| `POST /admin/generator` | admin | `{tps?, enabled?}` |
| `GET /admin/state` | admin | generator rate, injections, posterior means per arm |
| `GET /` + `GET /fragments/*` | none | dashboard (read-only aggregates, no secrets) |

## 7. Dashboard

Server-rendered inline **SVG fragments** + ~15 lines of inline JS fetch-polling
`/fragments/{name}` every 2 s and swapping innerHTML. One language, no build step,
fragments testable with TestClient. Port canibuy's `_PAGE` + `.format()` +
`html.escape` idiom. **Load the `dataviz` skill BEFORE writing any chart/tile code.**

Panels: (1) hero stat row — live auth rate, uplift pts, tx/min, "revenue recovered" $;
(2) auth-rate chart router vs baseline, 30 s buckets over 15 min, provider_events as
vertical annotations; (3) provider mix over time (router arm); (4) provider health
tiles — Prava badged **REAL · sandbox**, sims badged **SIMULATED**; (5) corridor
table; (6) recent transactions feed (Prava rows badged REAL; cascades show the chain
`stripe ✗ do_not_honor → adyen ✓`); (7) tenant strip (volume, fees metered).

## 8. MCP + SDK + demo script

- `sdk.py`: `PayOptimizeClient(base_url, api_key, http=injectable, timeout=…)` — the
  `http=` seam takes Starlette's TestClient in tests.
- `server.py`: MCP stdio server, thin client over the REST API via the SDK (env
  `PAYOPTIMIZE_API_URL` / `PAYOPTIMIZE_API_KEY`). Tools: `create_payment(amount_cents,
  currency, description, method="prava")` → `{payment_id, status, approval_url}`
  (docstring tells the agent to hand the approval URL to the human) ·
  `get_payment(id)` · `wait_for_payment(id, timeout_s=180)` · `provider_health()` ·
  `list_recent_payments(limit=10)`.
- `scripts/agent_demo.py`: same flow scripted — create → print iframe_url + **segno QR
  in the terminal** → poll → print settled result (rehearsal + fallback if the MCP
  client misbehaves on stage).

## 9. Test plan

`tests/conftest.py` autouse: `PAYOPTIMIZE_DB` → tmp_path ·
`PRAVA_SECRET_KEY=sk_test_unit` · `PAYOPTIMIZE_USER_ID/EMAIL=unit-test` ·
`PAYOPTIMIZE_ADMIN_TOKEN=test-admin` · **`PRAVA_API_BASE=https://prava.invalid`** (any
un-mocked call dies at DNS — no-network guarantee) · seeded `random.Random(42)`.

| File | Covers |
|---|---|
| `test_store.py` | schema init idempotent, WAL actually on, indexes exist, payment+attempt round-trip |
| `test_tenancy.py` | pok_ mint, hash-at-rest (raw key absent from DB), bad/revoked key → 401 |
| `test_providers_sim.py` | seeded determinism; corridor auth within ±3 pts over 2,000 draws; decline mixes; outage/degradation flips; latency_scale=0 |
| `test_router.py` | deterministic convergence (fixed 0.95/0.85/0.80, seed 42, 2,000 payments → best arm >70% share in last 500, router ≥ baseline+3 pts); outage recovery (<10% share within 300 decisions, recovers after clear); cascade table; budget=3; never-same-provider; hash split deterministic |
| `test_prava_adapter.py` | MockTransport port of canibuy's test_prava.py: mint body/bearer, 401 raises, empty identity refuses pre-spend, pending→completed settles with exactly one `report_status APPROVED`, failure_code verbatim, live-key guard |
| `test_api.py` | TestClient lifecycle: signup → pay → get → analytics; tenant isolation; admin guard; prava returns pending_approval+iframe_url |
| `test_billing.py` | fee = fixed + bps; ledger row only on success |
| `test_generator.py` | one `tick()` pure/deterministic (test the tick, never the loop) |
| `test_sdk_mcp.py` | SDK against injected TestClient; MCP tools through SDK stub |
| `test_dashboard.py` | `/` renders; fragments return SVG; REAL badge when a prava payment exists |

## 10. Phased hour budget (~24h working)

| # | Phase | Hrs | Cum | Exit criterion |
|---|---|---|---|---|
| 0 | Scaffold (DONE in prep session): git, uv, pyproject, docs | 0.5 | 0.5 | `uv run pytest` green |
| 1 | models + store (schema, WAL, init-once) + tenancy + tests | 2.5 | 3.0 | key auth round-trip green |
| 2 | providers/base + simulated (profiles, declines, injections) + tests | 2.0 | 5.0 | deterministic sim tests green |
| 3 | router (Thompson+decay+explore, cascade, baseline, boot rebuild) + tests | 2.5 | 7.5 | convergence test green |
| 4 | api.py (payments, auth, analytics) + TestClient tests | 2.0 | 9.5 | curl signup→pay→get works |
| 5 | generator + /admin/* + minimal dashboard v0 (auth line + feed) | 2.0 | 11.5 | outage inject visibly moves auth line |
| 6 | **Deploy to Fly NOW** + compose fallback | 1.0 | 12.5 | public URL shows moving dashboard |
| 7 | providers/prava.py port + poller + settle + mocked tests | 1.5 | 14.0 | mocked pending→settled green |
| 8 | sdk + MCP server + scripts/agent_demo.py (QR) | 1.5 | 15.5 | **MVP LINE** — agent creates prava payment locally |
| 9 | Dashboard full build (7 panels; dataviz skill first) | 3.0 | 18.5 | judges' page done on prod |
| 10 | billing ledger + tenant analytics + tenant strip | 1.0 | 19.5 | fees meter on real traffic |
| 11 | **Live Prava rehearsal** (≤5 sandbox tx: 2 API, 2 MCP-agent, 1 failure) + friction fixes + cli polish | 1.5 | 21.0 | end-to-end real payment on prod URL |
| 12 | README polish, Devfolio submission, demo run-through ×2 | 1.5 | 22.5 | submitted |
| — | Buffer (absorbed by whichever phase slips) | 1.5 | 24.0 | — |

**Slip valve:** if Phase 3 slips >1h, ship ε-greedy (same interfaces, ~20 lines),
upgrade to Thompson later. Stretch order after MVP: full dashboard > billing > CLI
polish.

**Pre-committed CUTS:** minute-rollup table · webhooks · refunds · idempotency keys ·
fee-aware reward · dashboard login · CSV export · rate limiting · key rotation ·
contextual features beyond corridor · multi-region · per-tenant generators · health
prober pings (the 2% explore covers it).

## 11. Risk register

| Risk | Mitigation |
|---|---|
| Time (solo, 24h) | MVP line at 15.5h; deploy at 12.5h; pre-committed cuts; green-merge discipline |
| Prava sandbox tx budget | all tests MockTransport (0 tx); identity fail-loud before minting; `report_status` always called (no hung sessions); rehearsal capped at ~5 tx |
| Demo-day passkey (phone/network) | passkey pre-enrolled; rehearse twice; QR fallback via scripts/agent_demo.py |
| Bandit invisible on stage | γ=0.98 + 2% explore designed for 45–90 s recovery; rehearse with `PAYOPTIMIZE_SEED` |
| SQLite contention under generator | WAL + busy_timeout + single worker + windowed indexed queries; default 2 tx/s, 5 only for demo |
| Fly surprises | deploy mid-build; `min_machines_running=1` (no cold start during judging); compose fallback |

## 12. Deploy runbook

**Dockerfile:** `FROM python:3.12-slim` → copy `pyproject.toml uv.lock README.md src/`
→ `pip install --no-cache-dir .` → `ENV PAYOPTIMIZE_DB=/data/payoptimize.sqlite3
PYTHONUNBUFFERED=1` → `VOLUME /data` → `EXPOSE 8080` →
`CMD ["python", "-m", "payoptimize", "serve"]`. Bind 0.0.0.0 when `/.dockerenv` or
`/run/.containerenv` exists (canibuy trick).

**fly.toml:** `app = "payoptimize"` · `[http_service] internal_port=8080,
force_https=true, auto_stop_machines=false, min_machines_running=1` · `[mounts]
source="data", destination="/data"` · `[env] PAYOPTIMIZE_DB="/data/payoptimize.sqlite3"`.

Commands: `fly launch --no-deploy` → `fly volumes create data --size 1` →
`fly secrets set PRAVA_SECRET_KEY=… PAYOPTIMIZE_USER_ID=… PAYOPTIMIZE_USER_EMAIL=…
PAYOPTIMIZE_ADMIN_TOKEN=…` → `fly deploy`. **Exactly one machine** (in-memory bandit).

**compose fallback:** bind-mount `./data:/data:z` (directory not file — WAL sibling
files; `:z` not `:Z` — Fedora/podman/SELinux), `env_file: .env`.

## 13. Demo script (~5 min)

- **0:00 — Problem + live product.** Public Fly URL. "5–15% of legitimate payments get
  declined; routing them intelligently recovers real revenue. This is live traffic
  through a real orchestration API." Router-vs-baseline lines + uplift stat.
- **0:45 — Merchant onboarding.** `curl POST /v1/tenants` → pok_ key →
  `POST /v1/payments` → JSON attempt chain → row in the feed → ledger meters the fee.
- **1:30 — Agent completes a REAL transaction.** Claude (MCP registered): "buy the Pro
  plan, $12.50" → `create_payment` → approval QR on screen → **passkey approval on the
  phone — Prava's actual UX** → `wait_for_payment` → succeeded; REAL-badged Prava row
  lands in the feed; note `report-status` settled it.
- **3:00 — Live failure, live learning.** `POST /admin/outage {stripe_sim, degraded}`
  → baseline sinks ~8–10 pts and stays; router dips then recovers in ~60–90 s; mix
  chart slides to adyen/braintree; annotation marks the injection. Show `/admin/state`
  posteriors while it happens. `clear` → probe traffic rediscovers stripe.
- **4:15 — Business + honesty.** Simulated rails labeled vs the real Prava rail;
  pricing = SaaS + bps, already metered; v2 = real PSP adapters + fee-aware reward.
  Close on repo + live URL.
