"""Canvas API connector (personal access token): planner items with submission status, plus announcements.

Endpoints (Canvas REST API, verified 2026-11-14 against canvas.instructure.com/doc/api):
  GET /api/v1/courses?enrollment_state=active
  GET /api/v1/planner/items?start_date&end_date      submissions: {excused, graded, late, missing,
                                                     needs_grading, with_feedback} or false
  GET /api/v1/announcements?context_codes[]=course_N&start_date&end_date
Requests are sequential (Canvas docs: sequential clients are rarely throttled); 429 = rate limited.
The token is sent only to the configured https base URL: pagination links to other hosts are refused.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx

from atlas.config import SyncSettings
from atlas.connectors.base import (AuthError, Coverage, FetchResult, NotConfigured, RateLimited, SourceError,
                                   make_client, window)
from atlas.security.sanitize import clean_title, to_plain_text
from atlas.store.models import Item
from atlas.vault import SecretStore

SOURCE = "canvas"
MAX_PAGES = 50
MIN_QUOTA = 20.0  # stop before Canvas throttles us (X-Rate-Limit-Remaining)


def normalize_base_url(url: str) -> str:
    parts = urlsplit((url or "").strip())
    if parts.scheme != "https" or not parts.netloc:
        raise NotConfigured('set [sources.canvas] base_url = "https://YOUR-SCHOOL.instructure.com" in '
                            "config/settings.toml (https only)")
    return f"https://{parts.netloc}"


def read_only(request: httpx.Request) -> None:
    """Enforced, not just conventional: a personal Canvas token can do anything you can (submit, post,
    delete), so the client refuses every method except GET before it leaves the machine."""
    if request.method != "GET":
        raise SourceError(f"blocked a {request.method} request: Atlas only reads from Canvas")


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt if dt.tzinfo else None  # Canvas always sends UTC; refuse to guess a zone


_COURSE_CODE = re.compile(r"^([A-Za-z]{2,5})\s*[-.]?\s*(\d{4})(?!\d)")


def course_label(course: dict) -> str:
    """Short, matchable course name. Northeastern codes look like "CS5100.17342.202710" (subject+number,
    CRN, term): keep "CS 5100", which is how people (and the timetable) write it. Anything that doesn't
    look like a subject+number code falls back to the course name, e.g. "Master's ... Orientation"."""
    code = str(course.get("course_code") or "").strip()
    if m := _COURSE_CODE.match(code) or _COURSE_CODE.match(str(course.get("name") or "")):
        return f"{m.group(1).upper()} {m.group(2)}"
    return clean_title(str(course.get("name") or code or course.get("id")), 60)


def submission_status(p: dict) -> str | None:
    override = p.get("planner_override") or {}
    if override.get("marked_complete"):
        return "done"
    sub = p.get("submissions")
    if isinstance(sub, dict):
        if sub.get("redo_request"):  # the instructor asked for a resubmission: open again, whatever else is set
            return "redo_requested"
        if sub.get("excused"):
            return "excused"
        if sub.get("graded"):
            return "graded"
        if sub.get("submitted") or sub.get("needs_grading"):
            return "submitted"
        if sub.get("missing"):
            return "missing"
    plannable = p.get("plannable") or {}
    if p.get("plannable_type") == "planner_note" and plannable.get("workflow_state") == "done":
        return "done"
    return None


class CanvasConnector:
    source = SOURCE

    def __init__(self, base_url: str, store: SecretStore, sync: SyncSettings,
                 client_factory: Callable[..., httpx.Client] = make_client) -> None:
        self.base_url = base_url
        self.store = store
        self.sync = sync
        self.client_factory = client_factory

    # ------------------------------------------------------------------ http
    def _get(self, client: httpx.Client, url: str, params: Any = None) -> httpx.Response:
        r = client.get(url, params=params)
        if r.status_code == 401:
            raise AuthError("Canvas rejected the token (HTTP 401): it may have expired or been deleted. "
                            "Create a new one and run `atlas auth canvas`")
        if r.status_code == 429 or (r.status_code == 403 and "rate limit" in r.text.lower()):
            raise RateLimited("Canvas is rate limiting (HTTP 429); will try again later")
        if r.status_code >= 400:
            raise SourceError(f"Canvas returned HTTP {r.status_code} for {urlsplit(str(r.url)).path}")
        remaining = r.headers.get("X-Rate-Limit-Remaining")
        if remaining is not None and float(remaining) < MIN_QUOTA:
            raise RateLimited(f"Canvas quota nearly used up ({remaining} left); will try again later")
        return r

    def _paged(self, client: httpx.Client, path: str, params: Any) -> list[dict]:
        base = urlsplit(self.base)
        out: list[dict] = []
        url: str | None = path
        for page in range(MAX_PAGES):
            r = self._get(client, url, params if page == 0 else None)
            data = r.json()
            if not isinstance(data, list):
                raise SourceError(f"unexpected Canvas response for {path}")
            out.extend(d for d in data if isinstance(d, dict))
            url = r.links.get("next", {}).get("url")
            if not url:
                return out
            nxt = urlsplit(url)
            if (nxt.scheme, nxt.netloc) != (base.scheme, base.netloc):
                raise SourceError("Canvas pagination pointed to another host; refusing to send the token there")
        raise SourceError(f"Canvas returned more than {MAX_PAGES} pages for {path}")

    # ------------------------------------------------------------------ fetch
    def fetch(self, now: datetime) -> FetchResult:
        self.base = normalize_base_url(self.base_url)
        token = self.store.get("canvas.token")
        if not token:
            raise NotConfigured("no Canvas token stored: run `atlas auth canvas`")
        start, end = window(now, self.sync)
        ann_start, ann_end = now - timedelta(days=self.sync.announcement_days), now + timedelta(days=1)
        warnings: list[str] = []
        with self.client_factory(base_url=self.base, headers={"Authorization": f"Bearer {token}"},
                                 event_hooks={"request": [read_only]}) as client:
            courses = self._paged(client, "/api/v1/courses", {"enrollment_state": "active", "per_page": 100})
            names = {c["id"]: course_label(c) for c in courses if "id" in c}
            planner = self._paged(client, "/api/v1/planner/items",
                                  {"start_date": start.isoformat(), "end_date": end.isoformat(), "per_page": 100})
            items = [i for p in planner if (i := self._planner_item(p, names))]
            coverage = [Coverage("due", start, end, frozenset({"deadline"})),
                        Coverage("start", start, end, frozenset({"event"}))]
            if names:
                params = [("context_codes[]", f"course_{cid}") for cid in names] + [
                    ("start_date", ann_start.isoformat()), ("end_date", ann_end.isoformat()), ("per_page", 100)]
                try:
                    anns = self._paged(client, "/api/v1/announcements", params)
                except (AuthError, RateLimited):
                    raise
                except SourceError as exc:  # e.g. one course hides announcements: keep the rest of the sync
                    warnings.append(f"announcements skipped: {exc}")
                else:
                    items += [i for a in anns if (i := self._announcement(a, names))]
                    coverage.append(Coverage("start", ann_start, ann_end, frozenset({"announcement"})))
        return FetchResult(items, coverage, warnings)

    def _abs_url(self, url: Any) -> str | None:
        if not url:
            return None
        url = str(url)
        if url.startswith("/"):
            return self.base + url
        return url if url.startswith("https://") else None

    def _planner_item(self, p: dict, names: dict) -> Item | None:
        ptype = str(p.get("plannable_type") or "")
        if ptype == "announcement":
            return None  # fetched from /announcements with the message body
        pl = p.get("plannable") or {}
        pid = p.get("plannable_id") or pl.get("id")
        if pid is None:
            return None
        common = dict(id=f"{SOURCE}:{ptype}:{pid}", source=SOURCE, source_id=f"{ptype}:{pid}",
                      title=clean_title(str(pl.get("title") or pl.get("name") or "")),
                      course=names.get(p.get("course_id")) or (clean_title(p["context_name"]) if p.get("context_name") else None),
                      url=self._abs_url(p.get("html_url") or pl.get("html_url")), status=submission_status(p),
                      updated_at=_parse_dt(pl.get("updated_at")))
        if ptype == "calendar_event":
            start = _parse_dt(pl.get("start_at"))
            if start is None:
                return None
            return Item(kind="event", start=start, end=_parse_dt(pl.get("end_at")), all_day=bool(pl.get("all_day")),
                        location=clean_title(pl["location_name"]) if pl.get("location_name") else None, **common)
        due = _parse_dt(pl.get("due_at") or pl.get("todo_date") or p.get("plannable_date"))
        if due is None:
            return None
        return Item(kind="deadline", due=due, **common)

    def _announcement(self, a: dict, names: dict) -> Item | None:
        posted = _parse_dt(a.get("posted_at") or a.get("delayed_post_at"))
        if a.get("id") is None or posted is None:
            return None
        code = str(a.get("context_code") or "")
        cid = int(code.split("_", 1)[1]) if code.startswith("course_") and code[7:].isdigit() else None
        url = self._abs_url(a.get("html_url")) or (f"{self.base}/courses/{cid}/discussion_topics/{a['id']}" if cid else None)
        return Item(id=f"{SOURCE}:announcement:{a['id']}", source=SOURCE, source_id=f"announcement:{a['id']}",
                    kind="announcement", title=clean_title(str(a.get("title") or "")), start=posted,
                    course=names.get(cid), url=url, body=to_plain_text(a.get("message")), trust="untrusted")
