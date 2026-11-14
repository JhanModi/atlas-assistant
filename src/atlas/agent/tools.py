"""The single agent's read tools (ADR-0003). Each returns structured JSON from the local store.

The tool the model picks is the *intent*; its arguments are the *slots*. Date phrases are passed through
as the user said them and resolved deterministically by atlas.timeutil (via security.validate).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import or_
from sqlalchemy.engine import Engine
from sqlmodel import Session, and_, col, select
from strands import tool

from atlas import timeutil
from atlas.agent import slots
from atlas.security import validate
from atlas.security.sanitize import wrap_untrusted
from atlas.store.models import Item
from atlas.timeline import find_overlaps

MAX_RESULTS = 25
DONE_STATUSES = {"submitted", "graded", "done", "completed"}

TOOL_NAMES = ["get_agenda", "get_deadlines", "find_conflicts", "get_routine", "search_items"]


def _iso(dt: datetime | None) -> str | None:
    return timeutil.to_local(dt).isoformat(timespec="minutes") if dt else None


def _clock(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")


def _relative(dt: datetime) -> str:
    days = (dt.date() - timeutil.now().date()).days
    if days == 0:
        return "today"
    if days == 1:
        return "tomorrow"
    if days == -1:
        return "yesterday"
    return f"in {days} days" if days > 0 else f"{-days} days ago"


def when_text(item: Item) -> str | None:
    """Ready-to-quote date text, so the model never computes weekdays or dates itself (small models get them wrong)."""
    if item.due is not None:
        d = timeutil.to_local(item.due)
        return f"due {d:%a %b} {d.day}, {_clock(d)} ({_relative(d)})"
    if item.start is not None:
        s = timeutil.to_local(item.start)
        if item.all_day:
            return f"{s:%a %b} {s.day}, all day ({_relative(s)})"
        span = f"{_clock(s)}"
        if item.end is not None:
            span += f" - {_clock(timeutil.to_local(item.end))}"
        return f"{s:%a %b} {s.day}, {span} ({_relative(s)})"
    return None


def item_view(item: Item, include_body: bool = False) -> dict[str, Any]:
    view: dict[str, Any] = {
        "id": item.id,
        "title": item.title,
        "kind": item.kind,
        "source": item.source,
    }
    if (when := when_text(item)) is not None:
        view["when"] = when
    for key, value in (
        ("course", item.course),
        ("start", _iso(item.start)),
        ("end", _iso(item.end)),
        ("due", _iso(item.due)),
        ("all_day", item.all_day or None),
        ("location", item.location),
        ("status", item.status),
    ):
        if value is not None:
            view[key] = value
    if include_body and item.body:
        view["text"] = wrap_untrusted(item.body)
    return view


def _range_meta(start: datetime, end: datetime) -> dict[str, str]:
    return {"from": start.isoformat(timespec="minutes"), "to": end.isoformat(timespec="minutes")}


def _result(items: list[Item], start: datetime, end: datetime, include_body: bool = False, **extra: Any) -> dict:
    return {
        "range": _range_meta(start, end),
        "count": len(items),
        "items": [item_view(i, include_body) for i in items[:MAX_RESULTS]],
        "truncated": len(items) > MAX_RESULTS,
        **extra,
    }


def make_tools(engine: Engine) -> list:
    """Build the tool functions bound to a database engine."""

    def session() -> Session:
        return Session(engine, expire_on_commit=False)

    @tool
    def get_agenda(start: str = "today", end: str = "") -> dict:
        """List EVERYTHING in a date range, in time order: lectures, events, meetings and deadlines.

        Use for: "what's on today", "what do I have tomorrow", "what do I have on Tuesday", "am I free Thursday",
        "my week", "what comes after my Monday lecture".

        Args:
            start: Start of the range in the user's words, e.g. "today", "tomorrow", "this week", "next 7 days", "friday", "2026-11-20".
            end: Optional end of the range in the user's words. Leave empty to cover just the start phrase.
        """
        s, e = slots.agenda_range(start, end)
        with session() as db:
            rows = db.exec(
                select(Item).where(
                    or_(
                        and_(col(Item.start) >= s, col(Item.start) < e),
                        and_(col(Item.due) >= s, col(Item.due) < e),
                    )
                )
            ).all()
        rows = sorted(rows, key=lambda i: i.start or i.due)
        return _result(rows, s, e)

    @tool
    def get_deadlines(start: str = "today", end: str = "", course: str = "", include_submitted: bool = False) -> dict:
        """List assignment/quiz/exam deadlines due in a date range, soonest first.

        Use for: "what's due this week", "when is my CS 5800 homework due", "anything due tomorrow".

        Args:
            start: Start of the range in the user's words, e.g. "today", "this week", "next 7 days", "next week".
            end: Optional end of the range in the user's words, e.g. "friday". Leave empty to cover the start phrase.
            course: Optional course code or name filter, e.g. "CS 5800". Leave empty for all courses.
            include_submitted: Set true only if the user asks about already-submitted work too.
        """
        s, e = slots.deadline_range(start, end)
        stmt = select(Item).where(col(Item.kind) == "deadline", col(Item.due) >= s, col(Item.due) < e)
        if course.strip():
            stmt = stmt.where(col(Item.course).ilike(f"%{course.strip()}%"))
        with session() as db:
            rows = db.exec(stmt).all()
        if not include_submitted:
            rows = [r for r in rows if (r.status or "").lower() not in DONE_STATUSES]
        rows.sort(key=lambda i: i.due)
        return _result(rows, s, e)

    @tool
    def find_conflicts(start: str = "this week", end: str = "") -> dict:
        """Find overlapping commitments (double-bookings) in a date range. The overlap math is exact.

        Use for: "do I have any conflicts this week", "does the career fair clash with my lectures".

        Args:
            start: Start of the range in the user's words, e.g. "this week", "next week", "next 14 days", "today".
            end: Optional end of the range in the user's words.
        """
        s, e = slots.conflict_range(start, end)
        with session() as db:
            rows = db.exec(select(Item).where(col(Item.start) >= s, col(Item.start) < e)).all()
        overlaps = find_overlaps(rows)
        return {
            "range": _range_meta(s, e),
            "count": len(overlaps),
            "conflicts": [
                {
                    "a": item_view(o.a),
                    "b": item_view(o.b),
                    "overlap_start": _iso(o.start),
                    "overlap_end": _iso(o.end),
                    "overlap_minutes": o.minutes,
                    "involves_optional": o.involves_optional,
                }
                for o in overlaps[:MAX_RESULTS]
            ],
        }

    @tool
    def get_routine(day: str = "this week") -> dict:
        """Show ONLY recurring classes (lectures, labs, office hours) for a day or week. Events, meetings and
        deadlines are NOT included: for "what do I have" or "am I free" questions use get_agenda instead.

        Use for: "when are my lectures on Tuesday", "what's my weekly class schedule", "when is my next CS 5800 lecture".

        Args:
            day: A day or range in the user's words, e.g. "tuesday", "tomorrow", "this week", "next week".
        """
        s, e = slots.routine_range(day)
        with session() as db:
            rows = db.exec(
                select(Item).where(
                    col(Item.source).in_(["routine", "demo"]),
                    col(Item.kind).in_(["lecture", "lab", "office_hours", "recitation"]),
                    col(Item.start) >= s,
                    col(Item.start) < e,
                )
            ).all()
        rows.sort(key=lambda i: i.start)
        return _result(rows, s, e)

    @tool
    def search_items(query: str, source: str = "", start: str = "", end: str = "") -> dict:
        """Keyword search over all synced items (titles, courses, locations and announcement/message text).

        Use for: "find the announcement about the midterm", "anything about the career fair", "where is the DBMS lecture".

        Args:
            query: Keywords to look for, e.g. "midterm", "career fair", "project proposal".
            source: Optional source filter: canvas, gcal, routine, gmail, github, outlook, linkedin. Leave empty for all.
            start: Optional start of a date range in the user's words. Leave empty to search everything.
            end: Optional end of a date range in the user's words.
        """
        q = validate.query(query)
        src = validate.source(source)
        terms = [t for t in q.split() if len(t) > 1][:6] or [q]
        stmt = select(Item)
        for term in terms:
            like = f"%{term}%"
            stmt = stmt.where(
                or_(
                    col(Item.title).ilike(like),
                    col(Item.course).ilike(like),
                    col(Item.location).ilike(like),
                    col(Item.body).ilike(like),
                )
            )
        if src:
            stmt = stmt.where(col(Item.source) == src)
        s = e = None
        if start or end:
            s, e = validate.date_range(start or None, end or None)
            stmt = stmt.where(
                or_(and_(col(Item.start) >= s, col(Item.start) < e), and_(col(Item.due) >= s, col(Item.due) < e))
            )
        with session() as db:
            rows = db.exec(stmt).all()
        rows.sort(key=lambda i: i.start or i.due or i.synced_at)
        result = {
            "query": q,
            "count": len(rows),
            "items": [item_view(i, include_body=True) for i in rows[:MAX_RESULTS]],
            "truncated": len(rows) > MAX_RESULTS,
        }
        if s and e:
            result["range"] = _range_meta(s, e)
        return result

    return [get_agenda, get_deadlines, find_conflicts, get_routine, search_items]
