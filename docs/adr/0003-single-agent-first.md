# ADR-0003: One agent with ≤5 tools first; specialists only on evidence

- **Status:** accepted
- **Date:** 2026-11-13

## Context
A multi-agent design (schedule, inbox, coursework, dev and briefing agents) is a sensible long-term shape. But it adds routing errors, latency (an extra model call per hop) and complexity before we know that a single agent struggles.

## Decision
Phases 1–3 use **one Strands agent** with five read tools: `get_agenda`, `get_deadlines`, `find_conflicts`, `get_routine` and `search_items`. The tool the agent chooses is the intent, and its arguments are the slots, so intent and slot accuracy can be measured without a separate router.

## Revisit when
Phase 4 adds Gmail/GitHub tools. If the golden-set intent accuracy drops by more than 5 points or the tool count exceeds 7, split out a specialist and expose it with `agent.as_tool()`. Record that decision in a new ADR.
