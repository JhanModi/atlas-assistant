"""Split latency: model time vs tool time, per run. End-to-end and time-to-first-token are measured by the Assistant.

We time tools ourselves with perf_counter: Strands' own `duration` uses time.monotonic(), which on Windows
only ticks every ~15.6 ms, so fast SQLite-backed tools would read as 0 ms.
"""

from __future__ import annotations

import time
from typing import Any

from strands.hooks import (
    AfterModelCallEvent,
    AfterToolCallEvent,
    BeforeModelCallEvent,
    BeforeToolCallEvent,
    HookProvider,
    HookRegistry,
)

from atlas.hooks.state import RunRegistry

TOOL_START_ORDER = 90  # after the allowlist/approval gate, so approval waiting isn't counted as tool time


class MetricsHook(HookProvider):
    def __init__(self, runs: RunRegistry, clock=time.perf_counter) -> None:
        self.runs = runs
        self.clock = clock
        self._tool_started: dict[str, float] = {}

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeModelCallEvent, self.before_model)
        registry.add_callback(AfterModelCallEvent, self.after_model)
        registry.add_callback(BeforeToolCallEvent, self.before_tool, order=TOOL_START_ORDER)
        registry.add_callback(AfterToolCallEvent, self.after_tool)

    def before_model(self, event: BeforeModelCallEvent) -> None:
        if run := self.runs.get(event.invocation_state):
            run._model_started = self.clock()

    def after_model(self, event: AfterModelCallEvent) -> None:
        run = self.runs.get(event.invocation_state)
        if run is None or run._model_started is None:
            return
        run.model_ms += (self.clock() - run._model_started) * 1000.0
        run.model_calls += 1
        run._model_started = None

    def before_tool(self, event: BeforeToolCallEvent) -> None:
        if self.runs.get(event.invocation_state) is not None:
            self._tool_started[event.tool_use.get("toolUseId", "")] = self.clock()

    def after_tool(self, event: AfterToolCallEvent) -> None:
        run = self.runs.get(event.invocation_state)
        tool_use_id = event.tool_use.get("toolUseId", "")
        started = self._tool_started.pop(tool_use_id, None)
        if run is None:
            return
        ms = (self.clock() - started) * 1000.0 if started is not None else (event.duration or 0.0) * 1000.0
        run.tool_ms += ms
        rec = run.tool_calls.get(tool_use_id)
        if rec is not None:
            rec.duration_ms = ms
            if rec.status != "cancelled":
                rec.status = (event.result or {}).get("status") or ("error" if event.exception else "success")
