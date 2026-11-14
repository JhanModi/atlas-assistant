# 00 · Development process

Atlas is built in phases. Each phase ends with a **gate review**: a short ADR in `docs/adr/` that records the measured results, what passed, what failed, and the decision to move on (or not).

Guiding principle: **reliable before autonomous.** No phase adds autonomy until the previous phase's behaviour is measured and trustworthy.

## Phases

| # | Phase | Goal | Gate (exit criteria) |
|---|---|---|---|
| 1 | Local foundation | Ollama + Strands + SQLite + minimal web chat | Ask Atlas a question and get a local answer. Model benchmark recorded in an ADR. Default model chosen from data. |
| 2 | Reliable daily briefing | Canvas + Google Calendar + `routine.yaml` → one accurate morning summary. Deterministic deadline/conflict detectors. | 7 days of briefs that match a manual check. 0 missed deadlines. Baselines recorded. |
| 3 | Conversational search | Answers grounded in synced data via the 5 read tools. Golden set ≥150 utterances. | Answers cite their items. Baseline intent/slot accuracy recorded. Failures analysed. |
| 4 | More integrations + approved actions | Gmail, GitHub, Outlook (if consent allows), LinkedIn emails. Drafts, reminders, archive, mark-done, all approval-gated. | Numeric KPI targets start to apply (docs/04-metrics.md). Test proves zero writes run without approval. |
| 5 | Proactivity, memory, advanced evaluation | Preference memory, prep briefs, weekly review, feedback-tuned alerts, graduated autonomy, red-team suite. | Useful-alert precision ≥70%, deadline recall 100%, red-team suite passes. |

## Per-phase workflow

1. **Requirements**: update `01-requirements.md` with the user stories in scope.
2. **Design**: update `02-architecture.md` / `03-security.md`; write an ADR for any significant choice.
3. **Test first where possible**: add golden-set cases and unit tests before the feature.
4. **Build** the smallest version that satisfies the stories.
5. **Measure**: run `atlas bench`, `atlas kpis`, and evals; compare against the previous baseline.
6. **Gate review**: write `docs/adr/NNNN-phase-N-gate.md` using the template.

## Definition of done (every change)

- Unit tests pass (`pytest`).
- No new tool without a `config/policy.yaml` entry (unknown tools are denied).
- Any new data source documents its auth path, fallback, rate limit and data trust level.
- Metrics hooks still record the interaction (check the Metrics page).
