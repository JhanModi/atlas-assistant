"""Strict allowlist: a tool runs only if policy.yaml lists it for this run's context."""

from __future__ import annotations

from typing import Any

from strands.hooks import BeforeToolCallEvent, HookProvider, HookRegistry

from atlas.config import Policy
from atlas.hooks.audit_log import AuditLog
from atlas.hooks.state import CTX, RunRegistry, ToolCallRecord

ORDER = -50  # before every other tool hook


class AllowlistHook(HookProvider):
    def __init__(self, policy: Policy, runs: RunRegistry, audit: AuditLog) -> None:
        self.policy = policy
        self.runs = runs
        self.audit = audit

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self.check, order=ORDER)

    def check(self, event: BeforeToolCallEvent) -> None:
        name = event.tool_use["name"]
        context = (event.invocation_state or {}).get(CTX)
        run = self.runs.get(event.invocation_state)
        tool_use_id = event.tool_use.get("toolUseId", "")
        if run is not None and tool_use_id not in run.tool_calls:
            run.tool_calls[tool_use_id] = ToolCallRecord(
                tool_use_id=tool_use_id,
                tool=name,
                tier=self.policy.tool(name).tier,
                args=dict(event.tool_use.get("input") or {}),
            )
        if context is None or not self.policy.allowed(context, name) or event.selected_tool is None:
            event.cancel_tool = f"Tool '{name}' is not available here. Answer with the tools you have, or say you can't."
            if run is not None:
                rec = run.tool_calls[tool_use_id]
                rec.decision, rec.status = "blocked_allowlist", "cancelled"
            self.audit.write(
                kind="tool_blocked", reason="allowlist", tool=name, context=context,
                interaction_id=run.interaction_id if run else None,
            )
