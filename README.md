# PayOptimize AI

**Payment orchestration that routes every payment to the provider most likely to
authorize it — and learns from every outcome.**

**Live: [payoptimize.fly.dev](https://payoptimize.fly.dev/)** — real traffic, moving now.
The dashboard is public and read-only; no login, nothing staged.

5–15% of legitimate payments are declined. Most of those declines are not about the
card: they are about *which provider* the payment went through, in *which corridor*, at
*which moment*. PayOptimize sits between merchants and payment providers, routes each
transaction with an online learner, cascades retriable declines to the next-best rail,
and shows the recovered revenue on a live dashboard.

```
merchants ──▶ PayOptimize API ──▶ bandit router + cascade retry ──▶ providers
                                    │                                ├─ Prava (LIVE, sandbox rail)
                                    │  discounted Thompson sampling  ├─ stripe_sim    (simulated)
                                    │  decline-code-aware failover   ├─ braintree_sim (simulated)
                                    ▼                                └─ adyen_sim     (simulated)
                             analytics + health + fee ledger
```

## Key features

- **Intelligent routing** — discounted Thompson sampling per corridor
  (country × currency × card brand): the router *measures* provider auth rates and
  shifts traffic to whoever is authorizing best, right now.
- **Authorization optimization** — decline-code-aware cascade: infrastructure failures
  (`issuer_unavailable`, `provider_timeout`, …) fail over to the next-best provider;
  terminal and fraud declines never retry.
- **Cross-border intelligence** — per-corridor learning: the provider that wins
  `US:USD:visa` is not the one that wins `DE:EUR:visa`, and the router knows.
- **Infrastructure monitoring** — rolling-window health per provider (auth rate, p95
  latency, state), with live outage/degradation injection so you can watch the router
  route around a failing rail and recover.
- **Analytics dashboard** — router-vs-baseline auth rate (a real 50/50 A/B split on
  generated traffic — uplift is measured, not claimed), provider mix over time,
  corridor table, transaction feed, fee ledger.

## Agent-native by design

An MCP server exposes `create_payment` / `wait_for_payment` / `provider_health`, so an
AI agent can complete a real purchase whose payment rides through PayOptimize to the
**Prava** rail: the agent initiates, the human approves with their passkey (Prava's
actual UX — the human stays in control of spend), and the settled transaction lands in
the dashboard feed.

## What is real and what is simulated (honest labeling)

This was built in a 48-hour window. We label rather than pretend:

- **Prava is real.** Sessions are minted, paid, and settled against Prava's sandbox
  API (`create_session` → passkey approval → `payment-result` → `report-status`).
  Prava rows in the feed are badged **REAL · sandbox**.
- **Stripe/Braintree/Adyen are simulated adapters** with realistic behavior profiles —
  per-corridor auth rates, latency distributions, decline-code mixes, fee schedules,
  injectable outages. Badged **SIMULATED** everywhere they appear. The v2 roadmap
  replaces them with real PSP sandbox integrations behind the same adapter protocol.
- **One mechanical simplification, stated plainly:** when a real Prava credential is
  received, PayOptimize's *downstream* authorization step is simulated (it approves)
  before we settle the session with `report-status APPROVED`. The session, the human
  passkey approval, the credential, and the settlement are all real sandbox operations.
- Generator traffic is synthetic and labeled as such; the API and MCP paths are fully
  usable by anyone with a key.

## Measured, not claimed

Every number below came out of the running system, not a design document.

**Routing.** Over 5 seeds × 4,000 payments on a 0.95 / 0.85 / 0.80 corridor, the router
puts **80%** of first attempts on the best rail and beats the round-robin control by
**+5.3 pts**. While a rail is degraded the gap widens to **+14 pts**, because
`do_not_honor` is non-retriable by design — the control eats every one of them.

**Reacting.** Degrade a rail to 45% auth and its share of router traffic falls under 10%
within ~130 decisions (~50 s at demo throughput). Clear the injection and it is
rediscovered in ~280 (~110 s), with no operator action.

**Uplift carries its uncertainty.** A live run of 30 payments per arm once read
`-10.0 pts`. That was noise — the 95% interval was `[-23.8, +3.8]`. So the API returns a
Wald interval and one of three statuses (`no_data` / `collecting` / `measured`) alongside
every uplift figure, and "measured" needs both 100 payments an arm *and* an interval
excluding zero. A point estimate without an interval is a claim wearing a measurement's
clothes.

**Verification.** 435 tests. The suite makes a real socket impossible — both httpx
transports raise — so it can never spend one of a finite number of Prava sandbox
transactions.

## The ops agent

An LLM sits inside the backend and does the work an on-call engineer would:
diagnoses a failure from the evidence, explains a health transition, answers a
merchant's question about their own payments, and proposes fixes. It is invoked
on request (`POST /v1/agent/ask`, `/diagnose`), by an outside agent over MCP
(`ask_ops`, `diagnose_payment`), and **unprompted** — a watcher narrates
unrecognised decline codes, health changes, and abandoned payments into the
dashboard's Incidents panel.

It never sees anything private, and that is enforced rather than intended:

- **A send-time denylist assert.** `llm.complete()` serializes the outgoing body,
  scans it for tenant secrets, `pok_`/`sk` keys and card-like digit runs, and
  **raises before the request leaves**. A redaction bug anywhere upstream fails
  loudly instead of leaking quietly.
- **Least privilege by construction.** A `ToolBox` is built with a `tenant_id`
  and no tool schema exposes it, so a toolbox built for one merchant physically
  cannot fetch another's payment — it is not a rule the model is asked to follow.
- **Tenant identities are pseudonyms.** Names and emails become `tenant_<id>`
  before they reach the model, restorable only for the caller's own tenant.
- **Exactly one write path.** A test greps the package: the agent may mutate
  payment state in one place, inside a guarded executor. If that ever fails,
  something gave the model a second route to the database.

**Autonomy splits by blast radius, not by a single switch.** Clearing an
injection or nudging the generator rate is reversible in one call, so those
execute immediately. Reconciling a payment writes a merchant's record of what
happened to their money, so it is *always* proposed for a human — and no
environment variable can promote it. `PAYOPTIMIZE_AGENT_AUTONOMY=propose` is a
kill switch that downgrades everything and upgrades nothing.

**An LLM problem is never a payments problem.** No key, a rate limit, a model
outage, a bug in a tool — each costs a narrative and nothing else. There is a
test asserting payments, routing, the dashboard and the fee ledger behave
bit-identically with the agent dead.

```bash
OPENAI_API_KEY=sk-…                 # unset is a normal deployment; agent routes 503
PAYOPTIMIZE_AGENT_MODEL=gpt-5       # default
PAYOPTIMIZE_AGENT_TRIGGERS=0        # stop it narrating on its own
PAYOPTIMIZE_AGENT_AUTONOMY=propose  # park every action for a human
```

## Monetization

Fixed fee + basis points on each **authorized** payment (default 0.45% + 5¢), metered
per tenant in a real ledger and readable at `GET /v1/ledger`. **Declines are never
charged**: a payment we failed to get through is not a service we performed, and billing
for it would invert the product's entire claim. The dashboard shows fees metered beside
revenue recovered, so the pricing argument is a ratio you can read rather than a slide.

## Quickstart

```bash
make setup    # uv sync + .env with a generated admin token
make test     # 435 tests; no network, no sandbox transactions
make serve    # http://127.0.0.1:8080 — dashboard + API
```

The Prava rail stays off until you put a real `sk_test_` key in `.env`; `method="prava"`
returns a clean 503 until then, and the card rails work regardless. To point an agent at
it, run the MCP server with `PAYOPTIMIZE_API_URL` and `PAYOPTIMIZE_API_KEY` set:

```bash
uv run python -m payoptimize.server                       # MCP over stdio
uv run python scripts/agent_demo.py --url … --key pok_…   # the same flow, with a terminal QR
```

See [`docs/PLAN.md`](docs/PLAN.md) for the full architecture, schema, router math,
API surface, and build order.

## Provenance and disclosure

Built solo for the Agentic Commerce Hackathon (Devfolio × Prava, Aug 2026), inside the
build window, in this repo.

**Disclosed pre-existing work:** the core of `src/payoptimize/providers/prava.py`
(session mint, payment-result poll with cache-buster, credential extraction,
`report-status` settlement, live-key safety guards) is ported from the author's
pre-hackathon project [canibuy](https://github.com/Pawansingh3889/canibuy), where it
was written and verified against the live sandbox. It is carried over near-verbatim on
purpose — rewriting a verified integration is how it quietly stops being verified. Two
changes: the env prefix, and canibuy's merchant-localhost guard is gone (it pointed
sessions at its own fixture storefront and had to refuse spending real money on it;
here the merchant is the API caller). The adapter, the settle poller, and everything
else in this repo are in-window.

**Deviations from the plan, and why.** `docs/PLAN.md` was written before the code and
is corrected where the code proved it wrong — the two disagreements are recorded there
rather than quietly resolved. The router originally specified decaying only the *played*
arm; built that way, a starved arm's counts freeze and a recovered rail was rediscovered
in **1 of 5 seeded runs**, which breaks the demo's closing beat. Discounting every arm
in the segment fixes it. And the deploy came up unreachable because a Fly machine is a
Firecracker microVM, not a container — it has neither `/.dockerenv` nor
`/run/.containerenv`, so container detection fell through to loopback. Deploying at the
planned mid-build hour rather than last is what surfaced it.

## v2 roadmap

Real PSP sandbox adapters (Stripe/Adyen test creds) behind the same `ProviderAdapter`
protocol · fee- and latency-aware reward (route on margin, not just auth) · contextual
bandit features (BIN, amount band, time of day) · smart retry *timing* · network
tokens · outbound webhooks · multi-region.
