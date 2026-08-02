# Fine-tuning the ops agent

Goal: distill the `gpt-5` teacher into a fine-tuned small model (`gpt-4.1-mini`
class) that runs the frequent, cost-sensitive trigger paths — cheaper, faster, more
consistent tool use — while the teacher stays on the rare, quality-sensitive
`/v1/agent/ask` path.

The privacy property carries through by construction: transcripts are captured
**after** the Redactor, so the training corpus physically cannot contain what the
model was never shown. No raw tenant name, email, key, or card-shaped number can
enter a training file, because it never entered a transcript.

```
1 Capture ──▶ 2 Generate ──▶ 3 Curate ──▶ 4 Export JSONL ──▶ 5 SFT ──▶ 6 Eval ──▶ 7 Rollout
   (built)      (scenarios       (evidence      (+ denylist       (gpt-4.1-   (teacher vs   (env
                 × teacher)       cross-check)   scan)             mini)       ft vs base)   switch)
                     ▲                                                            │
                     └────────────────────── not promoted ────────────────────────┘
```

## 1. Capture — already built

`PAYOPTIMIZE_AGENT_CAPTURE=1` makes every run store its full redacted message array
(system, user, assistant tool_calls, tool results, final answer) in the
`agent_transcripts` table (`store.insert_agent_transcript`, written from
`agent/loop.py`). Off by default; zero effect on the run itself. Turn it on at
deploy so the production corpus accumulates from day one.

## 2. Generate volume — `scripts/agent_scenarios.py` (to build)

Drive the local app (TestClient, `PAYOPTIMIZE_SEED` pinned, generator on) through
reproducible scenario families, with the gpt-5 teacher answering and capture on:

- degradation → recovery on each simulated rail (`/admin/outage` injections)
- hard outage → mix shift → clear
- stranded payments (seed `pending` rows, let the trigger reconcile)
- unknown decline codes (seed attempts with codes outside the taxonomy)
- Prava failures: `FETCH_AGENTIC_CREDS_ERROR`, `approval_timeout`, `PASSKEY_REG_FAILED`
- merchant analytics questions (uplift, corridors, fees) at varied windows/volumes

Target ~300–1,000 runs. At roughly 3–10k tokens per run this is ~1M–5M teacher
tokens — comfortably inside the hackathon credits.

## 3. Curate

Keep a run only if:

- every payment id and decline code cited in the answer appears in the run's
  evidence trail (automated cross-check — no hallucinated facts in the corpus);
- every action passed its guard (status `executed`, or correctly `proposed`);
- it finished under 7 turns (budget-cap runs teach flailing);
- it is not a near-duplicate of a kept run (hash on scenario family + tool sequence).

Balance across trigger kinds so `ask` chatter does not drown incident handling.
Hand-review ~10% before export.

## 4. Export — `scripts/ft_export.py` (to build)

- Read `agent_transcripts` (`store.agent_transcripts`), emit OpenAI fine-tuning
  JSONL in the tool-calling chat format (tools list included per example).
- Split train/val 90/10 **by scenario family**, never by run — sibling runs leak.
- Before writing anything: rebuild `Redactor` from the live environment and scan
  every line against `denylist()`; refuse the whole export on any hit. Same
  fail-loud contract as `llm.complete`.

## 5. Train

OpenAI fine-tuning API, SFT on `gpt-4.1-mini`, default hyperparameters, 2–3 epochs.
Watch validation loss — the corpus is small and overfits fast.

## 6. Eval — `scripts/ft_eval.py` (to build)

~50 held-out golden scenarios with expected outcomes. Score each model
(teacher / fine-tune / base-mini) on:

- **diagnosis correctness** — expected verdict / decline code named in the answer
- **action correctness** — expected action kind + params, exactly (or correctly none)
- **tool discipline** — zero unknown-tool or bad-argument calls
- **privacy** — zero denylist hits in any output (reuse the redactor test corpus)
- cost per run and p50 latency

**Promotion rule:** the fine-tune must land within 5 pts of the teacher on action
correctness AND strictly beat base-mini, with zero privacy hits. Otherwise iterate
on the corpus (step 2) — usually more scenario diversity, not more epochs.

## 7. Rollout

`fly secrets set PAYOPTIMIZE_AGENT_MODEL=ft:gpt-4.1-mini:…` — the config getter
already accepts any model id. Optional follow-up: a per-trigger model map so
triggers run the fine-tune while `/v1/agent/ask` keeps the teacher; one small
change in `agent/loop.py` (`model=` is already a parameter end to end).

Keep capture on after rollout: the fine-tune's own production runs, curated the
same way, become the next training round.
