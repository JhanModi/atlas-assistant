# ADR-0001: Strands Agents on a local Ollama model

- **Status:** accepted
- **Date:** 2026-11-13

## Context
Atlas needs an agent loop with tool calling, lifecycle hooks (for permission gating and metrics), human-in-the-loop approval, and per-run metrics. It must cost nothing and keep private data on the laptop.

## Options considered
1. **Strands Agents (Apache-2.0)** with `OllamaModel`
2. A hand-written agent loop over the Ollama API
3. Other agent frameworks

## Decision
Use **Strands Agents** (`strands-agents` 1.64.0) with `strands.models.ollama.OllamaModel`. Always pass the model explicitly, because the default provider is AWS Bedrock.

## Evidence (verified against the installed SDK, 2026-11-13)
- Hook events exist with the fields we need:
  - `BeforeToolCallEvent` has `cancel_tool`, `interrupt()` and `invocation_state`.
  - `AfterToolCallEvent` has `duration`, `result` and `exception`.
  - `Before/AfterModelCallEvent` exist.
- A spike with a scripted fake model confirmed:
  - `invocation_state` reaches the hooks.
  - `cancel_tool` returns an error tool result to the model.
  - `interrupt()` pauses the run with `stop_reason == "interrupt"`, and `agent([{"interruptResponse": ...}])` resumes it.
- `AgentResult.metrics` exposes `cycle_count`, `tool_metrics` (call/success/error counts), `accumulated_usage` and `accumulated_metrics.latencyMs`.
  - The **Ollama provider does not report `timeToFirstByteMs`**, so Atlas measures time to first token in its own streaming callback.
- `OllamaModel` passes `additional_args` straight into the Ollama request, so `{"think": false}` turns off Qwen3 thinking mode for latency.
- `strands-agents-tools` is deprecated, so we write our own `@tool` functions.

## Consequences
- Permission gating, metrics and approvals are implemented as Strands hooks rather than custom loop code.
- Strands has no built-in scheduler or requests-per-minute limiter, so we use APScheduler and our own token-bucket hook.
- SDK method names are pinned to `>=1.64,<2`. Re-run the spike tests (`tests/test_agent_pipeline.py`) after any upgrade.
- Model choice is decided by `atlas bench` and recorded in [ADR-0005](0005-default-model.md) (qwen3.5:4b).
