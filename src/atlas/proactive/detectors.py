"""Deterministic detectors (no LLM): what deserves the user's attention, computed from the store.

Precision matters more than volume (docs/04-metrics.md: *useful* alerts). So:
- "due soon" only for work not already submitted/graded/done/excused;
- "missing" only when Canvas itself says the submission is missing (feed mode can't know, so it never claims it);
- clashes ignore optional items (office hours, events shown as free) and declined/cancelled ones;
- each finding has a stable dedupe key, so the same alert is never raised twice.
Text that may reach a Windows toast never includes titles written by other people (phishing-shaped invites).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy.engine import Engine
from sqlmodel import Session, and_, col, or_, select

from atlas import timeutil
from atlas.agent.tools import DONE_STATUSES, when_text
from atlas.config import Settings
from atlas.connectors.base import is_stale
from atlas.store.models import Item, SyncState
from atlas.timeline import IGNORED_STATUSES, find_overlaps

DUE_SOON = timedelta(hours=48)
URGENT = timedelta(hours=24)
CONFLICT_HORIZON = timedelta(days=14)
MISSING_LOOKBACK = timedelta(days=7)
ANNOUNCEMENT_MAX_AGE = timedelta(days=3)
OTHERS_INVITE = "an event from someone else's invite"


@dataclass
class Finding:
    detector: str  # due_soon | missing | conflict | new_announcement | stale_source
    dedupe_key: str
    title: str  # safe for a toast: never contains untrusted text
    detail: str = ""
    item_ids: list[str] = field(default_factory=list)
    urgent: bool = False


def safe_title(item: Item) -> str:
    return item.title if item.trust != "untrusted" else OTHERS_INVITE


def _label(item: Item) -> str:
    return f"{safe_title(item)} ({item.course})" if item.course and item.trust != "untrusted" else safe_title(item)


def load_items(engine: Engine, now: datetime, since: datetime | None = None) -> list[Item]:
    """Items relevant to detectors: [since or now] - 7 days .. now + 14 days, plus anything in progress."""
    lo, hi = (since or now) - MISSING_LOOKBACK, now + CONFLICT_HORIZON
    with Session(engine) as db:
        return list(db.exec(select(Item).where(or_(
            and_(col(Item.start) >= lo, col(Item.start) < hi),
            and_(col(Item.due) >= lo, col(Item.due) < hi),
            and_(col(Item.end) > now, col(Item.start) < now),  # in progress
        ))).all())


def open_deadlines(items: list[Item], start: datetime, end: datetime) -> list[Item]:
    return sorted((i for i in items if i.kind == "deadline" and i.due is not None and start <= i.due < end
                   and (i.status or "").lower() not in DONE_STATUSES), key=lambda i: i.due)


def due_soon(items: list[Item], now: datetime) -> list[Finding]:
    out = []
    for d in open_deadlines(items, now, now + DUE_SOON):
        urgent = d.due - now <= URGENT
        out.append(Finding("due_soon", f"due:{d.id}:{d.due.isoformat()}:{'24h' if urgent else '48h'}",
                           f"Due {when_text(d).removeprefix('due ')}: {_label(d)}", item_ids=[d.id], urgent=urgent))
    return out


def missing_items(items: list[Item], now: datetime) -> list[Item]:
    return sorted((i for i in items if i.kind == "deadline" and i.due and now - MISSING_LOOKBACK <= i.due < now
                   and (i.status or "").lower() == "missing"), key=lambda i: i.due)


def missing(items: list[Item], now: datetime) -> list[Finding]:
    return [Finding("missing", f"missing:{i.id}", f"Missing submission: {_label(i)} ({when_text(i)})",
                    item_ids=[i.id], urgent=True) for i in missing_items(items, now)]


def conflicts(items: list[Item], now: datetime, include_optional: bool = False) -> list:
    timed = [i for i in items if i.start and now <= i.start < now + CONFLICT_HORIZON and i.kind != "announcement"
             and (i.status or "") not in IGNORED_STATUSES]
    return [o for o in find_overlaps(timed) if include_optional or not o.involves_optional]


def conflict_findings(items: list[Item], now: datetime) -> list[Finding]:
    out = []
    for o in conflicts(items, now):
        a, b = sorted((o.a, o.b), key=lambda i: i.id)
        out.append(Finding("conflict", f"conflict:{a.id}|{b.id}:{o.start.isoformat()}",
                           f"Clash {when_text(o.a if o.a.start == o.start else o.b).split(',')[0]}: "
                           f"{_label(o.a)} overlaps {_label(o.b)} for {o.minutes} min",
                           item_ids=[a.id, b.id], urgent=o.start - now <= URGENT))
    return out


def new_announcements(items: list[Item], now: datetime, since: datetime) -> list[Item]:
    return sorted((i for i in items if i.kind == "announcement" and i.first_seen_at and i.first_seen_at >= since
                   and i.start and i.start >= now - ANNOUNCEMENT_MAX_AGE), key=lambda i: i.start, reverse=True)


def announcement_findings(items: list[Item], now: datetime, since: datetime) -> list[Finding]:
    return [Finding("new_announcement", f"ann:{i.id}",
                    f"New announcement{' in ' + i.course if i.course else ''}", item_ids=[i.id])
            for i in new_announcements(items, now, since)]


def source_health(engine: Engine, settings: Settings, now: datetime) -> list[dict]:
    """One row per enabled source: is its data current enough to trust in a briefing?"""
    with Session(engine) as db:
        states = {s.source: s for s in db.exec(select(SyncState)).all()}
    rows = []
    for name, cfg in (("routine", settings.sources.routine), ("canvas", settings.sources.canvas),
                      ("gcal", settings.sources.gcal)):
        if not cfg.enabled:
            continue
        st = states.get(name)
        stale = name != "routine" and is_stale(st, now, settings.sync)  # a local file can't go stale
        if name == "routine" and st is not None and st.status not in ("ok", "never"):
            stale = True
        if st is None or st.status == "never":
            message = "not synced yet"
        elif st.status == "needs_reauth":
            message = f"sign-in expired: run `atlas auth {name}`"
        elif st.status == "not_configured":
            message = st.last_error or "not set up"
        elif st.status != "ok":
            message = f"last sync failed ({st.status}): {st.last_error}"
        elif stale:
            message = "no successful sync recently"
        else:
            message = "ok"
        warning = st.last_error.removeprefix("warning: ") if st and st.status == "ok" and \
            (st.last_error or "").startswith("warning:") else None
        rows.append({"source": name, "status": st.status if st else "never", "stale": stale, "message": message,
                     "warning": warning,
                     "last_success": timeutil.fmt_local(st.last_success_at) if st and st.last_success_at else None,
                     "mode": getattr(cfg, "mode", "file")})
    return rows


def stale_findings(health: list[dict], now: datetime) -> list[Finding]:
    day = timeutil.to_local(now).date().isoformat()
    return [Finding("stale_source", f"stale:{h['source']}:{h['status']}:{day}",
                    f"{h['source']}: {h['message']}", urgent=h["status"] == "needs_reauth")
            for h in health if h["stale"]]


def detect(engine: Engine, settings: Settings, now: datetime, since: datetime) -> list[Finding]:
    items = load_items(engine, now)
    return (missing(items, now) + due_soon(items, now) + conflict_findings(items, now)
            + announcement_findings(items, now, since) + stale_findings(source_health(engine, settings, now), now))
