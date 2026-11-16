# 02 · Architecture

## Core principle: a deterministic sync layer, with the LLM on top

```
 Sources (Canvas, Google Calendar, routine.yaml, later Gmail/GitHub/Outlook/LinkedIn-email)
        │  connectors: poll, normalize, rate-limit         (plain Python, no LLM)
        ▼
 SQLite store  ── Item / SyncState / Interaction / ToolCall / Approval / Alert / Feedback
        │                    ▲
        │  read tools        │ detectors (deadlines, conflicts) - plain Python
        ▼                    │
 Single Strands agent (local Ollama model)  ◄── hooks: allowlist → taint → approval gate,
        │                                            rate limiter, metrics, audit log
        ▼
 Sanitized response → web dashboard (127.0.0.1) / Windows toast
```

- **Connectors** never call the model. They turn every source into the same `Item` shape (`src/atlas/connectors/`, [ADR-0006](adr/0006-sync-layer-design.md)).
  - Each sync refreshes a fixed window (yesterday through 30 days out) and replaces only what that window covers.
  - A failed sync changes no data.
  - An expired sign-in parks the source as `needs_reauth`; other failures back off. There are no retry loops.
- **Ordinary code** does dates, overlaps, deadlines, authorization and scheduling. The model interprets the request, picks tools and explains results.
- **One agent with ≤5 tools** (Phase 1–3). Specialist agents are split out only when evals show the single agent struggling (ADR-0003).

## Runtime pipeline (every chat message or scheduled trigger)

1. **Ingest**: `Assistant.handle()` creates an `Interaction` row (context = chat / briefing / proactive / eval).
2. **Agent loop**: the Strands agent picks tools. The chosen tool = intent; its arguments = slots.
3. **Gate hooks** on every tool call, in order:
   1. `AllowlistHook`: cancels tools not allowed in this context.
   2. `TaintHook`: marks the run tainted after any tool returns untrusted text.
   3. `ApprovalGateHook`: T0 runs; T1/T2 interrupt for approval (unless graduated *and* untainted); T3 and unknown tools are cancelled.
   4. `RateLimitHook` throttles model calls (only needed for cloud tiers).
4. **Verify**: write tools check their post-conditions (Phase 4).
5. **Respond**: output passes through `security.sanitize.clean_output()`.
6. **Measure**: `MetricsHook` records model time, tool time, end-to-end time and time to first token, and writes `ToolCall` rows. The user can rate the answer.

Approval pauses use Strands interrupts: the run returns `stop_reason == "interrupt"`, an `Approval` row is created, and the agent resumes with `{"interruptResponse": ...}` when the user decides. In Phase 1 paused agents live in memory; Phase 4 moves them to `FileSessionManager` so they survive restarts.

## Data model (src/atlas/store/models.py)

| Table | Purpose |
|---|---|
| `Item` | One normalized thing: event, lecture, deadline, message, notification. `id = "<source>:<source_id>"`. Includes `trust`, `private` and `first_seen_at` (used to spot new announcements). |
| `SyncState` | Per-source health: `status` (never / ok / error / rate_limited / needs_reauth / not_configured), last attempt and last success, backoff (`next_attempt_at`, `consecutive_failures`), and an error message with URLs and tokens removed. |
| `Interaction` | One request/run: context, text, status, tools used, split latency, tokens, model id. |
| `ToolCall` | Every tool call: tier, decision (auto/approved/rejected/blocked), duration, status. |
| `Approval` | Pending/decided approval requests linked to a Strands interrupt id. |
| `Alert` | Proactive alerts (unique `dedupe_key`), how they were delivered (`channel`: toast or briefing) and whether the user found them useful. |
| `Briefing` | Each generated briefing. Daily ones have a unique `daily_key` (the local date), so there's one per day. Fields: trigger (scheduled / catch_up / manual), the content, stale sources, and the user's verdict for the Phase 2 gate. |
| `Feedback`, `NpsSurvey` | CSAT / NPS signals. |

All datetimes are stored in UTC and converted to `settings.timezone` (America/New_York) for display and date math.

## Code layout

```
src/atlas/
  config.py          settings.toml + policy.yaml + time_baselines.yaml loaders
  timeutil.py        timezone + natural-language date ranges (dateparser) - slots are normalized here
  store/             SQLModel tables + engine/session helpers + forward-only column migration
  connectors/        base (coverage, sync runner), routine, ics_feed, canvas, gcal (+ OAuth loopback/PKCE)
  vault.py           credentials in the OS keyring (never in files or the DB)
  proactive/         detectors (deadlines, missing, clashes, announcements, stale sources), alerts (dedupe + toast
                     cap), briefing (composed by code, unique per day), notify (Windows toast), scheduler (APScheduler
                     in `atlas serve`: sync every 30 min, daily briefing with catch-up) - ADR-0007
  agent/             model factory, system prompt, the 5 read tools, Assistant (runtime pipeline)
  hooks/             allowlist, taint, approval gate, rate limiter, metrics, audit log
  security/          output sanitizer, argument validation
  metrics/           KPI computation from SQLite
  evaluation/        model benchmark, synthetic dev data
  web/               FastAPI app + templates
  cli.py             atlas doctor | ask | serve | bench | kpis | seed-demo
```

## Model choice

`qwen3.5:4b` by default, chosen by `atlas bench` over `qwen3:8b` and `qwen3:4b` ([ADR-0005](adr/0005-default-model.md)). It fits entirely in 6 GB VRAM with thinking switched off. Re-run the benchmark before changing models; `think = "auto"` picks the thinking setting per model. Only `OllamaModel` is constructed today. A cloud provider can be added later behind the privacy rule in `03-security.md`.
