from datetime import date

import pytest

from atlas import timeutil


def days(text):
    s, e = timeutil.resolve_range(text)
    return s.date(), e.date()


@pytest.mark.parametrize(
    "text, start, end",
    [
        ("today", date(2026, 11, 13), date(2026, 11, 14)),
        ("tomorrow", date(2026, 11, 14), date(2026, 11, 15)),
        ("this week", date(2026, 11, 9), date(2026, 11, 16)),
        ("next week", date(2026, 11, 16), date(2026, 11, 23)),
        ("this weekend", date(2026, 11, 14), date(2026, 11, 16)),
        ("friday", date(2026, 11, 13), date(2026, 11, 14)),  # today is Friday
        ("next friday", date(2026, 11, 20), date(2026, 11, 21)),
        ("thu", date(2026, 11, 19), date(2026, 11, 20)),
        ("next 3 days", date(2026, 11, 13), date(2026, 11, 16)),
        ("this month", date(2026, 11, 1), date(2026, 12, 1)),
        ("next month", date(2026, 12, 1), date(2027, 1, 1)),
        ("2026-11-20", date(2026, 11, 20), date(2026, 11, 21)),
        ("Nov 20", date(2026, 11, 20), date(2026, 11, 21)),
        ("monday to friday", date(2026, 11, 16), date(2026, 11, 21)),
        ("", date(2026, 11, 13), date(2026, 11, 20)),
        # Windows starting today (benchmark finding: models phrase "next 7 days" many ways)
        ("next seven days", date(2026, 11, 13), date(2026, 11, 20)),
        ("in the next 7 days", date(2026, 11, 13), date(2026, 11, 20)),
        ("over the next two weeks", date(2026, 11, 13), date(2026, 11, 27)),
        ("next 2 weeks", date(2026, 11, 13), date(2026, 11, 27)),
        ("within 3 days", date(2026, 11, 13), date(2026, 11, 16)),
        ("the coming week", date(2026, 11, 13), date(2026, 11, 20)),
        # Single-day offsets (dateparser read "7 days from today" as today, "a week from today" as last week)
        ("7 days from today", date(2026, 11, 20), date(2026, 11, 21)),
        ("a week from today", date(2026, 11, 20), date(2026, 11, 21)),
        ("in 7 days", date(2026, 11, 20), date(2026, 11, 21)),
        ("in two weeks", date(2026, 11, 27), date(2026, 11, 28)),
        ("3 days later", date(2026, 11, 16), date(2026, 11, 17)),
        ("2 days ago", date(2026, 11, 11), date(2026, 11, 12)),
        ("end of the week", date(2026, 11, 15), date(2026, 11, 16)),
        ("end of next week", date(2026, 11, 22), date(2026, 11, 23)),
        ("end of the month", date(2026, 11, 30), date(2026, 12, 1)),
        ("by friday", date(2026, 11, 13), date(2026, 11, 14)),
    ],
)
def test_resolve_range(text, start, end):
    assert days(text) == (start, end)


def test_deadlines_for_seven_days_from_today_include_tomorrow():
    # Regression: qwen3:8b passed end="7 days from today"; the range collapsed to today and it said "nothing due".
    from atlas.agent.slots import deadline_range

    s, e = deadline_range("today", "7 days from today")
    assert (s.date(), e.date()) == (date(2026, 11, 13), date(2026, 11, 21))


def test_error_message_suggests_phrasings():
    with pytest.raises(ValueError, match="next 7 days"):
        timeutil.resolve_range("purple elephants")


def test_month_word_is_not_monday():
    assert days("this month")[0] == date(2026, 11, 1)


def test_unknown_phrase_raises():
    with pytest.raises(ValueError):
        timeutil.resolve_range("purple elephants")


def test_ranges_are_timezone_aware_new_york():
    s, _ = timeutil.resolve_range("today")
    assert s.tzinfo is not None and str(s.tzinfo) == "America/New_York"


def test_dst_boundary_day_is_25_hours():
    # 2026-11-01 is the US fall-back day: the local day is 25 real hours long. (Python subtracts same-zone
    # datetimes by wall clock, so compare the UTC instants that the database queries actually use.)
    s, e = timeutil.resolve_range("2026-11-01")
    assert (timeutil.to_utc(e) - timeutil.to_utc(s)).total_seconds() == 25 * 3600
