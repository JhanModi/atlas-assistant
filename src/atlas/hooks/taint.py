"""Data-flow tracking: once untrusted third-party text enters a run, the run is tainted.

Two signals: the tool's declared output trust (policy.yaml), and any returned item flagged "untrusted": true
(e.g. a calendar invite written by someone else, which can appear in get_agenda). A tainted run can't use
graduated (auto-approved) writes: every write needs explicit approval.
"""

from __future__ import annotations

import json
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
        result = event.result or {}
        if result.get("status") != "success":
            return
        if self.policy.tool(name).output_trust == "untrusted" or _flags_untrusted(result):
            run.tainted = True


def _flags_untrusted(result: dict) -> bool:
    """Strands serializes dict results with json.dumps, so the flag appears verbatim in the text content."""
    for block in result.get("content") or []:
        if isinstance(block, dict):
            if '"untrusted": true' in str(block.get("text", "")):
                return True
            if isinstance(block.get("json"), dict) and '"untrusted": true' in json.dumps(block["json"]):
                return True
    return False
