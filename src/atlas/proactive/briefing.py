"""The daily briefing (Phase 2 milestone): an accurate summary of today's classes, deadlines and meetings,
produced without being asked.

It is composed by plain code from the store, never by the model, so its accuracy is the accuracy of the
synced data. Every section is derived from the same detectors that raise alerts, and a "data health" section
says which sources are stale, so a briefing never looks complete when it isn't.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any, Callable

from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from atlas import timeutil
from atlas.agent.tools import item_view
from atlas.config import Settings
from atlas.proactive import alerts as alert_store
from atlas.proactive.detectors import (DUE_SOON, conflicts, detect, load_items, missing_items, new_announcements,
                                       open_deadlines, source_health)
from atlas.proactive.notify import Notifier
from atlas.store.models import Alert, Briefing, Item
from atlas.timeline import IGNORED_STATUSES

ON_TIME_WINDOW = timedelta(minutes=15)  # generated within 15 min of the scheduled time = "scheduled", else "catch_up"


def _view(item: Item) -> dict[str, Any]:
    v = item_view(item)
    return {k: v[k] for k in ("id", "title", "kind", "when", "start", "course", "location", "status", "untrusted",
                              "source") if k in v} | ({"url": item.url} if item.url else {})


def _last_daily(engine: Engine) -> Briefing | None:
    with Session(engine) as db:
        return db.exec(select(Briefing).where(col(Briefing.daily_key).is_not(None))
                       .order_by(col(Briefing.created_at).desc())).first()


def build(engine: Engine, settings: Settings, now: datetime, alerts: list[Alert] | None = None) -> dict[str, Any]:
    zone = timeutil.tz()
    local = timeutil.to_local(now)
    day_start = timeutil.start_of_day(local.date(), zone)
    day_end = day_start + timedelta(days=1)
    items = load_items(engine, now, since=day_start)  # from midnight, so this morning's classes are included
    last = _last_daily(engine)
    since = last.created_at if last else now - timedelta(hours=24)

    today = sorted((i for i in items if i.kind not in ("announcement", "deadline") and i.start
                    and (day_start <= i.start < day_end or (i.end and i.start < day_start < i.end))
                    and (i.status or "") not in IGNORED_STATUSES), key=lambda i: (not i.all_day, i.start))
    due_48h = open_deadlines(items, now, now + DUE_SOON)
    due_week = open_deadlines(items, now + DUE_SOON, now + timedelta(days=7))
    clashes = conflicts(items, now, include_optional=True)
    anns = new_announcements(items, now, since)
    health = source_health(engine, settings, now)
    with Session(engine) as db:
        demo = len(db.exec(select(Item.id).where(Item.source == "demo")).all())
    caveats = []
    if settings.sources.canvas.enabled and settings.sources.canvas.mode == "ics":
        caveats.append("Canvas is in feed mode, which can't see submissions: items listed as due may already be done.")
    if demo:
        caveats.append(f"{demo} fictional demo items are loaded. Run `atlas clear-demo` before relying on this.")

    content = {
        "day": local.date().isoformat(),
        "heading": local.strftime("%A, %B ") + str(local.day),
        "generated": timeutil.fmt_local(now),
        "today": [_view(i) for i in today],
        "due_48h": [_view(i) for i in due_48h],
        "due_week": [_view(i) for i in due_week],
        "missing": [_view(i) for i in missing_items(items, now)],
        "clashes": [{"a": _view(o.a), "b": _view(o.b), "minutes": o.minutes, "optional": o.involves_optional}
                    for o in clashes],
        "announcements": [_view(i) for i in anns],
        "health": health,
        "caveats": caveats,
        "alerts": [{"id": a.id, "title": a.title, "urgent": a.urgent, "detector": a.detector} for a in alerts or []],
    }
    content["summary"] = summary(content)
    return content


def summary(c: dict[str, Any]) -> str:
    classes = sum(1 for i in c["today"] if i["kind"] in ("lecture", "lab", "recitation", "office_hours"))
    events = len(c["today"]) - classes
    parts = [f"{classes} class{'es' if classes != 1 else ''}"]
    if events:
        parts.append(f"{events} other event{'s' if events != 1 else ''}")
    parts.append(f"{len(c['due_48h'])} due within 48 h")
    hard = sum(1 for x in c["clashes"] if not x["optional"])
    if hard:
        parts.append(f"{hard} clash{'es' if hard != 1 else ''}")
    if c["missing"]:
        parts.append(f"{len(c['missing'])} missing")
    stale = [h["source"] for h in c["health"] if h["stale"]]
    if stale:
        parts.append("stale: " + ", ".join(stale))
    return " · ".join(parts)


def render_text(c: dict[str, Any]) -> str:
    """Plain-text rendering for the CLI."""
    def line(v: dict) -> str:
        bits = [v["title"]]
        if v.get("course") and v["course"] not in v["title"]:
            bits.append(f"[{v['course']}]")
        if v.get("when"):
            bits.append(f"- {v['when']}")
        if v.get("location"):
            bits.append(f"@ {v['location']}")
        if v.get("status") in ("missing", "optional"):
            bits.append(f"({v['status']})")
        if v.get("untrusted"):
            bits.append("(from someone else's invite)")
        return "  - " + " ".join(bits)

    out = [f"Atlas briefing for {c['heading']}", c["summary"], ""]
    for key, title in (("today", "Today"), ("missing", "Missing submissions"), ("due_48h", "Due in the next 48 hours"),
                       ("due_week", "Due later this week")):
        out.append(f"{title}:")
        out += [line(v) for v in c[key]] or ["  - nothing"]
        out.append("")
    out.append("Clashes (next 14 days):")
    out += [f"  - {x['a']['title']} overlaps {x['b']['title']} for {x['minutes']} min"
            f"{' (optional)' if x['optional'] else ''} - {x['a']['when']}" for x in c["clashes"]] or ["  - none"]
    out.append("")
    if c["announcements"]:
        out.append("New announcements:")
        out += [line(v) for v in c["announcements"]]
        out.append("")
    out.append("Data health:")
    out += [f"  - {h['source']}: {h['message']}" + (f" (last success {h['last_success']})" if h["stale"] and h["last_success"] else "")
            for h in c["health"]] or ["  - no sources enabled"]
    out += [f"  ! {x}" for x in c["caveats"]]
    return "\n".join(out)


def toast_text(c: dict[str, Any], now: datetime) -> tuple[str, str]:
    """Toast title/body. Uses counts and trusted titles only, and points at what's *next* (a catch-up
    briefing in the afternoon shouldn't lead with a class that already ended)."""
    body = c["summary"]
    upcoming = (i for i in c["today"] if not i.get("untrusted") and not i.get("all_day") and i.get("start")
                and datetime.fromisoformat(i["start"]) >= now)
    nxt = next(upcoming, None)
    if nxt:
        body += f". Next: {nxt['title']} ({nxt['when'].split(', ', 1)[-1].split(' (')[0]})"
    return f"Atlas · {c['heading']}", body


def generate(engine: Engine, settings: Settings, now: datetime, trigger: str, daily: bool,
             notifier: Notifier | None = None, url: str | None = None) -> Briefing | None:
    """Detect and record alerts, build the briefing and store it. Daily briefings are unique per local day."""
    last = _last_daily(engine)
    findings = detect(engine, settings, now, since=last.created_at if last else now - timedelta(hours=24))
    new = alert_store.record(engine, findings, now)
    if notifier is not None:
        alert_store.deliver_urgent(engine, new, notifier, now, url)
    with Session(engine) as db:
        open_alerts = list(db.exec(select(Alert).where(col(Alert.dedupe_key).in_([f.dedupe_key for f in findings]))).all()) \
            if findings else []
    content = build(engine, settings, now, alerts=open_alerts)
    briefing = Briefing(day=content["day"], daily_key=content["day"] if daily else None, trigger=trigger,
                        created_at=now, content=content,
                        stale_sources=[h["source"] for h in content["health"] if h["stale"]])
    with Session(engine, expire_on_commit=False) as db:
        db.add(briefing)
        try:
            db.commit()
        except IntegrityError:  # someone else made today's daily briefing in the meantime
            db.rollback()
            return None
    alert_store.mark_in_briefing(engine, [a.id for a in open_alerts], now)
    if notifier is not None and daily:
        title, body = toast_text(content, now)
        notifier.send(title, body, url)
    return briefing


def ensure_daily(engine: Engine, settings: Settings, now: datetime, notifier: Notifier | None = None,
                 url: str | None = None, sync: Callable[[datetime], Any] | None = None) -> Briefing | None:
    """Idempotent: make today's briefing if it's past the briefing time and none exists yet.

    Runs from the scheduler every few minutes, on startup and after wake, and from `atlas brief --if-due`
    (e.g. Windows Task Scheduler), so a 7 AM briefing missed while the laptop slept arrives at wake.
    """
    local = timeutil.to_local(now)
    due_at = datetime.combine(local.date(), time(settings.briefing.hour, settings.briefing.minute), tzinfo=timeutil.tz())
    if local < due_at:
        return None
    with Session(engine) as db:
        if db.exec(select(Briefing.id).where(Briefing.daily_key == local.date().isoformat())).first():
            return None
    if sync is not None:
        sync(now)  # fresh data first; parked or backing-off sources are skipped by the sync runner itself
    trigger = "scheduled" if local - due_at <= ON_TIME_WINDOW else "catch_up"
    return generate(engine, settings, now, trigger, daily=True, notifier=notifier, url=url)


def latest(engine: Engine, day: str | None = None) -> Briefing | None:
    """Today's daily briefing if there is one, else the most recent briefing (of that day, if given)."""
    with Session(engine) as db:
        if day:
            daily = db.exec(select(Briefing).where(Briefing.daily_key == day)).first()
            if daily is not None:
                return daily
        stmt = select(Briefing).order_by(col(Briefing.created_at).desc())
        if day:
            stmt = stmt.where(Briefing.day == day)
        return db.exec(stmt).first()


VERDICTS = ("accurate", "inaccurate")


def set_verdict(engine: Engine, briefing_id: str, verdict: str, missed: int | None, note: str | None,
                now: datetime) -> Briefing | None:
    """Phase 2 gate input: the user checked the briefing against Canvas and the calendar."""
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {VERDICTS}")
    if missed is not None and missed < 0:
        raise ValueError("missed must be 0 or more")
    with Session(engine, expire_on_commit=False) as db:
        b = db.get(Briefing, briefing_id)
        if b is None:
            return None
        b.verdict, b.missed_items, b.verdict_at = verdict, missed, now
        b.verdict_note = (note or "").strip()[:500] or None
        db.add(b)
        db.commit()
        return b
