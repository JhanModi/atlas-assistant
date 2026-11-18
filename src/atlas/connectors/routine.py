"""Routine connector: config/routine.yaml (your semester timetable) -> dated lecture/lab/office-hours items.

Local file, no network, so it is the most reliable source. Times are local (settings.timezone).
See config/routine.example.yaml for the format.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import yaml

from atlas import timeutil
from atlas.config import SyncSettings, project_root
from atlas.connectors.base import Coverage, FetchResult, NotConfigured, window
from atlas.security.sanitize import clean_title
from atlas.store.models import Item

SOURCE = "routine"


def _date(value: Any, where: str) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise ValueError(f"{where}: {value!r} is not a date like 2026-09-03") from None


def _time(value: Any, where: str) -> time:
    if isinstance(value, int):  # YAML 1.1 reads an unquoted 11:45 as the sexagesimal number 705
        hours, minutes = divmod(value, 60)
        return time(hours, minutes)
    try:
        return time.fromisoformat(str(value).strip())
    except ValueError:
        raise ValueError(f'{where}: {value!r} is not a time like "11:45"') from None


def _datetime(value: Any, zone, where: str) -> tuple[datetime, bool]:
    """Returns (local datetime, all_day)."""
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=zone)), False
    if isinstance(value, date):
        return timeutil.start_of_day(value, zone), True
    try:
        dt = datetime.fromisoformat(str(value).strip())
    except ValueError:
        raise ValueError(f"{where}: {value!r} is not a date/time like 2026-11-18T10:00") from None
    return (dt if dt.tzinfo else dt.replace(tzinfo=zone)), False


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "entry"


def expand(raw: dict, start: datetime, end: datetime, zone) -> list[Item]:
    """All routine occurrences whose start lies in [start, end)."""
    items: list[Item] = []
    sem = raw.get("semester") or {}
    sem_start = _date(sem.get("start"), "semester.start") or date.min
    sem_end = _date(sem.get("end"), "semester.end") or date.max
    skip = {_date(d, "semester.skip_dates") for d in sem.get("skip_dates") or []}

    for idx, entry in enumerate(raw.get("recurring") or []):
        where = f"recurring[{idx}]"
        if not isinstance(entry, dict) or not entry.get("title"):
            raise ValueError(f"{where}: every entry needs a title")
        title = clean_title(str(entry["title"]))
        where = f"{where} ({title})"
        days = []
        for d in entry.get("days") or []:
            idx_day = timeutil._weekday_index(str(d))
            if idx_day is None:
                raise ValueError(f"{where}: unknown day {d!r}; use mon tue wed thu fri sat sun")
            days.append(idx_day)
        if not days:
            raise ValueError(f"{where}: list the days it happens, e.g. days: [tue, fri]")
        t_start, t_end = _time(entry.get("start"), f"{where}.start"), _time(entry.get("end"), f"{where}.end")
        if t_end <= t_start:
            raise ValueError(f"{where}: end must be after start")
        first = max(start.date(), sem_start, _date(entry.get("from"), f"{where}.from") or date.min)
        last = min(end.date(), sem_end, _date(entry.get("until"), f"{where}.until") or date.max)
        entry_skip = {_date(d, f"{where}.skip_dates") for d in entry.get("skip_dates") or []}
        slug = _slug(str(entry.get("id") or title))
        day = first
        while day <= last:
            if day.weekday() in days and day not in skip and day not in entry_skip:
                s = datetime.combine(day, t_start, tzinfo=zone)
                if start <= s < end:
                    items.append(Item(
                        id=f"{SOURCE}:{slug}:{day.isoformat()}T{t_start:%H%M}", source=SOURCE,
                        source_id=f"{slug}:{day.isoformat()}T{t_start:%H%M}",
                        kind=str(entry.get("kind") or "lecture"), title=title, start=s,
                        end=datetime.combine(day, t_end, tzinfo=zone), course=entry.get("course"),
                        location=entry.get("location"), status="optional" if entry.get("optional") else None,
                    ))
            day += timedelta(days=1)

    for idx, entry in enumerate(raw.get("one_off") or []):
        where = f"one_off[{idx}]"
        if not isinstance(entry, dict) or not entry.get("title"):
            raise ValueError(f"{where}: every entry needs a title")
        title = clean_title(str(entry["title"]))
        where = f"{where} ({title})"
        slug = _slug(str(entry.get("id") or title))
        if entry.get("due") is not None:  # a personal deadline
            due, _ = _datetime(entry["due"], zone, f"{where}.due")
            if start <= due < end:
                items.append(Item(id=f"{SOURCE}:{slug}:{due:%Y%m%dT%H%M}", source=SOURCE,
                                  source_id=f"{slug}:{due:%Y%m%dT%H%M}", kind="deadline", title=title, due=due,
                                  course=entry.get("course"), status=entry.get("status")))
            continue
        s, all_day = _datetime(entry.get("start"), zone, f"{where}.start")
        e = None
        if entry.get("end") is not None:
            e, _ = _datetime(entry["end"], zone, f"{where}.end")
            if e <= s:
                raise ValueError(f"{where}: end must be after start")
        elif all_day:
            e = s + timedelta(days=1)
        if start <= s < end:
            items.append(Item(id=f"{SOURCE}:{slug}:{s:%Y%m%dT%H%M}", source=SOURCE, source_id=f"{slug}:{s:%Y%m%dT%H%M}",
                              kind=str(entry.get("kind") or "event"), title=title, start=s, end=e, all_day=all_day,
                              course=entry.get("course"), location=entry.get("location"),
                              status="optional" if entry.get("optional") else None))
    return items


EXAMPLE_WARNING = ("config/routine.yaml is still the example timetable (fictional classes), so it was ignored. "
                   "Replace it with your real lectures, labs and office hours.")
_DEFAULT = object()


class RoutineConnector:
    source = SOURCE

    def __init__(self, path: str | Path, sync: SyncSettings, example_path: Path | None | object = _DEFAULT) -> None:
        p = Path(path)
        self.path = p if p.is_absolute() else project_root() / p
        self.sync = sync
        self.example_path = project_root() / "config" / "routine.example.yaml" if example_path is _DEFAULT else example_path

    def _is_example(self, raw: dict) -> bool:
        """An unedited copy of the example must never be presented as the user's real schedule."""
        if not self.example_path or not Path(self.example_path).exists():
            return False
        with open(self.example_path, encoding="utf-8") as f:
            return raw == (yaml.safe_load(f) or {})

    def fetch(self, now: datetime) -> FetchResult:
        if not self.path.exists():
            raise NotConfigured(f"{self.path.name} not found: copy config/routine.example.yaml to "
                                f"config/routine.yaml and fill in your timetable")
        with open(self.path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        if not isinstance(raw, dict):
            raise ValueError("routine.yaml must be a mapping with semester / recurring / one_off")
        start, end = window(now, self.sync)
        coverage = [Coverage("start", start, end), Coverage("due", start, end)]
        if self._is_example(raw):  # also removes example sessions an earlier sync stored
            return FetchResult([], coverage, [EXAMPLE_WARNING])
        return FetchResult(expand(raw, start, end, timeutil.tz()), coverage)
