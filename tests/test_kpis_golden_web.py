from datetime import timedelta

from fastapi.testclient import TestClient

from atlas.evaluation.golden import judge_intent, load_cases, run_case, summarize
from atlas.metrics import compute, percentile
from atlas.store import Alert, Feedback, Interaction, session_scope, utcnow


def test_percentile():
    assert percentile([1, 2, 3, 4], 0.5) == 2.5
    assert percentile([], 0.5) is None


def test_kpis_from_interactions(engine):
    now = utcnow()
    with session_scope(engine) as db:
        rows = [
            Interaction(status="completed", tools_used=["get_deadlines"], autonomy_level=1, e2e_ms=1000, model_ms=900, tool_ms=5),
            Interaction(status="completed", tools_used=["get_agenda", "find_conflicts"], autonomy_level=2, e2e_ms=3000, model_ms=2800, tool_ms=8),
            Interaction(status="clarified", autonomy_level=0, e2e_ms=500),
            Interaction(status="failed", autonomy_level=1, e2e_ms=200),
            Interaction(status="completed", context="eval", tools_used=["get_agenda"]),  # excluded
        ]
        db.add_all(rows)
        db.add(Feedback(interaction_id=rows[0].id, thumbs=1))
        db.add(Feedback(interaction_id=rows[1].id, thumbs=-1, reason="misunderstood"))
        db.add(Alert(detector="due_soon", dedupe_key="a", title="HW", notified_at=now, feedback="useful"))
        db.add(Alert(detector="conflict", dedupe_key="b", title="X", notified_at=now, feedback="not_useful"))
        db.commit()
    k = compute(engine, days=7)
    assert k["interactions"] == 4
    assert k["completion_rate"] == 0.5
    assert k["fallback_rate"] == 0.5
    assert k["autonomy_distribution"] == {"L0": 1, "L1": 2, "L2": 1}
    assert k["latency_ms"]["e2e_ms"]["p50"] == 750
    assert k["read_latency_ms"]["e2e_ms"]["n"] == 3  # the failed run isn't an answer...
    assert k["failed_latency_ms"]["e2e_ms"] == {"p50": 200, "p95": 200, "n": 1}  # ...but stays visible
    assert k["csat"]["thumbs_up_rate"] == 0.5
    assert k["intent_online"]["misunderstood_feedback"] == 1
    assert k["alerts"]["precision"] == 0.5
    # time saved: deadline 2 + agenda 1.5 + conflicts 3 + useful alert 5 - 1 correction = 10.5 min
    assert k["time_saved_hours"] == round(10.5 / 60, 2)


def test_kpis_window_excludes_old(engine):
    with session_scope(engine) as db:
        db.add(Interaction(status="completed", created_at=utcnow() - timedelta(days=30)))
        db.commit()
    assert compute(engine, days=7)["interactions"] == 0


def test_golden_dataset_is_well_formed():
    cases = load_cases()
    assert len(cases) >= 30
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids))
    valid = {"get_agenda", "get_deadlines", "find_conflicts", "get_routine", "search_items", None}
    assert all(c["tool"] in valid for c in cases)


def test_judge_intent_rules():
    assert judge_intent({"tool": None}, [])
    assert not judge_intent({"tool": None}, ["get_agenda"])
    assert judge_intent({"tool": "get_routine", "accept": ["get_agenda"]}, ["get_agenda"])
    assert judge_intent({"tool": "get_deadlines", "expect_all": ["get_deadlines", "find_conflicts"]},
                        ["find_conflicts", "get_deadlines"])
    assert not judge_intent({"tool": "get_deadlines", "expect_all": ["get_deadlines", "find_conflicts"]},
                            ["get_deadlines"])


def test_run_case_scores_intent_and_slots(make_assistant, demo_engine):
    a, _ = make_assistant([{"tool": "get_deadlines", "input": {"start": "this week"}}, {"text": "HW4"},
                           {"tool": "get_agenda", "input": {"start": "next week"}}, {"text": "x"}])
    good = run_case(a, demo_engine, {"id": "1", "utterance": "due this week?", "tool": "get_deadlines", "args": {"start": "this week"}})
    bad = run_case(a, demo_engine, {"id": "2", "utterance": "due next week?", "tool": "get_deadlines", "args": {"start": "next week"}})
    assert good.intent_ok and (good.slots_matched, good.slots_expected) == (2, 2)
    assert not bad.intent_ok and (bad.slots_matched, bad.slots_expected) == (0, 2)
    s = summarize([good, bad])
    assert s["intent_accuracy"] == 0.5 and s["slot_accuracy"] == 0.5 and len(s["misses"]) == 1


def test_web_chat_feedback_and_metrics(make_assistant, demo_engine):
    from atlas.config import Settings
    from atlas.web.app import create_app

    a, _ = make_assistant([{"tool": "get_deadlines", "input": {}}, {"text": "HW4 due tomorrow."}])
    client = TestClient(create_app(engine=demo_engine, assistant=a, settings=Settings()))
    page = client.get("/")
    assert page.status_code == 200 and "Content-Security-Policy" in page.text
    r = client.post("/api/chat", json={"text": "what's due?", "session_id": "web"}).json()
    assert r["status"] == "completed" and r["reply"] == "HW4 due tomorrow."
    assert client.post("/api/feedback", json={"interaction_id": r["interaction_id"], "thumbs": 1}).json() == {"ok": True}
    assert client.post("/api/feedback", json={"interaction_id": "nope", "thumbs": 1}).status_code == 404
    assert client.post("/api/nps", json={"score": 8}).json() == {"ok": True}
    assert client.post("/api/nps", json={"score": 11}).status_code == 422
    m = client.get("/metrics")
    assert m.status_code == 200 and "Task completion" in m.text
    assert client.get("/api/kpis").json()["csat"]["thumbs_up_rate"] == 1.0
