"""Slot resolution shared by the tools and the evaluator, so "slot accuracy" measures exactly what the
tools would do with the model's arguments."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from atlas.security import validate


def agenda_range(start: str | None = "today", end: str | None = "") -> tuple[datetime, datetime]:
    return validate.date_range(start or "today", end or None)


def deadline_range(start: str | None = "today", end: str | None = "") -> tuple[datetime, datetime]:
    if (start or "today").strip().lower() == "today" and not end:
        return validate.date_range("today", "next 14 days")  # "what's due?" means the next two weeks
    return validate.date_range(start or "today", end or None)


def conflict_range(start: str | None = "this week", end: str | None = "") -> tuple[datetime, datetime]:
    return validate.date_range(start or "this week", end or None)


def routine_range(day: str | None = "this week") -> tuple[datetime, datetime]:
    return validate.date_range(day or "this week", None)


def _norm(text: Any) -> str:
    return "".join(str(text or "").lower().split())


def canonical(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """Resolve a tool call's arguments to comparable slot values. Raises ToolInputError on bad input."""
    args = args or {}
    if tool == "get_agenda":
        s, e = agenda_range(args.get("start", "today"), args.get("end", ""))
    elif tool == "get_deadlines":
        s, e = deadline_range(args.get("start", "today"), args.get("end", ""))
        out = {"from": s.isoformat(), "to": e.isoformat()}
        if args.get("course"):
            out["course"] = _norm(args["course"])
        return out
    elif tool == "find_conflicts":
        s, e = conflict_range(args.get("start", "this week"), args.get("end", ""))
    elif tool == "get_routine":
        s, e = routine_range(args.get("day", "this week"))
    elif tool == "search_items":
        return {"query": _norm(args.get("query", ""))}
    else:
        return {}
    return {"from": s.isoformat(), "to": e.isoformat()}


def slot_score(tool: str, expected_args: dict[str, Any], actual_args: dict[str, Any]) -> tuple[int, int]:
    """(matched, expected) slot counts. A query slot matches if every expected keyword appears in the actual query."""
    try:
        exp = canonical(tool, expected_args)
    except validate.ToolInputError:
        return 0, 0
    if not any(k in (expected_args or {}) for k in ("start", "end", "day")):
        # The golden case doesn't pin a date range ("what's coming up?"): any sensible range is fine.
        exp.pop("from", None)
        exp.pop("to", None)
    if not exp:
        return 0, 0
    try:
        act = canonical(tool, actual_args)
    except validate.ToolInputError:
        return 0, len(exp)
    matched = 0
    for key, value in exp.items():
        if key == "query":
            words = str(expected_args.get("query", "")).lower().split()
            matched += int(all(w in str(actual_args.get("query", "")).lower() for w in words))
        elif key == "course":
            matched += int(value in act.get("course", "") or act.get("course", "") in value and bool(act.get("course")))
        else:
            matched += int(act.get(key) == value)
    return matched, len(exp)
