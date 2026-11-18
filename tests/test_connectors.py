"""Phase 2 connectors on recorded-style fixtures (no live calls): routine, ICS feeds, Canvas API, sync rules."""

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import respx
from sqlmodel import Session, select

from atlas import timeutil
from atlas.config import SyncSettings
from atlas.connectors import sync_source
from atlas.connectors.canvas import CanvasConnector, submission_status
from atlas.connectors.ics_feed import IcsFeedConnector
from atlas.connectors.routine import RoutineConnector
from atlas.security.sanitize import safe_error
from atlas.store import Item, SyncState, make_engine
from atlas.vault import MemoryStore

FIX = Path(__file__).parent / "fixtures"
ROOT = Path(__file__).resolve().parents[1]
SYNC = SyncSettings()
BASE = "https://canvas.example.edu"
FEED = "https://canvas.example.edu/feeds/calendars/user_SECRETfeedTOKEN123.ics"


def fixture(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def items(engine, source=None):
    with Session(engine) as db:
        stmt = select(Item) if source is None else select(Item).where(Item.source == source)
        return {i.id: i for i in db.exec(stmt).all()}


def state(engine, source):
    with Session(engine) as db:
        return db.get(SyncState, source)


def local(dt):
    return timeutil.to_local(dt)


# ------------------------------------------------------------------ routine

@pytest.fixture
def routine_file(tmp_path):
    """The example timetable, edited slightly (an unedited copy is deliberately ignored)."""
    p = tmp_path / "routine.yaml"
    text = (ROOT / "config" / "routine.example.yaml").read_text(encoding="utf-8")
    p.write_text(text.replace('name: "Fall 2026"', 'name: "Fall 2026 (test copy)"'), encoding="utf-8")
    return p


def test_unedited_example_timetable_is_ignored_and_its_sessions_removed(engine, tmp_path):
    p = tmp_path / "routine.yaml"
    shutil.copy(ROOT / "config" / "routine.example.yaml", p)
    edited = tmp_path / "edited.yaml"
    edited.write_text(p.read_text(encoding="utf-8").replace("Fall 2026", "Fall 2026 (edited)"), encoding="utf-8")
    sync_source(engine, RoutineConnector(edited, SYNC), SYNC)  # earlier sync stored sessions
    assert items(engine, "routine")
    r = sync_source(engine, RoutineConnector(p, SYNC), SYNC, now=timeutil.now() + timedelta(minutes=1))
    assert r.status == "ok" and "still the example timetable" in r.error
    assert items(engine, "routine") == {}  # fictional classes never reach a briefing
    assert state(engine, "routine").last_error.startswith("warning:")


def test_routine_expands_semester_with_skips(engine, routine_file):
    r = sync_source(engine, RoutineConnector(routine_file, SYNC), SYNC)
    assert r.status == "ok"
    got = list(items(engine, "routine").values())
    algo = sorted(local(i.start).date().isoformat() for i in got if i.course == "CS 5800" and i.kind == "lecture")
    # Tue/Fri from Fri Nov 13; Thanksgiving Fri Nov 27 skipped; semester ends Dec 12
    assert algo == ["2026-11-13", "2026-11-17", "2026-11-20", "2026-11-24", "2026-12-01", "2026-12-04",
                    "2026-12-08", "2026-12-11"]
    dbms = [local(i.start).date().isoformat() for i in got if i.course == "CS 5200"]
    assert "2026-11-25" not in dbms and len(dbms) == 7
    tue = next(i for i in got if local(i.start).date().isoformat() == "2026-11-17" and i.course == "CS 5800")
    assert local(tue.start).strftime("%H:%M") == "11:45" and local(tue.end).strftime("%H:%M") == "13:25"
    assert all(i.status == "optional" for i in got if i.kind == "office_hours")
    assert any(i.title == "Career fair" and i.kind == "event" for i in got)


def test_routine_accepts_unquoted_times(engine, tmp_path):
    p = tmp_path / "r.yaml"
    p.write_text("recurring:\n  - title: Lab\n    days: [tue]\n    start: 11:45\n    end: 13:25\n", encoding="utf-8")
    sync_source(engine, RoutineConnector(p, SYNC), SYNC)
    lab = next(iter(items(engine, "routine").values()))
    assert local(lab.start).strftime("%a %H:%M") == "Tue 11:45"


def test_routine_errors_are_clear_and_keep_old_data(engine, routine_file):
    sync_source(engine, RoutineConnector(routine_file, SYNC), SYNC)
    before = len(items(engine, "routine"))
    routine_file.write_text("recurring:\n  - title: Bad\n    days: [funday]\n    start: '10:00'\n    end: '11:00'\n",
                            encoding="utf-8")
    r = sync_source(engine, RoutineConnector(routine_file, SYNC), SYNC, force=True)
    assert r.status == "error" and "unknown day 'funday'" in r.error
    assert len(items(engine, "routine")) == before  # a broken file never wipes the timetable


def test_routine_missing_file_is_not_configured(engine, tmp_path):
    r = sync_source(engine, RoutineConnector(tmp_path / "nope.yaml", SYNC), SYNC)
    assert r.status == "not_configured" and "routine.example.yaml" in r.error


# ------------------------------------------------------------------ ICS feeds

@respx.mock
def test_canvas_feed_maps_assignments_and_events(engine):
    respx.get(FEED).mock(return_value=httpx.Response(200, content=(FIX / "canvas_feed.ics").read_bytes()))
    r = sync_source(engine, IcsFeedConnector("canvas", FEED, SYNC, "canvas", "atlas auth canvas --ics"), SYNC)
    assert r.status == "ok", r.error
    got = {i.title: i for i in items(engine, "canvas").values()}
    hw = got["Homework 5"]
    assert hw.kind == "deadline" and hw.course == "CS 5800 Algorithms" and hw.status is None
    assert local(hw.due).strftime("%a %b %d %H:%M") == "Tue Nov 17 23:59"
    assert hw.body == "Submit a PDF."  # HTML stripped
    quiz = got["Reading quiz"]  # date-only assignment -> due at end of that day
    assert quiz.course == "CS 5200" and local(quiz.due).strftime("%b %d %H:%M") == "Nov 19 23:59"
    demo = got["Project demo"]
    assert demo.kind == "event" and demo.location == "Room 5"
    assert "Far future assignment" not in got  # outside the sync window


@respx.mock
def test_generic_feed_expands_recurrence_and_marks_invites_untrusted(engine):
    url = "https://calendar.google.com/calendar/ical/me%40example.com/private-abc123/basic.ics"
    respx.get(url).mock(return_value=httpx.Response(200, content=(FIX / "google_feed.ics").read_bytes()))
    r = sync_source(engine, IcsFeedConnector("gcal", url, SYNC, "generic", "atlas auth gcal --ics"), SYNC)
    assert r.status == "ok", r.error
    got = list(items(engine, "gcal").values())
    study = sorted(local(i.start).strftime("%a %b %d %H:%M") for i in got if i.title == "Study group")
    assert study == ["Mon Nov 16 17:00", "Mon Nov 23 17:00", "Mon Nov 30 17:00"]
    invite = next(i for i in got if i.title.startswith("Call with recruiter"))
    assert invite.trust == "untrusted"
    assert next(i for i in got if i.title == "Cancelled coffee").status == "cancelled"
    hack = next(i for i in got if i.title == "Hackathon")
    assert hack.all_day and (hack.end - hack.start) == timedelta(days=1)


@respx.mock
def test_feed_errors_never_leak_the_secret_url(engine):
    respx.get(FEED).mock(return_value=httpx.Response(404))
    r = sync_source(engine, IcsFeedConnector("canvas", FEED, SYNC, "canvas", "atlas auth canvas --ics"), SYNC)
    assert r.status == "needs_reauth"
    stored = state(engine, "canvas").last_error
    assert "SECRETfeedTOKEN123" not in stored and "http" not in stored


@respx.mock
def test_feed_refuses_redirect_to_http(engine):
    respx.get(FEED).mock(return_value=httpx.Response(302, headers={"Location": "http://plain.example/feed.ics"}))
    respx.get("http://plain.example/feed.ics").mock(return_value=httpx.Response(200, content=b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n"))
    r = sync_source(engine, IcsFeedConnector("canvas", FEED, SYNC, "canvas", "x"), SYNC)
    assert r.status == "error" and "non-https" in r.error


# ------------------------------------------------------------------ Canvas API

def test_course_labels_from_real_northeastern_codes():
    from atlas.connectors.canvas import course_label

    assert course_label({"course_code": "CS5100.17342.202710", "name": "CS5100 17342 Foundations AI SEC 01"}) == "CS 5100"
    assert course_label({"course_code": "CS5010.MERGED.202710"}) == "CS 5010"
    assert course_label({"course_code": "EXED6000.24365.202710"}) == "EXED 6000"
    assert course_label({"course_code": "CS 5800"}) == "CS 5800"
    assert course_label({"course_code": "Masters.ProfDoc.Orientation",
                         "name": "Master's and Professional Doctorate Orientation"}) == \
        "Master's and Professional Doctorate Orientation"  # not a subject+number code: keep the readable name
    assert course_label({"id": 7}) == "7"


def test_course_filter_ignores_spacing_and_punctuation(engine):
    from atlas.agent.tools import make_tools

    with Session(engine) as db:
        db.add(Item(id="canvas:assignment:1", source="canvas", source_id="assignment:1", kind="deadline",
                    title="Lab 9: Adapter", course="CS 5010", due=datetime(2026, 11, 14, 2, tzinfo=timezone.utc)))
        db.add(Item(id="canvas:assignment:2", source="canvas", source_id="assignment:2", kind="deadline",
                    title="Assignment 3", course="CS 5100", due=datetime(2026, 11, 14, 4, tzinfo=timezone.utc)))
        db.commit()
    get_deadlines = next(t for t in make_tools(engine) if t.tool_name == "get_deadlines")
    for asked in ("CS5010", "cs 5010", "CS-5010", "CS 5010"):
        assert [i["title"] for i in get_deadlines(start="today", end="", course=asked)["items"]] == ["Lab 9: Adapter"]


def test_redo_request_reopens_graded_work():
    p = {"submissions": {"submitted": True, "graded": True, "redo_request": True}}
    assert submission_status(p) == "redo_requested"
    from atlas.agent.tools import DONE_STATUSES

    assert "redo_requested" not in DONE_STATUSES  # so it is reported as due again


@respx.mock
def test_canvas_client_refuses_anything_but_get(engine):
    from atlas.connectors.base import SourceError, make_client
    from atlas.connectors.canvas import read_only

    write = respx.post(host="canvas.example.edu").mock(return_value=httpx.Response(200))
    with make_client(base_url=BASE, event_hooks={"request": [read_only]}) as c:
        with pytest.raises(SourceError, match="only reads"):
            c.post("/api/v1/courses/1/assignments/2/submissions", json={})
    assert write.call_count == 0  # blocked before leaving the machine
    canvas_routes()
    seen = {}

    def factory(**kwargs):  # the connector must install the guard on the client it really uses
        seen.update(kwargs)
        return make_client(**kwargs)

    sync_source(engine, CanvasConnector(BASE, MemoryStore({"canvas.token": "t"}), SYNC, client_factory=factory), SYNC)
    assert read_only in seen["event_hooks"]["request"]
    assert {call.request.method for call in respx.calls} == {"GET"}


def test_submission_status_mapping():
    assert submission_status({"submissions": {"graded": True}}) == "graded"
    assert submission_status({"submissions": {"needs_grading": True}}) == "submitted"
    assert submission_status({"submissions": {"missing": True}}) == "missing"
    assert submission_status({"submissions": {"excused": True, "missing": True}}) == "excused"
    assert submission_status({"submissions": False, "planner_override": {"marked_complete": True}}) == "done"
    assert submission_status({"submissions": False}) is None


def canvas_routes(planner_pages=None, announcements=None, courses_status=200):
    pages = planner_pages or [fixture("canvas_planner_page1.json"), fixture("canvas_planner_page2.json")]
    courses = respx.get(host="canvas.example.edu", path="/api/v1/courses").mock(
        return_value=httpx.Response(courses_status, json=[{"id": 101, "name": "Algorithms", "course_code": "CS 5800"},
                                                          {"id": 102, "name": "DBMS", "course_code": "CS 5200"}]))

    def planner(request):
        page = int(request.url.params.get("page", "1"))
        headers = {}
        if page < len(pages):
            headers["Link"] = f'<{BASE}/api/v1/planner/items?page={page + 1}&per_page=100>; rel="next"'
        return httpx.Response(200, json=pages[page - 1], headers=headers)

    respx.get(host="canvas.example.edu", path="/api/v1/planner/items").mock(side_effect=planner)
    respx.get(host="canvas.example.edu", path="/api/v1/announcements").mock(
        return_value=announcements or httpx.Response(200, json=fixture("canvas_announcements.json")))
    return courses


def canvas(token="1234~" + "a" * 64):
    return CanvasConnector(BASE, MemoryStore({"canvas.token": token}), SYNC)


@respx.mock
def test_canvas_api_full_sync(engine):
    canvas_routes()
    r = sync_source(engine, canvas(), SYNC)
    assert r.status == "ok", r.error
    got = {i.title: i for i in items(engine, "canvas").values()}
    hw = got["Homework 4"]
    assert hw.kind == "deadline" and hw.course == "CS 5800" and hw.status is None
    assert local(hw.due).strftime("%a %b %d %H:%M") == "Sat Nov 14 23:59"
    assert hw.url == f"{BASE}/courses/101/assignments/501"
    assert got["Quiz 3"].status == "graded"
    assert got["Reading response"].status == "done"
    assert got["Week 11 discussion"].kind == "deadline"
    assert got["Lab 3 late"].status == "missing"  # bidi override and <b> stripped from the title
    assert got["Midterm review session"].kind == "event"
    assert got["Midterm review session"].location == "West Village H 108"
    assert got["Email advisor about spring courses"].course is None
    assert "Duplicate of an announcement" not in got and "Undated extra credit" not in got
    ann = got["Midterm review moved"]
    assert ann.kind == "announcement" and ann.trust == "untrusted" and ann.course == "CS 5800"
    assert "<" not in ann.body and "alert" not in ann.body and "Monday" in ann.body
    assert "evil.example" not in got["Project teams"].body  # remote image dropped, never stored
    assert len(got) == 9


@respx.mock
def test_canvas_removes_deleted_items_but_keeps_items_outside_window(engine):
    canvas_routes()
    sync_source(engine, canvas(), SYNC)
    far = Item(id="canvas:assignment:far", source="canvas", source_id="assignment:far", kind="deadline",
               title="Final paper", due=datetime(2027, 1, 20, 5, tzinfo=timezone.utc))
    with Session(engine) as db:
        db.add(far)
        db.commit()
    first_seen = items(engine)["canvas:assignment:501"].first_seen_at
    respx.reset()
    page1 = [p for p in fixture("canvas_planner_page1.json") if p["plannable_id"] != "503"]  # deleted upstream
    canvas_routes(planner_pages=[page1, fixture("canvas_planner_page2.json")])
    r = sync_source(engine, canvas(), SYNC, now=timeutil.now() + timedelta(minutes=30))
    assert r.removed == 1 and r.added == 0
    got = items(engine, "canvas")
    assert "canvas:assignment:503" not in got and "canvas:assignment:far" in got
    assert got["canvas:assignment:501"].first_seen_at == first_seen  # unchanged items keep their first-seen time


@respx.mock
def test_canvas_expired_token_parks_source_and_keeps_data(engine):
    canvas_routes()
    sync_source(engine, canvas(), SYNC)
    before = set(items(engine, "canvas"))
    respx.reset()
    courses = canvas_routes(courses_status=401)
    r = sync_source(engine, canvas(), SYNC)
    assert r.status == "needs_reauth" and "atlas auth canvas" in r.error
    assert set(items(engine, "canvas")) == before  # last good data kept
    again = sync_source(engine, canvas(), SYNC)
    assert again.skipped and courses.call_count == 1  # no retry loop: parked until re-auth


@respx.mock
def test_canvas_rate_limit_backs_off(engine):
    respx.get(host="canvas.example.edu", path="/api/v1/courses").mock(return_value=httpx.Response(429))
    now = timeutil.now()
    r1 = sync_source(engine, canvas(), SYNC, now=now)
    s1 = state(engine, "canvas")
    assert r1.status == "rate_limited" and s1.next_attempt_at == now + timedelta(minutes=30)
    assert sync_source(engine, canvas(), SYNC, now=now + timedelta(minutes=10)).skipped
    sync_source(engine, canvas(), SYNC, now=now + timedelta(minutes=31))
    s2 = state(engine, "canvas")
    assert s2.consecutive_failures == 2 and s2.next_attempt_at == now + timedelta(minutes=31 + 60)


@respx.mock
def test_canvas_low_quota_stops_before_throttling(engine):
    respx.get(host="canvas.example.edu", path="/api/v1/courses").mock(
        return_value=httpx.Response(200, json=[], headers={"X-Rate-Limit-Remaining": "5.0"}))
    assert sync_source(engine, canvas(), SYNC).status == "rate_limited"


@respx.mock
def test_canvas_refuses_to_follow_pagination_to_another_host(engine):
    respx.get(host="canvas.example.edu", path="/api/v1/courses").mock(return_value=httpx.Response(
        200, json=[{"id": 1}], headers={"Link": '<https://evil.example/steal?page=2>; rel="next"'}))
    stolen = respx.get(host="evil.example").mock(return_value=httpx.Response(200, json=[]))
    r = sync_source(engine, canvas(), SYNC)
    assert r.status == "error" and "another host" in r.error and stolen.call_count == 0


@respx.mock
def test_canvas_announcement_failure_is_a_warning_not_a_wipe(engine):
    canvas_routes()
    sync_source(engine, canvas(), SYNC)
    respx.reset()
    canvas_routes(announcements=httpx.Response(403, json={"errors": [{"message": "unauthorized"}]}))
    r = sync_source(engine, canvas(), SYNC, now=timeutil.now() + timedelta(minutes=30))
    assert r.status == "ok" and "announcements skipped" in r.error
    assert "canvas:announcement:9001" in items(engine, "canvas")  # not covered this time, so kept
    assert state(engine, "canvas").last_error.startswith("warning:")


def test_canvas_requires_https_base_url(engine):
    c = CanvasConnector("http://canvas.example.edu", MemoryStore({"canvas.token": "x"}), SYNC)
    assert sync_source(engine, c, SYNC).status == "not_configured"


def test_canvas_without_token_is_not_configured(engine):
    r = sync_source(engine, CanvasConnector(BASE, MemoryStore(), SYNC), SYNC)
    assert r.status == "not_configured" and "atlas auth canvas" in r.error


# ------------------------------------------------------------------ sync rules and storage

def test_safe_error_strips_urls_and_tokens():
    msg = safe_error(f"GET {FEED} failed with token 1234~{'b' * 64}")
    assert "SECRETfeedTOKEN123" not in msg and "1234~" not in msg and "[url]" in msg


def test_dry_run_saves_nothing(engine, routine_file):
    r = sync_source(engine, RoutineConnector(routine_file, SYNC), SYNC, dry_run=True)
    assert r.total > 0 and r.items and items(engine) == {} and state(engine, "routine") is None


def test_backoff_is_capped(engine):
    class Broken:
        source = "x"

        def fetch(self, now):
            raise httpx.ConnectError("boom https://secret.example/feed?token=abc")

    now = timeutil.now()
    for _ in range(10):
        r = sync_source(engine, Broken(), SYNC, now=now, force=True)
    s = state(engine, "x")
    assert r.error == "network error (ConnectError)" and "secret" not in s.last_error
    assert s.next_attempt_at - now == timedelta(hours=SYNC.max_backoff_hours)


def test_migration_adds_new_columns_to_an_existing_database(tmp_path):
    import sqlite3

    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE syncstate (source VARCHAR PRIMARY KEY, cursor VARCHAR, last_success_at DATETIME, "
                "last_error VARCHAR, last_duration_ms INTEGER)")
    con.execute("INSERT INTO syncstate (source) VALUES ('canvas')")
    con.commit()
    con.close()
    engine = make_engine(f"sqlite:///{db.as_posix()}")
    with Session(engine) as s:
        old = s.get(SyncState, "canvas")
    assert old.status == "never" and old.consecutive_failures == 0
