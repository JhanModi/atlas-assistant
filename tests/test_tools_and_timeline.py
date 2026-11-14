from datetime import datetime, timedelta, timezone

from atlas.agent.slots import canonical, slot_score
from atlas.agent.tools import make_tools
from atlas.store import Item
from atlas.timeline import find_overlaps


def tool(engine, name):
    return next(t for t in make_tools(engine) if t.tool_name == name)


def call(engine, name, **kwargs):
    return tool(engine, name)(**kwargs)


def ev(i, start_h, end_h, **kw):
    base = datetime(2026, 11, 17, tzinfo=timezone.utc)
    return Item(id=f"t:{i}", source="t", source_id=str(i), kind=kw.pop("kind", "event"), title=f"E{i}",
                start=base + timedelta(hours=start_h), end=base + timedelta(hours=end_h), **kw)


def test_overlaps_detected_and_sorted():
    overlaps = find_overlaps([ev(1, 10, 12), ev(2, 11, 13), ev(3, 13, 14)])
    assert [(o.a.id, o.b.id, o.minutes) for o in overlaps] == [("t:1", "t:2", 60)]


def test_back_to_back_is_not_a_conflict():
    assert find_overlaps([ev(1, 10, 11), ev(2, 11, 12)]) == []


def test_cancelled_and_all_day_ignored():
    assert find_overlaps([ev(1, 10, 12, status="cancelled"), ev(2, 11, 13)]) == []
    assert find_overlaps([ev(1, 10, 12, all_day=True), ev(2, 11, 13)]) == []


def test_missing_end_defaults_to_one_hour():
    a = ev(1, 10, 10)  # end == start -> 1h default
    assert len(find_overlaps([a, ev(2, 10.5, 12)])) == 1


def test_get_deadlines_excludes_submitted(demo_engine):
    out = call(demo_engine, "get_deadlines", start="today", end="")
    titles = [i["title"] for i in out["items"]]
    assert "CS 5800 Homework 4" in titles and "CS 5200 Project proposal" in titles
    assert "CS 5200 Quiz 3" not in titles  # submitted
    with_done = call(demo_engine, "get_deadlines", start="today", end="", include_submitted=True)
    assert "CS 5200 Quiz 3" in [i["title"] for i in with_done["items"]]


def test_get_deadlines_course_filter(demo_engine):
    out = call(demo_engine, "get_deadlines", start="today", end="next 14 days", course="cs 5200")
    assert {i["course"] for i in out["items"]} == {"CS 5200"}


def test_find_conflicts_finds_career_fair_clash(demo_engine):
    out = call(demo_engine, "find_conflicts", start="next week", end="")
    pairs = [{c["a"]["title"], c["b"]["title"]} for c in out["conflicts"]]
    assert {"Career fair", "CS 5800 Algorithms - Lecture"} in pairs


def test_get_routine_tuesday(demo_engine):
    out = call(demo_engine, "get_routine", day="tuesday")
    assert [i["title"] for i in out["items"]] == ["CS 5800 Algorithms - Lecture"]
    assert out["items"][0]["start"].startswith("2026-11-17T11:45")


def test_search_wraps_untrusted_text(demo_engine):
    out = call(demo_engine, "search_items", query="midterm")
    ann = next(i for i in out["items"] if i["kind"] == "announcement")
    assert ann["text"].startswith("<untrusted_content>")


def test_when_text_is_ready_to_quote(demo_engine):
    out = call(demo_engine, "get_deadlines", start="today", end="")
    hw4 = next(i for i in out["items"] if i["title"] == "CS 5800 Homework 4")
    assert hw4["when"] == "due Sat Nov 14, 11:59 PM (tomorrow)"
    lec = call(demo_engine, "get_routine", day="tuesday")["items"][0]
    assert lec["when"] == "Tue Nov 17, 11:45 AM - 1:25 PM (in 4 days)"


def test_times_are_local(demo_engine):
    out = call(demo_engine, "get_agenda", start="tuesday", end="")
    assert all(i["start"].endswith("-05:00") for i in out["items"] if "start" in i)


def test_slot_scoring():
    assert slot_score("get_deadlines", {"start": "this week"}, {"start": "this week"}) == (2, 2)
    assert slot_score("get_deadlines", {"start": "this week"}, {"start": "next week"}) == (0, 2)
    # Equivalent phrasing resolves to the same range
    assert slot_score("get_agenda", {"start": "tomorrow"}, {"start": "2026-11-14"}) == (2, 2)
    assert slot_score("get_deadlines", {"course": "CS 5800"}, {"course": "cs5800"}) == (1, 1)
    assert slot_score("search_items", {"query": "career fair"}, {"query": "Career Fair event"}) == (1, 1)
    # No date pinned in the golden case -> date slots not scored
    assert slot_score("get_deadlines", {}, {"start": "next week"}) == (0, 0)


def test_canonical_rejects_garbage():
    import pytest

    from atlas.security.validate import ToolInputError

    with pytest.raises(ToolInputError):
        canonical("get_agenda", {"start": "purple elephants"})
