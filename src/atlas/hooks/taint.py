"""Data-flow tracking: once untrusted third-party text enters a run, the run is tainted.

A tainted run can't use graduated (auto-approved) writes: every write needs explicit approval.
"""

from __future__ import annotations

from typing import Any

from strands.hooks import AfterToolCallEvent, HookProvider, HookRegistry

from atlas.config import Policy
from atlas.hooks.state import RunRegistry


class TaintHook(HookProvider):
    def __init__(self, policy: Policy, runs: RunRegistry) -> None:
        self.policy = policy
        self.runs = runs

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(AfterToolCallEvent, self.after_tool)

    def after_tool(self, event: AfterToolCallEvent) -> None:
        run = self.runs.get(event.invocation_state)
        if run is None:
            return
        name = event.tool_use["name"]
        status = (event.result or {}).get("status")
        if status == "success" and self.policy.tool(name).output_trust == "untrusted":
            run.tainted = True
