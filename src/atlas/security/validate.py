"""Input validation for tool arguments. Tools call these instead of trusting model-provided values."""

from __future__ import annotations

from datetime import datetime

from atlas import timeutil

MAX_RANGE_DAYS = 120
MAX_QUERY_LEN = 200
ALLOWED_SOURCES = {"canvas", "gcal", "routine", "gmail", "github", "outlook", "linkedin", "demo"}


class ToolInputError(ValueError):
    """Raised for bad tool input; the message is returned to the model so it can retry or ask the user."""


def date_range(start: str | None, end: str | None) -> tuple[datetime, datetime]:
    """Resolve model-provided phrases into a bounded local range."""
    try:
        if start and end:
            s, _ = timeutil.resolve_range(start)
            _, e = timeutil.resolve_range(end)
        elif start:
            s, e = timeutil.resolve_range(start)
        elif end:
            s, _ = timeutil.resolve_range("today")
            _, e = timeutil.resolve_range(end)
        else:
            s, e = timeutil.resolve_range(None)
    except ValueError as exc:
        raise ToolInputError(str(exc)) from exc
    if e <= s:
        raise ToolInputError(f"end ({end!r}) is before start ({start!r})")
    if (e - s).days > MAX_RANGE_DAYS:
        raise ToolInputError(f"range too long; ask for at most {MAX_RANGE_DAYS} days")
    return s, e


def query(text: str) -> str:
    text = (text or "").strip()
    if not text:
        raise ToolInputError("query must not be empty")
    return text[:MAX_QUERY_LEN]


def source(name: str | None) -> str | None:
    if name in (None, "", "any", "all"):
        return None
    name = name.lower().strip()
    if name not in ALLOWED_SOURCES:
        raise ToolInputError(f"unknown source {name!r}; use one of {sorted(ALLOWED_SOURCES)}")
    return name
