"""Connector framework (ADR-0006): fetch -> normalize -> replace-in-window upsert, with health tracking.

Rules that make sync reliable:
- A connector is authoritative only for what its fetch *covers* (a window per kind). Items it no longer
  returns inside that window are removed (deleted or cancelled upstream); items outside it are left alone.
- A failed fetch changes no items: the last good data stays, and the briefing marks the source stale.
- No retry loops. An auth failure parks the source as `needs_reauth` until the user runs `atlas auth`;
  other failures back off exponentially (capped) until a later scheduled attempt.
- Errors are stored without URLs or tokens (feed URLs are secrets).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

import httpx
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from atlas import timeutil
from atlas.config import SyncSettings
from atlas.security.sanitize import safe_error
from atlas.store.models import Item, SyncState

USER_AGENT = "atlas-assistant/0.1 (personal assistant; local, single user)"
CONTENT_FIELDS = ("source_id", "kind", "title", "start", "end", "due", "all_day", "course", "location", "url",
                  "status", "body", "private", "trust", "updated_at")


class SourceError(Exception):
    """A fetch failed; the source's previous data is kept."""

    status = "error"


class AuthError(SourceError):
    """Credentials missing, expired or revoked. Not retried until the user signs in again."""

    status = "needs_reauth"


class RateLimited(SourceError):
    status = "rate_limited"


class NotConfigured(SourceError):
    status = "not_configured"


@dataclass(frozen=True)
class Coverage:
    """The slice of this source's items that a fetch is authoritative for."""

    field: str  # "start" | "due"
    start: datetime
    end: datetime
    kinds: frozenset[str] | None = None

    def covers(self, item: Item) -> bool:
        value = getattr(item, self.field)
        return value is not None and self.start <= value < self.end and (self.kinds is None or item.kind in self.kinds)


@dataclass
class FetchResult:
    items: list[Item]
    coverage: list[Coverage]
    warnings: list[str] = field(default_factory=list)


class Connector(Protocol):
    source: str

    def fetch(self, now: datetime) -> FetchResult: ...


@dataclass
class SyncReport:
    source: str
    status: str
    added: int = 0
    updated: int = 0
    removed: int = 0
    total: int = 0
    duration_ms: int = 0
    error: str | None = None
    skipped: bool = False
    items: list[Item] = field(default_factory=list)  # filled only for dry runs

    def line(self) -> str:
        if self.skipped:
            return f"{self.source:8} skipped ({self.status}): {self.error}"
        if self.status != "ok":
            return f"{self.source:8} {self.status}: {self.error}"
        return (f"{self.source:8} ok: {self.total} items (+{self.added} new, {self.updated} changed, "
                f"-{self.removed} removed) in {self.duration_ms} ms" + (f"  [{self.error}]" if self.error else ""))


def window(now: datetime, sync: SyncSettings) -> tuple[datetime, datetime]:
    """Local-midnight-aligned sync window [today - past_days, today + future_days)."""
    zone = timeutil.tz()
    today = timeutil.to_local(now).date()
    return (timeutil.start_of_day(today - timedelta(days=sync.past_days), zone),
            timeutil.start_of_day(today + timedelta(days=sync.future_days), zone))


def make_client(**kwargs) -> httpx.Client:
    headers = {"User-Agent": USER_AGENT, **kwargs.pop("headers", {})}
    kwargs.setdefault("timeout", httpx.Timeout(20.0, connect=10.0))
    kwargs.setdefault("follow_redirects", False)  # never carry a token to another host
    return httpx.Client(headers=headers, **kwargs)


def apply_result(engine: Engine, source: str, result: FetchResult, now: datetime) -> tuple[int, int, int]:
    """Upsert fetched items and remove covered items that disappeared. Returns (added, updated, removed)."""
    added = updated = removed = 0
    with Session(engine, expire_on_commit=False) as db:
        existing = {i.id: i for i in db.exec(select(Item).where(Item.source == source)).all()}
        seen: set[str] = set()
        for new in result.items:
            if new.source != source:
                raise ValueError(f"{source} connector returned an item for source {new.source!r}")
            if new.id in seen:
                continue  # duplicates in one feed: keep the first
            seen.add(new.id)
            old = existing.get(new.id)
            if old is None:
                new.first_seen_at = now
                new.synced_at = now
                db.add(new)
                added += 1
                continue
            if any(getattr(old, f) != getattr(new, f) for f in CONTENT_FIELDS):
                for f in CONTENT_FIELDS:
                    setattr(old, f, getattr(new, f))
                updated += 1
            old.synced_at = now
        for item_id, old in existing.items():
            if item_id not in seen and any(c.covers(old) for c in result.coverage):
                db.delete(old)
                removed += 1
        db.commit()
    return added, updated, removed


def _backoff(failures: int, sync: SyncSettings) -> timedelta:
    minutes = sync.interval_minutes * (2 ** max(failures - 1, 0))
    return timedelta(minutes=min(minutes, sync.max_backoff_hours * 60))


def sync_source(engine: Engine, connector: Connector, sync: SyncSettings, now: datetime | None = None,
                force: bool = False, dry_run: bool = False) -> SyncReport:
    now = now or timeutil.now()
    source = connector.source
    with Session(engine, expire_on_commit=False) as db:
        state = db.get(SyncState, source) or SyncState(source=source)
    if not force and not dry_run:
        if state.status == "needs_reauth":
            return SyncReport(source, state.status, skipped=True,
                              error=f"sign-in needed: run `atlas auth {source}` (last error: {state.last_error})")
        if state.next_attempt_at and now < state.next_attempt_at:
            return SyncReport(source, state.status, skipped=True,
                              error=f"backing off after {state.consecutive_failures} failure(s) until "
                                    f"{timeutil.fmt_local(state.next_attempt_at)}")

    t0 = time.perf_counter()
    report = SyncReport(source, "ok")
    result: FetchResult | None = None
    try:
        result = connector.fetch(now)
    except SourceError as exc:
        report.status, report.error = exc.status, safe_error(exc)
    except httpx.HTTPError as exc:  # transport problems; message may contain a URL, so don't keep it
        report.status, report.error = "error", f"network error ({type(exc).__name__})"
    except (ValueError, KeyError, TypeError) as exc:
        report.status, report.error = "error", safe_error(f"could not read {source} data: {exc}")
    report.duration_ms = int((time.perf_counter() - t0) * 1000)

    if dry_run:
        report.items = result.items if result else []
        report.total = len(report.items)
        return report

    if result is not None:
        report.added, report.updated, report.removed = apply_result(engine, source, result, now)
        report.total = len(result.items)
        if result.warnings:
            report.error = safe_error("; ".join(result.warnings))

    with Session(engine, expire_on_commit=False) as db:
        state = db.get(SyncState, source) or SyncState(source=source)
        state.last_attempt_at = now
        state.last_duration_ms = report.duration_ms
        state.status = report.status
        if report.status == "ok":
            state.last_success_at = now
            state.consecutive_failures = 0
            state.next_attempt_at = None
            state.item_count = report.total
            state.last_error = f"warning: {report.error}" if report.error else None
        else:
            state.consecutive_failures += 1
            state.last_error = report.error
            # needs_reauth is parked by status; not_configured is cheap to re-check every tick
            backs_off = report.status in ("error", "rate_limited")
            state.next_attempt_at = now + _backoff(state.consecutive_failures, sync) if backs_off else None
        db.add(state)
        db.commit()
    return report


def is_stale(state: SyncState | None, now: datetime, sync: SyncSettings) -> bool:
    """True when a source's data can't be trusted to be current (never synced, failing, or too old)."""
    if state is None or state.last_success_at is None or state.status != "ok":
        return True
    return now - state.last_success_at > timedelta(minutes=3 * sync.interval_minutes)
