# PayOptimize AI

**Payment orchestration that routes every payment to the provider most likely to
authorize it — and learns from every outcome.**

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

## Monetization

SaaS platform fee + basis points on every routed transaction — the fee ledger already
meters it per tenant. The pricing story is self-funding: **we charge a slice of the
declines we recover.**

## Quickstart

```bash
uv sync
cp .env.example .env         # sk_test_ key from dashboard.prava.space + passkey identity

uv run pytest                # no network, no sandbox transactions
uv run python -m payoptimize serve    # http://127.0.0.1:8080 — dashboard + API
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
was written and verified against the live sandbox. Everything else here is in-window.

## v2 roadmap

Real PSP sandbox adapters (Stripe/Adyen test creds) behind the same `ProviderAdapter`
protocol · fee- and latency-aware reward (route on margin, not just auth) · contextual
bandit features (BIN, amount band, time of day) · smart retry *timing* · network
tokens · outbound webhooks · multi-region.
