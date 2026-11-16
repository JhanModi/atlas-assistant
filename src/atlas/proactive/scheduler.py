"""Background jobs inside `atlas serve` (APScheduler 3, in-process):

- sync: every `sync.interval_minutes`, then run the detectors and toast new urgent alerts;
- briefing: every 5 minutes, `ensure_daily()`, which is a no-op unless it's past the briefing time and today's
  briefing doesn't exist yet. Missed runs while the laptop sleeps are coalesced into one run at wake
  (misfire_grace_time=None), and the first ticks run seconds after start-up, so a missed 7 AM briefing
  arrives as a catch-up instead of being skipped.

Both jobs take one lock, so a sync and a briefing never interleave. Job errors are recorded, never raised.
"""

from __future__ import annotations

import threading
import traceback
from datetime import datetime, timedelta
from typing import Any, Callable

from sqlalchemy.engine import Engine
from sqlmodel import Session

from atlas import timeutil
from atlas.config import Settings
from atlas.connectors import build_connectors, sync_source
from atlas.connectors.base import SyncReport
from atlas.proactive import alerts as alert_store
from atlas.proactive import briefing
from atlas.proactive.detectors import detect
from atlas.proactive.notify import Notifier
from atlas.security.sanitize import safe_error
from atlas.store.models import SyncState

BRIEFING_CHECK_MINUTES = 5
FRESH_ENOUGH = timedelta(minutes=10)  # don't re-sync a source that synced this recently


class AtlasScheduler:
    def __init__(self, engine: Engine, settings: Settings, notifier: Notifier, url: str | None = None,
                 connectors: Callable[[Settings], list] = build_connectors) -> None:
        self.engine = engine
        self.settings = settings
        self.notifier = notifier
        self.url = url
        self.connectors = connectors
        self.lock = threading.Lock()
        self.status: dict[str, dict[str, Any]] = {}
        self._sched = None

    # ----------------------------------------------------------------- jobs
    def sync_all(self, now: datetime, skip_fresh: bool = False) -> list[SyncReport]:
        reports = []
        for conn in self.connectors(self.settings):
            if skip_fresh:
                with Session(self.engine) as db:
                    st = db.get(SyncState, conn.source)
                if st and st.status == "ok" and st.last_success_at and now - st.last_success_at < FRESH_ENOUGH:
                    continue
            reports.append(sync_source(self.engine, conn, self.settings.sync, now=now))
        return reports

    def sync_tick(self, now: datetime | None = None) -> None:
        with self.lock:
            now = now or timeutil.now()
            reports = self.sync_all(now)
            last = briefing._last_daily(self.engine)
            findings = detect(self.engine, self.settings, now,
                              since=last.created_at if last else now - timedelta(hours=24))
            new = alert_store.record(self.engine, findings, now)
            shown = alert_store.deliver_urgent(self.engine, new, self.notifier, now, self.url)
            self.status["sync"] = {"at": timeutil.fmt_local(now), "sources": {r.source: r.status for r in reports},
                                   "new_alerts": len(new), "toasts": shown}

    def briefing_tick(self, now: datetime | None = None) -> None:
        with self.lock:
            now = now or timeutil.now()
            made = briefing.ensure_daily(self.engine, self.settings, now, self.notifier, self.url,
                                         sync=lambda t: self.sync_all(t, skip_fresh=True))
            if made is not None:
                self.status["briefing"] = {"at": timeutil.fmt_local(now), "trigger": made.trigger, "id": made.id}

    def _guard(self, name: str, fn: Callable[[], None]) -> Callable[[], None]:
        def run() -> None:
            try:
                fn()
            except Exception as exc:  # noqa: BLE001 - keep the scheduler alive; surface the error instead
                self.status[f"{name}_error"] = {"at": timeutil.fmt_local(timeutil.now()),
                                                "error": safe_error(f"{type(exc).__name__}: {exc}"),
                                                "trace": safe_error(traceback.format_exc(limit=3))}
        return run

    # ----------------------------------------------------------------- lifecycle
    def start(self) -> None:
        from apscheduler.schedulers.background import BackgroundScheduler

        zone = timeutil.tz()
        sched = BackgroundScheduler(timezone=zone, job_defaults={
            "coalesce": True, "max_instances": 1, "misfire_grace_time": None})
        start = datetime.now(zone)
        sched.add_job(self._guard("sync", self.sync_tick), "interval", id="sync",
                      minutes=self.settings.sync.interval_minutes, next_run_time=start + timedelta(seconds=5))
        sched.add_job(self._guard("briefing", self.briefing_tick), "interval", id="briefing",
                      minutes=BRIEFING_CHECK_MINUTES, next_run_time=start + timedelta(seconds=30))
        sched.start()
        self._sched = sched

    def shutdown(self) -> None:
        if self._sched is not None:
            self._sched.shutdown(wait=False)
            self._sched = None

    def jobs(self) -> list:
        return self._sched.get_jobs() if self._sched else []
