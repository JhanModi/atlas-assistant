"""SQLite tables. All datetimes are stored as UTC and returned timezone-aware (UTC)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import JSON, Column
from sqlalchemy.types import DateTime, TypeDecorator
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """Stores aware datetimes as naive UTC; returns aware UTC. Rejects naive input."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime given to the store; attach a timezone first")
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc)


def _dt(nullable: bool = True, index: bool = False, default_now: bool = False):
    kwargs: dict[str, Any] = {"sa_type": UTCDateTime, "nullable": nullable, "index": index}
    if default_now:
        return Field(default_factory=utcnow, **kwargs)
    return Field(default=None, **kwargs)


def _json(default_factory=dict):
    return Field(default_factory=default_factory, sa_column=Column(JSON))


class Item(SQLModel, table=True):
    """One normalized thing from any source: lecture, event, deadline, announcement, message..."""

    id: str = Field(primary_key=True)  # "<source>:<source_id>"
    source: str = Field(index=True)  # canvas | gcal | routine | gmail | github | outlook | linkedin | demo
    source_id: str
    kind: str = Field(index=True)  # lecture | event | deadline | announcement | message | notification | office_hours
    title: str
    start: Optional[datetime] = _dt(index=True)
    end: Optional[datetime] = _dt()
    due: Optional[datetime] = _dt(index=True)
    all_day: bool = False
    course: Optional[str] = Field(default=None, index=True)
    location: Optional[str] = None
    url: Optional[str] = None
    status: Optional[str] = None  # e.g. submitted | missing | graded | optional | cancelled
    body: Optional[str] = None  # free text written by others -> untrusted
    private: bool = True
    trust: str = "trusted"  # trusted | untrusted (body present from a third party)
    updated_at: Optional[datetime] = _dt()
    synced_at: datetime = _dt(nullable=False, default_now=True)


class SyncState(SQLModel, table=True):
    source: str = Field(primary_key=True)
    cursor: Optional[str] = None
    last_success_at: Optional[datetime] = _dt()
    last_error: Optional[str] = None
    last_duration_ms: Optional[int] = None


class Interaction(SQLModel, table=True):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex, primary_key=True)
    created_at: datetime = _dt(nullable=False, index=True, default_now=True)
    context: str = "chat"  # chat | briefing | proactive | eval
    session_id: Optional[str] = None
    user_text: str = ""
    response_text: Optional[str] = None
    # completed | completed_with_approval | awaiting_approval | clarified | failed | escalated
    status: str = "running"
    autonomy_level: Optional[int] = None  # 0..4, see docs/04-metrics.md
    tools_used: list = _json(list)
    tool_args: list = _json(list)
    model_id: Optional[str] = None
    model_ms: Optional[float] = None
    tool_ms: Optional[float] = None
    e2e_ms: Optional[float] = None
    ttfb_ms: Optional[float] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cycle_count: Optional[int] = None
    error: Optional[str] = None


class ToolCall(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    interaction_id: str = Field(index=True)
    created_at: datetime = _dt(nullable=False, default_now=True)
    tool: str
    tier: str
    decision: str  # auto | approved | rejected | blocked_allowlist | blocked_tier
    args: dict = _json(dict)
    duration_ms: Optional[float] = None
    status: Optional[str] = None  # success | error | cancelled


class Approval(SQLModel, table=True):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex, primary_key=True)
    interaction_id: str = Field(index=True)
    interrupt_id: str
    tool: str
    args: dict = _json(dict)
    reason: dict = _json(dict)
    status: str = "pending"  # pending | approved | rejected | expired
    created_at: datetime = _dt(nullable=False, default_now=True)
    decided_at: Optional[datetime] = _dt()


class Alert(SQLModel, table=True):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex, primary_key=True)
    detector: str = Field(index=True)  # due_soon | conflict | new_announcement | ...
    dedupe_key: str = Field(index=True, unique=True)
    title: str
    detail: Optional[str] = None
    item_ids: list = _json(list)
    urgent: bool = False
    created_at: datetime = _dt(nullable=False, default_now=True)
    notified_at: Optional[datetime] = _dt()
    feedback: Optional[str] = None  # useful | not_useful
    acted: bool = False


class Feedback(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    interaction_id: str = Field(index=True)
    thumbs: int  # +1 / -1
    csat: Optional[int] = None  # 1..5
    reason: Optional[str] = None  # e.g. misunderstood | wrong_data | too_slow | other
    comment: Optional[str] = None
    created_at: datetime = _dt(nullable=False, default_now=True)


class NpsSurvey(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    score: int  # 0..10
    created_at: datetime = _dt(nullable=False, default_now=True)
