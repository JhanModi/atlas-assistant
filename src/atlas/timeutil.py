"""Timezone helpers and natural-language date ranges.

Slots such as "next friday" or "this week" are normalized here, deterministically, so the model never
has to do date arithmetic (ADR-0002). `now()` can be frozen with ATLAS_NOW for evals and tests.
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import dateparser

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_WEEKDAY_ABBR = {
    "mon": 0, "tue": 1, "tues": 1, "wed": 2, "thu": 3, "thur": 3, "thurs": 3,
    "fri": 4, "sat": 5, "sun": 6,
}
_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "twenty": 20,
    "thirty": 30,
}
_UNIT_DAYS = {"day": 1, "week": 7}
_LEAD = r"(?:in |over |during |for |within )?(?:the )?"
# "next 7 days", "over the next two weeks", "within 7 days", "the coming week" -> a window starting today
_WINDOW = re.compile(rf"{_LEAD}(?:next|coming|following|upcoming) (\d{{1,3}}) (day|week)s?|within (?:the )?(\d{{1,3}}) (day|week)s?")
_WINDOW_ONE = re.compile(rf"{_LEAD}(?:next|coming|upcoming) (week)")  # "over the coming week" (bare "next week" = calendar week, handled earlier)
# "in 7 days", "7 days from today", "a week from now", "2 weeks later" -> that single day
_OFFSET = re.compile(r"in (\d{1,3}) (day|week)s?(?: from (?:now|today))?|(\d{1,3}) (day|week)s? (?:from (?:now|today)|later|ahead|out)")
_AGO = re.compile(r"(\d{1,3}) (day|week)s? ago")
# dateparser misreads some future phrases as past dates; never accept a past date for these
_FUTURE_CUE = re.compile(r"\b(from now|from today|later|ahead|next|coming|upcoming)\b|^in \d")
HINT = "Use phrases like 'today', 'tomorrow', 'next 7 days', 'next friday', 'this week' or a date like 2026-11-20."


def _normalize(t: str) -> str:
    t = re.sub(r"\s+", " ", t.strip().lower())
    t = re.sub(r"^by ", "", t)  # "by friday" -> "friday"
    t = re.sub(r"\ba couple of\b", "2", t)
    t = re.sub(r"\ba few\b", "3", t)
    t = re.sub(r"\ban? (day|week)\b", r"1 \1", t)  # "a week from today" -> "1 week from today"
    return re.sub(r"\b(" + "|".join(_NUMBER_WORDS) + r")\b", lambda m: str(_NUMBER_WORDS[m.group(1)]), t)


def tz(name: str | None = None) -> ZoneInfo:
    if name is None:
        from atlas.config import get_settings

        name = get_settings().timezone
    return ZoneInfo(name)


def now(tzname: str | None = None) -> datetime:
    """Current local time, or ATLAS_NOW (ISO 8601) when set - used to freeze time in evals/tests."""
    zone = tz(tzname)
    if frozen := os.environ.get("ATLAS_NOW"):
        dt = datetime.fromisoformat(frozen)
        return dt.astimezone(zone) if dt.tzinfo else dt.replace(tzinfo=zone)
    return datetime.now(zone)


def to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("naive datetime; attach a timezone first")
    return dt.astimezone(timezone.utc)


def to_local(dt: datetime, tzname: str | None = None) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz(tzname))


def start_of_day(d: date, zone: ZoneInfo) -> datetime:
    return datetime.combine(d, time.min, tzinfo=zone)


def _weekday_index(word: str) -> int | None:
    word = word.lower().strip().rstrip(".")
    if word in WEEKDAYS:
        return WEEKDAYS.index(word)
    return _WEEKDAY_ABBR.get(word)


def resolve_range(text: str | None, ref: datetime | None = None) -> tuple[datetime, datetime]:
    """Turn a phrase into a [start, end) local datetime range.

    Handles: today, tonight, tomorrow, yesterday, this/next/last week, this weekend, next weekend,
    this/next month, windows ("next 7 days", "over the next two weeks", "within 3 days"), offsets
    ("in 7 days", "a week from today", "2 days ago"), "end of the week/month", weekday names ("friday",
    "next tue"), ISO dates, "A to B" ranges, and anything dateparser understands (treated as a single day).
    Empty -> next 7 days.
    """
    ref = ref or now()
    zone = ref.tzinfo if isinstance(ref.tzinfo, ZoneInfo) else tz()
    today = ref.date()
    t = _normalize(text or "")

    def day(d: date, days: int = 1) -> tuple[datetime, datetime]:
        s = start_of_day(d, zone)
        return s, s + timedelta(days=days)

    if not t or t in {"upcoming", "soon", "coming up", "next few days"}:
        return day(today, 7)
    if t in {"today", "tonight", "now"}:
        return day(today)
    if t == "tomorrow":
        return day(today + timedelta(days=1))
    if t == "yesterday":
        return day(today - timedelta(days=1))

    monday = today - timedelta(days=today.weekday())
    if t in {"this week", "the week", "week"}:
        return day(monday, 7)
    if t == "next week":
        return day(monday + timedelta(days=7), 7)
    if t == "last week":
        return day(monday - timedelta(days=7), 7)
    if t in {"this weekend", "weekend", "the weekend"}:
        return day(monday + timedelta(days=5), 2)
    if t == "next weekend":
        return day(monday + timedelta(days=12), 2)
    if t in {"this month", "month"}:
        first = today.replace(day=1)
        nxt = (first + timedelta(days=32)).replace(day=1)
        return start_of_day(first, zone), start_of_day(nxt, zone)
    if t == "next month":
        first = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
        nxt = (first + timedelta(days=32)).replace(day=1)
        return start_of_day(first, zone), start_of_day(nxt, zone)

    if m := _WINDOW.fullmatch(t):
        n, unit = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        return day(today, int(n) * _UNIT_DAYS[unit])
    if m := _WINDOW_ONE.fullmatch(t):
        return day(today, _UNIT_DAYS[m.group(1)])
    if m := _OFFSET.fullmatch(t):
        n, unit = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        return day(today + timedelta(days=int(n) * _UNIT_DAYS[unit]))
    if m := _AGO.fullmatch(t):
        return day(today - timedelta(days=int(m.group(1)) * _UNIT_DAYS[m.group(2)]))
    if m := re.fullmatch(r"(?:the )?end of (the |this |next )?(week|month|day|today)", t):
        which, unit = (m.group(1) or "").strip(), m.group(2)
        if unit in {"day", "today"}:
            return day(today)
        if unit == "week":
            return day(monday + timedelta(days=13 if which == "next" else 6))
        first = today.replace(day=1)
        if which == "next":
            first = (first + timedelta(days=32)).replace(day=1)
        return day((first + timedelta(days=32)).replace(day=1) - timedelta(days=1))

    if m := re.fullmatch(r"(this |next |coming |on )?([a-z]+)", t):
        idx = _weekday_index(m.group(2))
        if idx is not None:
            ahead = (idx - today.weekday()) % 7
            if m.group(1) == "next " and ahead == 0:
                ahead = 7
            return day(today + timedelta(days=ahead))

    for sep in (" to ", " until ", " through ", " - "):
        if sep in t:
            a, b = t.split(sep, 1)
            a_start, _ = resolve_range(a, ref)
            _, b_end = resolve_range(b, ref)
            if b_end <= a_start:  # "monday to friday" said on a Friday -> the Friday after that Monday
                _, b_end = resolve_range(b, a_start)
            if b_end > a_start:
                return a_start, b_end

    parsed = dateparser.parse(
        t,
        settings={
            "TIMEZONE": str(zone),
            "RETURN_AS_TIMEZONE_AWARE": True,
            "PREFER_DATES_FROM": "future",
            "RELATIVE_BASE": ref.replace(tzinfo=None),
        },
    )
    if parsed is None:
        raise ValueError(f"could not understand the date/time range: {text!r}. {HINT}")
    parsed_day = parsed.astimezone(zone).date()
    if parsed_day < today and _FUTURE_CUE.search(t):
        raise ValueError(f"ambiguous date/time range: {text!r}. {HINT}")
    return day(parsed_day)


def fmt_local(dt: datetime | None, tzname: str | None = None) -> str | None:
    if dt is None:
        return None
    return to_local(dt, tzname).strftime("%a %b %d %Y %I:%M %p").replace(" 0", " ")
