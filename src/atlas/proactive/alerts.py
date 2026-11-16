"""Turn findings into Alert rows (deduplicated by the DB) and deliver the urgent ones as toasts.

Non-urgent alerts are not pushed: they appear in the next briefing. Toasts are capped per day; anything over
the cap is folded into one summary toast, so a bad day can't turn into a notification storm.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from atlas import timeutil
from atlas.proactive.detectors import Finding
from atlas.proactive.notify import Notifier
from atlas.store.models import Alert

MAX_TOASTS_PER_DAY = 5


def record(engine: Engine, findings: list[Finding], now: datetime) -> list[Alert]:
    """Insert alerts for findings not seen before. Returns only the new ones."""
    new: list[Alert] = []
    with Session(engine, expire_on_commit=False) as db:
        known = set(db.exec(select(Alert.dedupe_key).where(
            col(Alert.dedupe_key).in_([f.dedupe_key for f in findings]))).all()) if findings else set()
        for f in findings:
            if f.dedupe_key in known:
                continue
            known.add(f.dedupe_key)
            alert = Alert(detector=f.detector, dedupe_key=f.dedupe_key, title=f.title, detail=f.detail or None,
                          item_ids=f.item_ids, urgent=f.urgent, created_at=now)
            db.add(alert)
            try:
                db.commit()
            except IntegrityError:  # another process recorded it first
                db.rollback()
                continue
            new.append(alert)
    return new


def deliver_urgent(engine: Engine, alerts: list[Alert], notifier: Notifier, now: datetime,
                   url: str | None = None, cap: int = MAX_TOASTS_PER_DAY) -> int:
    """Toast new urgent alerts, respecting the daily cap. Returns the number of toasts shown."""
    urgent = [a for a in alerts if a.urgent and a.notified_at is None]
    if not urgent:
        return 0
    day_start = timeutil.start_of_day(timeutil.to_local(now).date(), timeutil.tz())
    with Session(engine, expire_on_commit=False) as db:
        used = len(db.exec(select(Alert.id).where(Alert.channel == "toast", col(Alert.notified_at) >= day_start,
                                                  col(Alert.notified_at) < day_start + timedelta(days=1))).all())
        room = max(cap - used, 0)
        shown = 0
        individual = urgent[:room] if len(urgent) <= room else urgent[:max(room - 1, 0)]
        for a in individual:
            if notifier.send("Atlas", a.title, url):
                shown += 1
                _mark(db, a, now, "toast")
        rest = urgent[len(individual):]
        if rest and room > len(individual):
            if notifier.send("Atlas", f"{len(rest)} more urgent item(s). Open Atlas for details.", url):
                shown += 1
                for a in rest:
                    _mark(db, a, now, "toast")
        db.commit()
    return shown


def _mark(db: Session, alert: Alert, now: datetime, channel: str) -> None:
    row = db.get(Alert, alert.id)
    if row is not None:
        row.notified_at, row.channel = now, channel
        alert.notified_at, alert.channel = now, channel
        db.add(row)


def mark_in_briefing(engine: Engine, alert_ids: list[str], now: datetime) -> None:
    """Alerts shown in a briefing count as delivered (for alert precision), unless already toasted."""
    with Session(engine) as db:
        for row in db.exec(select(Alert).where(col(Alert.id).in_(alert_ids))).all():
            if row.notified_at is None:
                row.notified_at, row.channel = now, "briefing"
                db.add(row)
        db.commit()
