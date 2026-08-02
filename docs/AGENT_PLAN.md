# Ops Agent — build status and carry-forward plan

An LLM agent embedded in the backend that diagnoses failures, explains incidents,
answers merchant questions, and fixes operational problems — under a privacy boundary
that provably keeps tenant PII and credentials out of every model call.

Decisions locked: OpenAI via raw httpx (no SDK), model default `gpt-5`
(`PAYOPTIMIZE_AGENT_MODEL` overrides); **guarded full autonomy** — whitelisted
actions execute immediately when their hard in-code guard passes,
`PAYOPTIMIZE_AGENT_AUTONOMY=propose` parks everything for human approval instead;
triggers fire the agent on events, kill-switch `PAYOPTIMIZE_AGENT_TRIGGERS=0`.

## Built and green (branch `feat/ops-agent`)

| Piece | Where | State |
|---|---|---|
| Audit schema: `agent_runs`, `agent_actions`, `agent_transcripts` + accessors (`decide_agent_action` is an exactly-once guarded UPDATE); `stranded_payments()` (the reconcile guard's definition, in SQL); watcher accessors (`resolved_attempts_since`, `provider_events_since`, high-water marks) | `store.py` | done, tested |
| Agent config: `openai_api_key/base`, `agent_model`, `agent_autonomy`, `agent_triggers_enabled`, `agent_capture_enabled`, `secret_values` | `config.py` | done, tested |
| Privacy boundary: `Redactor` — tenant names/emails → stable `tenant_<id>` pseudonyms (restorable only for the caller's own tenant), secrets/`pok_…`/`sk…`/card-like digit runs removed; `denylist()` | `agent/privacy.py` | done, tested |
| OpenAI wrapper: sync, `http=` seam, **send-time denylist assert on the serialized body**, no temperature sent | `agent/llm.py` | done, tested |
| Read tools, tenant-bound at construction, output redacted: `get_payment`, `list_recent_payments`, `analytics_summary`, `provider_health`, `provider_events`, `find_stranded_payments`, `ledger_statement` | `agent/tools.py` | done, tested |
| Agent loop: system prompt (taxonomy, cascade, corridors, Prava, honest labeling), 10-turn cap, unconditional audit row, opt-in transcript capture | `agent/loop.py` | done, tested |
| Test env: `OPENAI_API_BASE=https://openai.invalid` + capture/trigger pins; `tests/agent_stub.py` scripted-OpenAI MockTransport | `tests/` | done |

Everything below is **not built yet** — it is the remaining plan.

## Remaining build order

### Step 4 — `agent/actions.py` + action tools + admin endpoints

- `agent/actions.py`: whitelist `reconcile | clear_injection | generator_rate`.
  `submit(engine, generator, run_id, kind, params, rationale)`:
  - `agent_autonomy()=='auto'` → insert action row `proposed`, run the guard+executor,
    `mark_agent_action` → `executed` (or `failed` on exception, `proposed` stays when
    the guard refuses — a human can approve later when state changed; guards re-run
    at approval).
  - `propose` mode → insert row `proposed`, never execute.
  - Guards (re-checked at execution time, always):
    - **reconcile**: re-fetch payment; require `status='pending'` and no pending
      attempt (exactly `store.stranded_payments()` semantics) →
      `store.finalize_payment(status=FAILED, decline_code=engine.RAIL_UNAVAILABLE_CODE)`.
    - **clear_injection**: provider exists and `hasattr(p, 'inject')` (the real rail
      physically excluded — same check as `api.admin_outage`) → `p.inject('clear')` +
      `store.insert_provider_event(name, 'cleared', detail='agent')`.
    - **generator_rate**: generator running and `0 < tps <= 5` → `generator.set_rate()`.
- Action tools in `agent/tools.py` (`reconcile_payment`, `clear_injection`,
  `set_generator_rate`, each with a `reason` arg) → delegate to `actions.submit`,
  append to `ToolBox.actions_log`, tool result tells the model executed/proposed.
  Add their specs to `ToolBox.specs()`.
- Admin endpoints in `api.py`: `GET /admin/agent/actions` (pending + recent),
  `POST /admin/agent/actions/{id} {decision: approved|rejected}` via existing
  `_require_admin`; approve = `store.decide_agent_action(..., 'approved')` then
  execute then `mark_agent_action` — exactly-once by construction.
- Tests: guard refusals (succeeded payment, prava injection, tps 50), auto-vs-propose,
  double-approve races, executor failure → `failed`.

### Step 5 — `/v1/agent/*` + SDK + MCP

- `api.py` routes (tenant key auth; unconfigured agent → typed 503 when
  `config.openai_api_key()` is empty):
  - `POST /v1/agent/ask {question}` → `run_in_threadpool(loop.run, engine, question,
    tenant_id=…, trigger='ask', generator=app.state.generator,
    http=app.state.agent_http)` → `{run_id, answer: display_answer, actions, usage}`.
    (`app.state.agent_http = None` in lifespan; tests inject a MockTransport client.)
  - `POST /v1/agent/diagnose {payment_id}` → 404-not-403 ownership check first
    (same as `get_payment`), then a canned diagnose question, `trigger='diagnose'`.
  - `GET /v1/agent/runs` → `store.recent_agent_runs(tenant_id=…)`.
- `sdk.py`: `ask_ops(question)`, `diagnose_payment(payment_id)`, `agent_runs(limit)`.
- `server.py` MCP tools `ask_ops`, `diagnose_payment` — thin over the SDK, docstrings
  explain the resident agent.
- Tests in `test_api.py` / `test_sdk_mcp.py` style.

### Step 6 — `agent/triggers.py` + lifespan + dashboard Incidents

- `TriggerWatcher` dataclass mirroring `PravaPoller`: `watch_once()` holds all logic,
  `run()` is a trivial suppress-and-sleep loop (~15 s). High-water marks
  (`latest_attempt_id` / `latest_provider_event_id`) initialized on first sweep — no
  backfill. Sweeps:
  1. failed attempts whose `decline_code` is outside `DeclineCode` ∪
     `{rail_unavailable, provider_error, prava_failed, downstream_declined}` →
     diagnosis run (`trigger='unknown_decline'`);
  2. `health_degraded`/`health_recovered` events → narrative run (`trigger='health_event'`);
  3. `store.stranded_payments()` non-empty (minus an in-memory seen-set, so propose
     mode does not refire every sweep) → reconcile run (`trigger='stranded'`).
  Cap LLM runs per sweep (~3). Loop calls via `run_in_threadpool`.
- Lifespan in `api.py`: start the watcher only when
  `config.agent_triggers_enabled() and config.openai_api_key()`; task cancelled on
  shutdown like the poller. **Money-path isolation test**: payments process
  identically with the agent unconfigured or its client raising.
- Dashboard: `render_incidents` in `dashboard.py`, `FRAGMENTS['incidents']` from
  `store.recent_agent_runs(kinds=('unknown_decline','health_event','stranded'))`,
  one `<section>` in `_PAGE` + add to the JS poll list. Public page → redacted text
  only, pseudonyms stay pseudonyms, auto-executed actions read "auto-remediated ✓".

### Step 7 — README + deploy (owner steps at the end)

- README: Ops Agent section (what it is, privacy invariants, autonomy modes,
  endpoints) + runbook below. Update the test count.
- Deploy runbook (needs flyctl + secrets, so run locally):
  ```bash
  fly secrets set OPENAI_API_KEY=sk-…        # stages
  fly deploy                                  # single machine, as always
  ```
  On first boot the stranded-payment trigger finds the two stranded production rows,
  explains them, and (autonomy=auto) reconciles them to `failed/rail_unavailable` —
  audited in `agent_runs`/`agent_actions`, visible in the Incidents panel. Then one
  live smoke:
  ```bash
  curl -s -X POST https://payoptimize.fly.dev/v1/agent/diagnose \
    -H "Authorization: Bearer pok_…" -H 'content-type: application/json' \
    -d '{"payment_id": "pay_9d6c3403ee650850"}'
  ```
  Confirm the narrative cites the verbatim `FETCH_AGENTIC_CREDS_ERROR` /
  "Visa 400 — Fetching cryptogram failed" story and check the fee ledger grew no
  new lines. Verify `/admin/agent/actions` shows the reconciles `executed`.

## Privacy invariants (each needs its test kept green)

1. Nothing on the denylist crosses — send-time assert + the seeded-PII loop test.
2. Least privilege — tools tenant-bound at construction (tested).
3. No unguarded writes — loop/tools import no store write functions; mutations only
   via `actions.submit()`'s guards (grep-verifiable; add the guard tests in step 4).
4. Everything audited — runs and actions always logged (tested).
5. Money path isolation — triggers suppress-and-log; payments identical with the
   agent dead (test in step 6).

## Last stage

After deploy + live smoke, the fine-tuning milestone takes over — see
`docs/FINETUNE.md`. The capture hook (`PAYOPTIMIZE_AGENT_CAPTURE=1`) is already
built, so the corpus starts accumulating from the first deployed run.
