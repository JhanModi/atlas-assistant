"""Phase 2 part 2: detectors, alerts and toasts, the daily briefing, catch-up scheduling and the gate KPI.

Frozen now (conftest): Friday 2026-11-13 09:00 America/New_York. Demo data is relative to that.
"""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from atlas import timeutil
from atlas.config import Settings
from atlas.metrics.kpis import accurate_streak, compute
from atlas.proactive import alerts as alert_store
from atlas.proactive import briefing
from atlas.proactive.detectors import detect
from atlas.proactive.notify import RecordingNotifier, ToastNotifier
from atlas.proactive.scheduler import AtlasScheduler
from atlas.store import Alert, Briefing, Item, SyncState

ZONE = timeutil.tz()


def at(day, hour, minute=0):
    return datetime(2026, 11, day, hour, minute, tzinfo=ZONE)


NOW = at(13, 9)


def add(engine, **kw):
    kw.setdefault("source", "gcal")
    kw.setdefault("source_id", kw["id"])
    with Session(engine) as db:
        db.add(Item(**kw))
        db.commit()


def scheduler(engine, settings=None, connectors=None, notifier=None):
    notifier = notifier or RecordingNotifier()
    return AtlasScheduler(engine, settings or Settings(), notifier, "http://127.0.0.1:8765",
                          connectors=lambda s: connectors or []), notifier


def alerts(engine):
    with Session(engine) as db:
        return list(db.exec(select(Alert)).all())


# ------------------------------------------------------------------ detectors

def test_detectors_on_demo_data(demo_engine):
    found: dict[str, list] = {}
    for f in detect(demo_engine, Settings(), NOW, since=NOW - timedelta(hours=24)):
        found.setdefault(f.detector, []).append(f)
    due = [f.title for f in found["due_soon"]]
    assert any("Homework 4" in t for t in due) and not any("Quiz 3" in t for t in due)  # quiz 3 already submitted
    assert all(not f.urgent for f in found["due_soon"])  # HW4 is ~39 h away: 48 h tier, no toast
    clash = [f.title for f in found["conflict"]]
    assert any("Career fair" in t and "CS 5800" in t for t in clash)
    assert not any("office hours" in t.lower() for t in clash)  # optional items never raise clash alerts


def test_finished_work_is_never_reported_as_due(engine):
    for n, status in enumerate(["submitted", "graded", "excused", "done", None]):
        add(engine, id=f"canvas:a:{n}", source="canvas", kind="deadline", title=f"Work {status}",
            due=NOW + timedelta(hours=10), status=status)
    due = [f.title for f in detect(engine, Settings(), NOW, since=NOW) if f.detector == "due_soon"]
    assert len(due) == 1 and "Work None" in due[0]
    assert [v["title"] for v in briefing.build(engine, Settings(), NOW)["due_48h"]] == ["Work None"]


def test_missing_is_only_claimed_when_canvas_says_so(engine):
    add(engine, id="canvas:a:1", source="canvas", kind="deadline", title="Lab 2", due=at(12, 23, 59))
    add(engine, id="canvas:a:2", source="canvas", kind="deadline", title="Lab 3", due=at(12, 23, 59), status="missing")
    found = detect(engine, Settings(), NOW, since=NOW - timedelta(days=1))
    missing = [f for f in found if f.detector == "missing"]
    assert [f.item_ids for f in missing] == [["canvas:a:2"]] and missing[0].urgent


def test_due_within_24h_toasts_once(engine):
    add(engine, id="canvas:a:9", source="canvas", kind="deadline", title="Reading quiz", course="CS 5200",
        due=NOW + timedelta(hours=5))
    s, notes = scheduler(engine)
    s.sync_tick(NOW)
    s.sync_tick(NOW + timedelta(minutes=30))
    assert len(notes.sent) == 1 and "Reading quiz (CS 5200)" in notes.sent[0][1]
    assert notes.sent[0][2] == "http://127.0.0.1:8765"  # clicking opens the dashboard
    [a] = alerts(engine)
    assert a.channel == "toast" and a.notified_at is not None


def test_invite_titles_never_reach_a_toast(engine):
    add(engine, id="routine:l", source="routine", kind="lecture", title="CS 5800 Algorithms - Lecture",
        start=NOW + timedelta(hours=2), end=NOW + timedelta(hours=4))
    add(engine, id="gcal:x", kind="event", trust="untrusted", start=NOW + timedelta(hours=3), end=NOW + timedelta(hours=5),
        title="URGENT: your NEU password expires, verify at evil.example")
    s, notes = scheduler(engine)
    s.sync_tick(NOW)
    text = " ".join(body for _, body, _ in notes.sent)
    assert "Clash" in text and "someone else's invite" in text
    assert "evil.example" not in text and "password" not in text


def test_toasts_are_capped_per_day(engine):
    for n in range(8):
        add(engine, id=f"canvas:a:{n}", source="canvas", kind="deadline", title=f"Task {n}", due=NOW + timedelta(hours=2 + n))
    s, notes = scheduler(engine)
    s.sync_tick(NOW)
    assert len(notes.sent) == alert_store.MAX_TOASTS_PER_DAY
    assert "4 more urgent item(s)" in notes.sent[-1][1]
    assert all(a.notified_at for a in alerts(engine))


def test_notifier_failure_never_raises(monkeypatch):
    import win11toast

    def boom(**_):
        raise RuntimeError("no notification service")

    monkeypatch.setattr(win11toast, "notify", boom)
    assert ToastNotifier().send("Atlas", "hello") is False


# ------------------------------------------------------------------ briefing content

def test_briefing_content_matches_the_store(demo_engine):
    c = briefing.build(demo_engine, Settings(), NOW)
    titles = lambda key: [v["title"] for v in c[key]]  # noqa: E731
    assert "CS 5800 Algorithms - Lecture" in titles("today")
    assert "CS 5800 Homework 4" in titles("due_48h") and "CS 5200 Quiz 3" not in titles("due_48h")
    assert "CS 5200 Project proposal" in titles("due_week")
    assert any({x["a"]["title"], x["b"]["title"]} == {"Career fair", "CS 5800 Algorithms - Lecture"} for x in c["clashes"])
    assert c["heading"] == "Friday, November 13"
    assert "1 due within 48 h" in c["summary"] and "1 clash" in c["summary"]
    assert any("demo items" in x for x in c["caveats"])  # never looks like real data
    text = briefing.render_text(c)
    assert "Due in the next 48 hours:" in text and "CS 5800 Homework 4" in text and "due Sat Nov 14, 11:59 PM" in text


def test_new_announcements_are_those_first_seen_since_last_briefing(demo_engine):
    with Session(demo_engine) as db:
        ann = db.get(Item, "demo:ann-midterm")
        ann.first_seen_at = NOW - timedelta(hours=1)
        db.add(ann)
        db.commit()
    assert [v["id"] for v in briefing.build(demo_engine, Settings(), NOW)["announcements"]] == ["demo:ann-midterm"]
    briefing.generate(demo_engine, Settings(), NOW, trigger="scheduled", daily=True)
    later = briefing.build(demo_engine, Settings(), NOW + timedelta(hours=2))
    assert later["announcements"] == []  # already reported in this morning's briefing


def test_stale_source_is_flagged_in_briefing_and_toasted_once(engine):
    settings = Settings(sources={"canvas": {"enabled": True, "base_url": "https://canvas.example.edu"}})
    with Session(engine) as db:
        db.add(SyncState(source="canvas", status="needs_reauth", last_success_at=NOW - timedelta(days=1),
                         last_error="Canvas rejected the token"))
        db.commit()
    s, notes = scheduler(engine, settings)
    s.sync_tick(NOW)
    s.sync_tick(NOW + timedelta(hours=1))
    assert [b for _, b, _ in notes.sent] == ["canvas: sign-in expired: run `atlas auth canvas`"]
    b = briefing.generate(engine, settings, NOW, trigger="manual", daily=False)
    assert b.stale_sources == ["canvas"] and "stale: canvas" in b.content["summary"]
    [row] = [h for h in b.content["health"] if h["source"] == "canvas"]
    assert row["stale"] and row["last_success"] == "Thu Nov 12 2026 9:00 AM"


def test_toast_points_at_whats_next_not_whats_over(engine):
    add(engine, id="routine:a", source="routine", kind="lecture", title="Morning lecture", start=at(13, 8), end=at(13, 9))
    add(engine, id="routine:b", source="routine", kind="lecture", title="Afternoon lab", start=at(13, 14), end=at(13, 16))
    add(engine, id="gcal:i", kind="event", trust="untrusted", title="Click evil.example", start=at(13, 10), end=at(13, 11))
    c = briefing.build(engine, Settings(), at(13, 9, 30))
    _, body = briefing.toast_text(c, at(13, 9, 30))
    assert "Next: Afternoon lab (2:00 PM - 4:00 PM)" in body
    assert "Morning lecture" not in body and "evil.example" not in body  # over already / someone else's text


def test_sync_warnings_reach_the_briefing(engine):
    from atlas.connectors.routine import EXAMPLE_WARNING

    with Session(engine) as db:
        db.add(SyncState(source="routine", status="ok", last_success_at=NOW, last_error=f"warning: {EXAMPLE_WARNING}"))
        db.commit()
    add(engine, id="canvas:a:1", source="canvas", kind="deadline", title="Lab 9", due=NOW + timedelta(days=3))
    c = briefing.build(engine, Settings(), NOW)
    assert any("still the example timetable" in x for x in c["caveats"])
    assert "Replace it with your real lectures" in briefing.render_text(c)
    # Real-data finding (2026-11-17): the headline said "0 classes · 0 due within 48 h" with no timetable
    # and 3 deadlines later in the week. It must say what it doesn't know and what's coming.
    assert c["summary"] == "timetable not set up · 0 due within 48 h · 1 more this week"


def test_summary_counts_classes_when_timetable_is_synced(engine):
    with Session(engine) as db:
        db.add(SyncState(source="routine", status="ok", last_success_at=NOW))
        db.commit()
    add(engine, id="routine:l", source="routine", kind="lecture", title="CS 5100 Lecture", start=at(13, 13), end=at(13, 14))
    assert briefing.build(engine, Settings(), NOW)["summary"].startswith("1 class · 0 due within 48 h")


def test_feed_mode_caveat(engine):
    settings = Settings(sources={"canvas": {"enabled": True, "mode": "ics"}})
    assert any("can't see submissions" in x for x in briefing.build(engine, settings, NOW)["caveats"])


# ------------------------------------------------------------------ daily timing and catch-up

def test_daily_briefing_timing_and_idempotence(demo_engine):
    notes, synced = RecordingNotifier(), []
    s = Settings()
    assert briefing.ensure_daily(demo_engine, s, at(13, 6, 59), notes, sync=synced.append) is None and not synced
    b = briefing.ensure_daily(demo_engine, s, at(13, 7, 2), notes, sync=synced.append)
    assert b.trigger == "scheduled" and b.daily_key == "2026-11-13" and synced == [at(13, 7, 2)]
    assert notes.sent[-1][0] == "Atlas · Friday, November 13"
    assert briefing.ensure_daily(demo_engine, s, at(13, 7, 7), notes, sync=synced.append) is None
    assert len(synced) == 1  # no extra sync once today's briefing exists


def test_missed_briefing_is_delivered_as_catch_up_after_sleep(demo_engine):
    fetched = []

    class FakeConnector:
        source = "routine"

        def fetch(self, now):
            from atlas.connectors import FetchResult

            fetched.append(now)
            return FetchResult([], [])

    s, notes = scheduler(demo_engine, connectors=[FakeConnector()])
    s.briefing_tick(at(14, 9, 40))  # laptop woke at 9:40 on Saturday; 7:00 was missed
    with Session(demo_engine) as db:
        [b] = db.exec(select(Briefing)).all()
    assert b.trigger == "catch_up" and b.day == "2026-11-14"
    assert fetched == [at(14, 9, 40)]  # synced first, so the briefing isn't built from old data
    assert s.status["briefing"]["trigger"] == "catch_up"


def test_daily_briefing_made_elsewhere_is_not_duplicated(engine):
    with Session(engine) as db:
        db.add(Briefing(day="2026-11-13", daily_key="2026-11-13", trigger="scheduled", content={}))
        db.commit()
    assert briefing.ensure_daily(engine, Settings(), at(13, 8)) is None


def test_scheduler_jobs_coalesce_missed_runs(engine):
    s, _ = scheduler(engine)
    s.start()
    try:
        jobs = {j.id: j for j in s.jobs()}
        assert set(jobs) == {"sync", "briefing"}
        assert all(j.coalesce and j.misfire_grace_time is None and j.max_instances == 1 for j in jobs.values())
    finally:
        s.shutdown()


def test_job_errors_are_recorded_not_raised(engine):
    s, _ = scheduler(engine)
    s.sync_tick = lambda now=None: (_ for _ in ()).throw(RuntimeError("db locked"))
    s._guard("sync", s.sync_tick)()
    assert "db locked" in s.status["sync_error"]["error"]


# ------------------------------------------------------------------ gate KPI and dashboard

def _daily(day, verdict=None, missed=None):
    return Briefing(day=day, daily_key=day, trigger="scheduled", content={}, verdict=verdict, missed_items=missed,
                    created_at=datetime.fromisoformat(day + "T07:00:00").replace(tzinfo=ZONE))


def test_accurate_streak_rules():
    days = [_daily("2026-11-10", "accurate"), _daily("2026-11-11", "accurate"), _daily("2026-11-12", "accurate"),
            _daily("2026-11-13")]  # today not checked yet: doesn't break the streak
    assert accurate_streak(days) == 3
    assert accurate_streak(days[:1] + days[2:]) == 1  # gap on the 11th
    assert accurate_streak(days[:2] + [_daily("2026-11-12", "accurate", missed=1)]) == 0  # something missed
    assert accurate_streak([_daily("2026-11-12", "inaccurate")] + days[:2]) == 0


def test_kpis_count_briefings(engine):
    with Session(engine) as db:
        for b in (_daily("2026-11-11", "accurate"), _daily("2026-11-12", "inaccurate", 2)):
            db.add(b)
        db.commit()
    k = compute(engine, days=7, now=NOW)
    assert k["briefings"]["daily"] == 2 and k["briefings"]["accurate"] == 1 and k["briefings"]["missed_items"] == 2
    assert k["autonomy_distribution"]["L3"] == 2
    assert k["time_saved_hours"] == 0.25  # one morning check (15 min); the inaccurate day earns nothing


def test_dashboard_briefing_flow(demo_engine):
    from atlas.web.app import create_app

    client = TestClient(create_app(engine=demo_engine, settings=Settings()))
    assert client.get("/api/briefing").json() == {"briefing": None}
    b = client.post("/api/briefing").json()["briefing"]
    assert b["content"]["summary"] and b["daily"] is False
    assert client.post(f"/api/briefing/{b['id']}/verdict", json={"verdict": "great"}).status_code == 422
    assert client.post("/api/briefing/nope/verdict", json={"verdict": "accurate"}).status_code == 404
    assert client.post(f"/api/briefing/{b['id']}/verdict", json={"verdict": "inaccurate", "missed": 1,
                                                                 "note": "missed a lab"}).json()["ok"]
    alert_id = b["content"]["alerts"][0]["id"]
    assert client.post(f"/api/alerts/{alert_id}/feedback", json={"useful": True}).json()["ok"]
    assert client.post("/api/alerts/nope/feedback", json={"useful": True}).status_code == 404
    again = client.get("/api/briefing").json()["briefing"]
    assert again["verdict"] == "inaccurate" and again["alert_feedback"][alert_id] == "useful"
    page = client.get("/metrics").text
    assert "accurate days in a row" in page and "Daily briefings" in page
