"""Per-run state shared by the hooks, keyed by the interaction id passed in invocation_state."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

IID = "atlas_interaction_id"
CTX = "atlas_context"


@dataclass
class ToolCallRecord:
    tool_use_id: str
    tool: str
    tier: str
    args: dict
    decision: str = "pending"  # auto | approved | rejected | blocked_allowlist | blocked_tier
    duration_ms: float | None = None
    status: str | None = None  # success | error | cancelled


@dataclass
class RunState:
    interaction_id: str
    context: str
    tainted: bool = False
    model_ms: float = 0.0
    tool_ms: float = 0.0
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cycles: int = 0
    _model_started: float | None = None
    tool_calls: dict[str, ToolCallRecord] = field(default_factory=dict)

    @property
    def ordered_calls(self) -> list[ToolCallRecord]:
        return list(self.tool_calls.values())


class RunRegistry:
    def __init__(self) -> None:
        self._runs: dict[str, RunState] = {}
        self._lock = threading.Lock()

    def start(self, interaction_id: str, context: str) -> RunState:
        with self._lock:
            run = self._runs.get(interaction_id)
            if run is None:
                run = RunState(interaction_id, context)
                self._runs[interaction_id] = run
            return run

    def get(self, invocation_state: dict[str, Any] | None) -> RunState | None:
        if not invocation_state:
            return None
        iid = invocation_state.get(IID)
        return self._runs.get(iid) if iid else None

    def by_id(self, interaction_id: str) -> RunState | None:
        return self._runs.get(interaction_id)

    def pop(self, interaction_id: str) -> RunState | None:
        with self._lock:
            return self._runs.pop(interaction_id, None)
