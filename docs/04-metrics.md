# 04 · Metrics

**Policy:** Phases 1–3 **record baselines**. Numeric targets are aspirational until Phase 4, when they become gate criteria. A useful prototype is never blocked by a target it hasn't been measured against yet.

All online metrics are computed by `src/atlas/metrics/kpis.py` from SQLite and shown on the Metrics page (`/metrics`) and by `atlas kpis`.

## Definitions

### 1. Intent & slot accuracy
- **Offline (primary):** `evals/datasets/golden.jsonl` maps each utterance to the expected tool(s) and arguments.
  - *Intent accuracy* = share of cases where the first tool called equals the expected tool. For out-of-scope cases, the expectation is "no tool".
  - *Slot accuracy* = share of expected arguments that match after normalization. Date ranges are compared after `timeutil` resolves them.
- **Online:** a 👎 with the reason "misunderstood" counts as an intent miss.
- Aspirational: intent ≥90%, slot ≥85%.

### 2. Task completion rate
`Interaction.status` takes one of these values:
- `completed`: answered or done with no user help
- `completed_with_approval`: done; the approval was required by policy, **not** counted as intervention
- `clarified`: Atlas asked the user a question
- `failed`: error, or an answer Atlas couldn't give
- `escalated`: handed back to the user

Completion = (completed + completed_with_approval) / all. Aspirational ≥80%.

### 3. Level of autonomy
Each interaction is tagged L0–L4:
- **L0**: answered without tools
- **L1**: one source or tool
- **L2**: cross-tool, two or more distinct tools
- **L3**: proactively triggered (briefing/alert)
- **L4**: proactive and auto-executed a graduated write

Reported as a distribution, plus the number of graduated write tools.

### 4. Latency, split
| Field | Meaning |
|---|---|
| `model_ms` | Sum of model call durations (BeforeModelCall → AfterModelCall hooks) |
| `tool_ms` | Sum of tool durations (AfterToolCall `duration`) |
| `e2e_ms` | Request in → response ready |
| `ttfb_ms` | Request in → first streamed token (measured by Atlas; Ollama doesn't report it) |
| `sync_ms` | Connector/network time (per `SyncState`, Phase 2+), kept separate from answer latency |

Reported as p50 and p95 per context. Aspirational: read answers p50 < 3 s.

### 5. Fallback & escalation rate
(clarified + failed + escalated) / all interactions. Aspirational < 10%.

### 6. Useful proactivity (replaces "% proactive")
- **Alert precision** = alerts marked useful or acted on / alerts sent. Aspirational ≥70%.
- **Deadline recall** = deadlines that got an alert before they were due / all deadlines. Target **100%**.
- **Volume cap:** at most 5 non-urgent alerts per day, so the KPI can't be gamed by spamming.

### 7. Time saved
For each completed interaction, add the minutes from `config/time_baselines.yaml` for the tasks its tools map to. Add the value of each useful alert. Subtract an estimated 1 minute for each 👎 (correction time). Reported as hours per week.

### 8. CSAT / NPS
- **CSAT** = 👍 / (👍 + 👎) plus an optional 1–5 score per answer. Aspirational ≥ 4.2/5 (equivalent: ≥84% 👍).
- **Self-NPS:** a weekly 0–10 "how likely would you recommend Atlas" prompt. With one user it's a **trend**, not a real NPS, and is labelled that way.
- From Phase 4, the **edit distance** of approved drafts is tracked as an implicit signal.

## Baselines log
Record baselines in phase gate ADRs (`docs/adr/`). Current baseline ([ADR-0005](adr/0005-default-model.md), re-run 2026-11-14): "qwen3.5:4b, 8k ctx, demo data, reference Friday: intent 1.00 (38 cases), slots 0.98, scenarios 8/8, weekday sweep 28/28, p50 e2e 2.6 s (p95 7.5 s), first token 1.8 s".
