"""ICS feed connector: Canvas "Calendar Feed" (fallback when tokens are unavailable) or Google Calendar's
"secret address in iCal format". The feed URL is itself a secret, so it lives in the OS keyring and is never
logged or stored in errors.

Canvas feed limits: no submission status and no To-Do items, and Canvas may lag up to ~24 h. Prefer API mode.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from typing import Any, Callable, Literal

import httpx
import icalendar
import recurring_ical_events

from atlas import timeutil
from atlas.config import SyncSettings
from atlas.connectors.base import (AuthError, Coverage, FetchResult, NotConfigured, RateLimited, SourceError,
                                   make_client, window)
from atlas.security.sanitize import clean_title, to_plain_text
from atlas.store.models import Item

_COURSE_SUFFIX = re.compile(r"^(.*?)\s*\[([^\]]+)\]\s*$")  # Canvas: "Homework 4 [CS 5800 Algorithms]"
MAX_FEED_BYTES = 5_000_000


def _as_local(value: date | datetime, zone) -> tuple[datetime, bool]:
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=zone)).astimezone(zone), False  # floating = local
    return timeutil.start_of_day(value, zone), True


def _url(value: Any) -> str | None:
    text = str(value or "").strip()
    return text if text.startswith(("https://", "http://")) else None


class IcsFeedConnector:
    def __init__(self, source: str, url: str | None, sync: SyncSettings, flavor: Literal["canvas", "generic"],
                 auth_hint: str, client_factory: Callable[..., httpx.Client] = make_client) -> None:
        self.source = source
        self.url = url
        self.sync = sync
        self.flavor = flavor
        self.auth_hint = auth_hint
        self.client_factory = client_factory

    def download(self) -> bytes:
        if not self.url:
            raise NotConfigured(f"no feed URL stored: run `{self.auth_hint}`")
        if not self.url.startswith("https://"):
            raise SourceError("the feed URL must start with https://")
        with self.client_factory(follow_redirects=True) as client:
            r = client.get(self.url)
        if r.url.scheme != "https":
            raise SourceError("the feed redirected to a non-https address; refusing it")
        if r.status_code in (401, 403, 404, 410):
            raise AuthError(f"the feed address was rejected (HTTP {r.status_code}); it may have been reset. "
                            f"Copy the new address and run `{self.auth_hint}`")
        if r.status_code == 429:
            raise RateLimited("the feed server is rate limiting (HTTP 429)")
        if r.status_code >= 400:
            raise SourceError(f"feed download failed (HTTP {r.status_code})")
        if len(r.content) > MAX_FEED_BYTES:
            raise SourceError("the feed is larger than 5 MB; refusing to parse it")
        return r.content

    def fetch(self, now: datetime) -> FetchResult:
        start, end = window(now, self.sync)
        cal = icalendar.Calendar.from_ical(self.download())
        zone = timeutil.tz()
        items = [i for ev in recurring_ical_events.of(cal).between(start, end) if (i := self._item(ev, zone))]
        if self.flavor == "canvas":
            coverage = [Coverage("due", start, end, frozenset({"deadline"})),
                        Coverage("start", start, end, frozenset({"event"}))]
        else:
            coverage = [Coverage("start", start, end)]
        return FetchResult(items, coverage)

    def _item(self, ev, zone) -> Item | None:
        uid = str(ev.get("UID") or "").strip()
        if not uid or "DTSTART" not in ev:
            return None
        start, all_day = _as_local(ev.decoded("DTSTART"), zone)
        end = None
        if "DTEND" in ev:
            end, _ = _as_local(ev.decoded("DTEND"), zone)
        elif "DURATION" in ev:
            end = start + ev.decoded("DURATION")
        recurring = "RRULE" in ev or "RECURRENCE-ID" in ev
        source_id = f"ics:{uid}@{start.isoformat()}" if recurring else f"ics:{uid}"
        raw_title = str(ev.get("SUMMARY") or "")
        status = "cancelled" if str(ev.get("STATUS") or "").upper() == "CANCELLED" else None
        common = dict(id=f"{self.source}:{source_id}", source=self.source, source_id=source_id,
                      location=clean_title(str(ev["LOCATION"])) if ev.get("LOCATION") else None,
                      url=_url(ev.get("URL")), body=to_plain_text(str(ev.get("DESCRIPTION") or "")), status=status)

        if self.flavor == "canvas":
            course = None
            if m := _COURSE_SUFFIX.match(raw_title):
                raw_title, course = m.group(1), clean_title(m.group(2))
            if "assignment" in uid.lower() or "/assignments/" in (common["url"] or ""):
                # Date-only assignments are due at the end of that day
                due = datetime.combine(start.date(), time(23, 59), tzinfo=zone) if all_day else start
                return Item(kind="deadline", title=clean_title(raw_title), due=due, course=course, **common)
            return Item(kind="event", title=clean_title(raw_title), start=start, end=end, all_day=all_day,
                        course=course, **common)

        # Generic calendar (e.g. Google ICS): a meeting with attendees may have been written by someone else
        trust = "untrusted" if "ATTENDEE" in ev else "trusted"
        if all_day and end is None:
            end = start + timedelta(days=1)
        return Item(kind="event", title=clean_title(raw_title), start=start, end=end, all_day=all_day, trust=trust,
                    **common)
