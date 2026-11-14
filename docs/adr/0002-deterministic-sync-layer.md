# ADR-0002: Deterministic sync layer + SQLite, LLM on top

- **Status:** accepted
- **Date:** 2026-11-13

## Context
Small local models (4–8B) are unreliable at date arithmetic, comparing calendars and multi-step API orchestration. Calling remote APIs on every question is also slow and burns rate limits.

## Decision
- Connectors poll each source on a schedule and normalize everything into one SQLite `Item` table.
- Deadlines, overlaps, date ranges, authorization and scheduling are **plain Python**.
- The model only interprets requests, calls a few coarse read tools over the local store, and explains the results.
- Relative dates in tool arguments ("next friday", "this week") are resolved by `atlas.timeutil` (dateparser plus explicit rules), not by the model.

## Consequences
- Answers are fast and work offline for data already synced. Accuracy no longer depends on the model doing arithmetic.
- Data is only as fresh as the last sync. The briefing shows the sync time for each source.
- Every new source needs a connector and normalizer, which is more code but testable with recorded fixtures.
