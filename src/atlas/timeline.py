"""Deterministic schedule logic (no LLM): intervals and overlaps."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable

from atlas.store.models import Item

DEFAULT_DURATION = timedelta(hours=1)
IGNORED_STATUSES = {"cancelled", "declined"}


@dataclass(frozen=True)
class Overlap:
    a: Item
    b: Item
    start: datetime
    end: datetime

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)

    @property
    def involves_optional(self) -> bool:
        return "optional" in {self.a.status, self.b.status} or "office_hours" in {self.a.kind, self.b.kind}


def interval(item: Item) -> tuple[datetime, datetime] | None:
    """Timed interval for an item, or None for points (deadlines), all-day items and cancelled items."""
    if item.start is None or item.all_day or (item.status or "") in IGNORED_STATUSES:
        return None
    end = item.end if item.end and item.end > item.start else item.start + DEFAULT_DURATION
    return item.start, end


def find_overlaps(items: Iterable[Item]) -> list[Overlap]:
    timed = sorted(((iv, it) for it in items if (iv := interval(it))), key=lambda x: x[0][0])
    out: list[Overlap] = []
    for i, ((s1, e1), a) in enumerate(timed):
        for (s2, e2), b in timed[i + 1 :]:
            if s2 >= e1:
                break  # sorted by start: nothing later can overlap a
            if a.id == b.id:
                continue
            out.append(Overlap(a, b, max(s1, s2), min(e1, e2)))
    return out
