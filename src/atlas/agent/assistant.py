"""The runtime pipeline (docs/02-architecture.md): ingest → agent loop → gate hooks → respond → measure.

One Strands agent per chat session (so follow-up questions keep context). Paused runs (approval
interrupts) are kept in memory in Phase 1; Phase 4 moves them to FileSessionManager.
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from sqlalchemy.engine import Engine
from sqlmodel import select
from strands import Agent
from strands.agent.conversation_manager import SlidingWindowConversationManager
from strands.models.model import Model

from atlas import timeutil
from atlas.agent.model import build_model
from atlas.agent.prompts import build_system_prompt
from atlas.agent.tools import make_tools
from atlas.config import Policy, Settings, get_settings, load_policy
from atlas.hooks import (
    APPROVE,
    CTX,
    IID,
    AllowlistHook,
    ApprovalGateHook,
    AuditLog,
    MetricsHook,
    RateLimitHook,
    RunRegistry,
    RunState,
    TaintHook,
)
from atlas.ratelimit import TokenBucket
from atlas.security.integrity import args_digest
from atlas.security.sanitize import clean_output
from atlas.store import Approval, Interaction, ToolCall, session_scope, utcnow

_CANT = re.compile(r"^\s*(i can'?t do that|i can(?:no|')t|i'?m (?:not able|unable)|i am (?:not able|unable)|sorry, i can)", re.I)


@dataclass
class TurnResult:
    interaction_id: str
    status: str
    reply: str
    approvals: list[dict] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    tools: list[str] = field(default_factory=list)
    raw_reply: str = ""  # model output before sanitizing; for evals only, never rendered


class _StreamProbe:
    """Callback handler that timestamps the first streamed text chunk (Ollama doesn't report TTFB)."""

    def __init__(self) -> None:
        self.first_token_at: float | None = None
        self.on_text: Callable[[str], None] | None = None

    def reset(self) -> None:
        self.first_token_at = None

    def __call__(self, **kwargs: Any) -> None:
        data = kwargs.get("data")
        if data:
            if self.first_token_at is None:
                self.first_token_at = time.perf_counter()
            if self.on_text:
                self.on_text(data)


@dataclass
class _Session:
    agent: Agent
    probe: _StreamProbe
    lock: threading.Lock = field(default_factory=threading.Lock)


@dataclass
class _Paused:
    session_id: str
    context: str
    active_ms: float
    ttfb_ms: float | None
    approval_ids: list[str]


class Assistant:
    def __init__(
        self,
        engine: Engine,
        settings: Settings | None = None,
        policy: Policy | None = None,
        model: Model | None = None,
        audit: AuditLog | None = None,
        tools: list | None = None,
    ) -> None:
        self.engine = engine
        self.settings = settings or get_settings()
        self.policy = policy or load_policy()
        self.model = model or build_model(self.settings.model)
        self.model_id = (self.model.get_config() or {}).get("model_id", "unknown")
        self.runs = RunRegistry()
        self.audit = audit if audit is not None else AuditLog(self.settings.data_path / "audit.log")
        self.tools = tools if tools is not None else make_tools(engine)
        self.hooks: list = [
            AllowlistHook(self.policy, self.runs, self.audit),
            TaintHook(self.policy, self.runs),
            ApprovalGateHook(self.policy, self.runs, self.audit),
            MetricsHook(self.runs),
        ]
        rpm = self.settings.model.rate_limit.requests_per_minute
        if rpm > 0:
            self.hooks.append(RateLimitHook(TokenBucket(rpm)))
        self._sessions: dict[str, _Session] = {}
        self._paused: dict[str, _Paused] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ sessions

    def _session(self, session_id: str) -> _Session:
        with self._lock:
            sess = self._sessions.get(session_id)
            if sess is None:
                probe = _StreamProbe()
                agent = Agent(
                    model=self.model,
                    tools=list(self.tools),
                    system_prompt=build_system_prompt(timeutil.now()),
                    hooks=self.hooks,
                    callback_handler=probe,
                    conversation_manager=SlidingWindowConversationManager(window_size=20),
                    name="atlas",
                )
                sess = _Session(agent, probe)
                self._sessions[session_id] = sess
            return sess

    def reset_session(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    # ------------------------------------------------------------------ public API

    def handle(self, text: str, context: str = "chat", session_id: str | None = None) -> TurnResult:
        """Run one user message (or scheduled trigger) through the pipeline."""
        session_id = session_id or uuid.uuid4().hex
        interaction = Interaction(context=context, session_id=session_id, user_text=text, model_id=self.model_id)
        with session_scope(self.engine) as db:
            db.add(interaction)
            db.commit()
        run = self.runs.start(interaction.id, context)
        sess = self._session(session_id)
        with sess.lock:
            sess.agent.system_prompt = build_system_prompt(timeutil.now())
            return self._run(session_id, sess, run, text, started=time.perf_counter(), prior_active_ms=0.0, prior_ttfb=None)

    def pending_approvals(self) -> list[Approval]:
        with session_scope(self.engine) as db:
            return list(db.exec(select(Approval).where(Approval.status == "pending")).all())

    def decide(self, approval_id: str, approve: bool) -> TurnResult | None:
        """Record a decision. When every approval of the paused run is decided, resume it."""
        with session_scope(self.engine) as db:
            appr = db.get(Approval, approval_id)
            if appr is None or appr.status != "pending":
                return None
            paused = self._paused.get(appr.interaction_id)
            if paused is None:  # process restarted: the paused agent is gone (Phase 4 persists it)
                appr.status, appr.decided_at = "expired", utcnow()
                db.add(appr)
                db.commit()
                return None
            appr.status, appr.decided_at = ("approved" if approve else "rejected"), utcnow()
            db.add(appr)
            db.commit()
            batch = [db.get(Approval, aid) for aid in paused.approval_ids]
            if any(a is None or a.status == "pending" for a in batch):
                return None
            # Each decision carries a digest of the arguments the user was shown; the gate re-checks it.
            responses = [
                {"interruptResponse": {"interruptId": a.interrupt_id, "response": {
                    "decision": APPROVE if a.status == "approved" else "reject",
                    "args_digest": args_digest(a.args),
                }}}
                for a in batch
            ]
        iid = appr.interaction_id
        self._paused.pop(iid, None)
        run = self.runs.by_id(iid) or self.runs.start(iid, paused.context)
        sess = self._session(paused.session_id)
        with sess.lock:
            return self._run(paused.session_id, sess, run, responses, started=time.perf_counter(),
                             prior_active_ms=paused.active_ms, prior_ttfb=paused.ttfb_ms)

    # ------------------------------------------------------------------ internals

    def _run(self, session_id: str, sess: _Session, run: RunState, prompt: Any, started: float,
             prior_active_ms: float, prior_ttfb: float | None) -> TurnResult:
        state = {IID: run.interaction_id, CTX: run.context}
        sess.probe.reset()
        error: str | None = None
        result = None
        try:
            result = sess.agent(prompt, invocation_state=state)
        except Exception as exc:  # model down, bad tool output, etc. -> a measured failure, not a crash
            error = f"{type(exc).__name__}: {exc}"
        active_ms = prior_active_ms + (time.perf_counter() - started) * 1000.0
        ttfb = prior_ttfb
        if ttfb is None and sess.probe.first_token_at is not None:
            ttfb = (sess.probe.first_token_at - started) * 1000.0 + prior_active_ms
        if result is not None and (inv := result.metrics.latest_agent_invocation) is not None:
            run.input_tokens += inv.usage.get("inputTokens", 0) or 0
            run.output_tokens += inv.usage.get("outputTokens", 0) or 0
            run.cycles += len(inv.cycles)

        if result is not None and result.stop_reason == "interrupt":
            return self._pause(session_id, run, result, active_ms, ttfb)
        return self._finish(run, result, error, active_ms, ttfb)

    def _pause(self, session_id: str, run: RunState, result, active_ms: float, ttfb: float | None) -> TurnResult:
        approvals: list[dict] = []
        with session_scope(self.engine) as db:
            for intr in result.interrupts or []:
                reason = intr.reason if isinstance(intr.reason, dict) else {"detail": intr.reason}
                appr = Approval(
                    interaction_id=run.interaction_id,
                    interrupt_id=intr.id,
                    tool=str(reason.get("tool", "?")),
                    args=reason.get("args") or {},
                    reason=reason,
                )
                db.add(appr)
                approvals.append({"id": appr.id, "tool": appr.tool, "args": appr.args, "why": reason.get("why")})
            inter = db.get(Interaction, run.interaction_id)
            inter.status = "awaiting_approval"
            db.add(inter)
            db.commit()
        self._paused[run.interaction_id] = _Paused(
            session_id, run.context, active_ms, ttfb, [a["id"] for a in approvals]
        )
        return TurnResult(run.interaction_id, "awaiting_approval", "This needs your approval.", approvals)

    def _finish(self, run: RunState, result, error: str | None, active_ms: float, ttfb: float | None) -> TurnResult:
        calls = run.ordered_calls
        executed = [c for c in calls if c.decision in ("auto", "approved") and c.status != "cancelled"]
        raw_reply = str(result) if result is not None else ""
        reply = clean_output(raw_reply)
        tools_used = [c.tool for c in executed]

        succeeded = [c for c in executed if c.status == "success"]
        if error is not None:
            status = "failed"
            reply = "Something went wrong and I couldn't finish. (The error was logged.)"
        elif executed and not succeeded:  # every tool errored: any answer is ungrounded
            status = "failed"
        elif any(c.decision == "approved" for c in calls):
            status = "completed_with_approval"
        elif not tools_used and reply.rstrip().endswith("?"):
            status = "clarified"
        elif _CANT.match(reply) or not reply:
            status = "escalated"
        else:
            status = "completed"

        distinct = len(set(tools_used))
        if run.context in ("briefing", "proactive"):
            autonomy = 3
        else:
            autonomy = 0 if distinct == 0 else (1 if distinct == 1 else 2)

        metrics = {
            "e2e_ms": round(active_ms, 1),
            "model_ms": round(run.model_ms, 1),
            "tool_ms": round(run.tool_ms, 1),
            "ttfb_ms": round(ttfb, 1) if ttfb is not None else None,
            "model_calls": run.model_calls,
            "input_tokens": run.input_tokens,
            "output_tokens": run.output_tokens,
        }
        with session_scope(self.engine) as db:
            inter = db.get(Interaction, run.interaction_id)
            inter.response_text = reply
            inter.status = status
            inter.autonomy_level = autonomy
            inter.tools_used = tools_used
            inter.tool_args = [{"tool": c.tool, "args": c.args, "decision": c.decision} for c in calls]
            inter.model_ms, inter.tool_ms, inter.e2e_ms, inter.ttfb_ms = (
                metrics["model_ms"], metrics["tool_ms"], metrics["e2e_ms"], metrics["ttfb_ms"],
            )
            inter.input_tokens, inter.output_tokens = metrics["input_tokens"], metrics["output_tokens"]
            inter.cycle_count = run.cycles
            inter.error = error
            db.add(inter)
            for c in calls:
                db.add(ToolCall(
                    interaction_id=run.interaction_id, tool=c.tool, tier=c.tier, decision=c.decision,
                    args=c.args, duration_ms=c.duration_ms, status=c.status,
                ))
            db.commit()
        if error:
            self.audit.write(kind="run_error", interaction_id=run.interaction_id, error=error)
        self.runs.pop(run.interaction_id)
        return TurnResult(run.interaction_id, status, reply, [], metrics, tools_used, raw_reply)
