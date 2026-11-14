"""Approval gate: T0 runs, T1/T2 pause for human approval (unless graduated and untainted), T3 is cancelled.

Uses Strands interrupts: event.interrupt() pauses the whole run (stop_reason == "interrupt"); when the
agent is resumed with an interruptResponse, the same call returns the user's decision.

The decision is bound to the arguments the user was shown ({"decision": "approve", "args_digest": ...}).
If the arguments differ from what the tool would actually run with, the call is cancelled.
"""

from __future__ import annotations

from typing import Any

from strands.hooks import BeforeToolCallEvent, HookProvider, HookRegistry

from atlas.config import Policy
from atlas.hooks.audit_log import AuditLog
from atlas.hooks.state import RunRegistry
from atlas.security.integrity import args_digest

ORDER = 10  # after the allowlist
APPROVE = "approve"


class ApprovalGateHook(HookProvider):
    def __init__(self, policy: Policy, runs: RunRegistry, audit: AuditLog) -> None:
        self.policy = policy
        self.runs = runs
        self.audit = audit

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self.gate, order=ORDER)

    def gate(self, event: BeforeToolCallEvent) -> None:
        if event.cancel_tool:  # already blocked by the allowlist
            return
        name = event.tool_use["name"]
        tier = self.policy.tool(name).tier
        run = self.runs.get(event.invocation_state)
        rec = run.tool_calls.get(event.tool_use.get("toolUseId", "")) if run else None
        iid = run.interaction_id if run else None

        def decide(decision: str) -> None:
            if rec is not None:
                rec.decision = decision
            self.audit.write(kind="tool_decision", tool=name, tier=tier, decision=decision, interaction_id=iid)

        if tier == "T0":
            decide("auto")
            return
        if tier == "T3":
            event.cancel_tool = f"'{name}' is never allowed. Tell the user you can't do this."
            if rec is not None:
                rec.status = "cancelled"
            decide("blocked_tier")
            return

        # T1 / T2: writes.
        tainted = bool(run and run.tainted)
        if name in self.policy.graduated and not tainted:
            decide("auto")
            return

        args = dict(event.tool_use.get("input") or {})
        digest = args_digest(args)
        answer = event.interrupt(
            "approval",
            reason={
                "tool": name,
                "tier": tier,
                "args": args,
                "args_digest": digest,
                "tainted": tainted,
                "why": "untrusted content was read in this run" if tainted else f"{tier} write needs approval",
            },
        )
        # The response must approve *these exact arguments* (the ones shown to the user).
        decision = answer.get("decision") if isinstance(answer, dict) else None
        if decision == APPROVE and answer.get("args_digest") == digest:
            decide("approved")
            return
        if rec is not None:
            rec.status = "cancelled"
        if decision == APPROVE:
            event.cancel_tool = "The approval did not match this action, so it was not done. Tell the user."
            decide("rejected_mismatch")
        else:
            event.cancel_tool = "The user rejected this action. Do not retry it; tell the user it was not done."
            decide("rejected")
