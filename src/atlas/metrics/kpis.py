"""The 7 KPIs (docs/04-metrics.md), computed from SQLite. Phases 1–3 record baselines; targets are aspirational."""

from __future__ import annotations

import math
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy.engine import Engine
from sqlmodel import col, select

from atlas.config import load_time_baselines
from atlas.store import Alert, Approval, Briefing, Feedback, Interaction, NpsSurvey, session_scope, utcnow

FINAL_STATUSES = {"completed", "completed_with_approval", "clarified", "failed", "escalated"}
FALLBACK_STATUSES = {"clarified", "failed", "escalated"}
CORRECTION_MINUTES = 1.0
GRADUATION_MIN_APPROVALS = 20

TARGETS = {
    "intent_accuracy": 0.90,
    "slot_accuracy": 0.85,
    "completion_rate": 0.80,
    "fallback_rate": 0.10,  # lower is better
    "read_p50_ms": 3000,
    "alert_precision": 0.70,
    "csat": 4.2,
}


def percentile(values: list[float], p: float) -> float | None:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    k = (len(vals) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    return vals[lo] if lo == hi else vals[lo] + (vals[hi] - vals[lo]) * (k - lo)


def _rate(num: int, den: int) -> float | None:
    return round(num / den, 3) if den else None


def _latency(rows: list[Interaction]) -> dict[str, dict[str, float | None]]:
    out = {}
    for field in ("e2e_ms", "model_ms", "tool_ms", "ttfb_ms"):
        vals = [getattr(r, field) for r in rows if getattr(r, field) is not None]
        out[field] = {"p50": percentile(vals, 0.5), "p95": percentile(vals, 0.95), "n": len(vals)}
    return out


def time_saved_minutes(rows: list[Interaction], useful_alerts: int, thumbs_down: int, baselines: dict) -> float:
    tasks = baselines.get("tasks", {})
    mapping = baselines.get("tool_to_task", {})
    total = 0.0
    for r in rows:
        if r.status not in ("completed", "completed_with_approval"):
            continue
        for task in {mapping.get(t) for t in (r.tools_used or [])} - {None}:
            total += float(tasks.get(task, 0.0))
    total += useful_alerts * float(tasks.get("alert_useful", 0.0))
    total -= thumbs_down * CORRECTION_MINUTES
    return round(total, 1)


GATE_STREAK_DAYS = 7  # Phase 2 gate: 7 consecutive daily briefings verified accurate with nothing missed


def accurate_streak(daily: list[Briefing]) -> int:
    """Consecutive days, ending at the latest verified daily briefing, that were checked and fully accurate.
    Today's briefing not being checked yet doesn't break the streak; a gap day or an inaccurate one does."""
    ordered = sorted(daily, key=lambda b: b.day, reverse=True)
    if ordered and ordered[0].verdict is None:
        ordered = ordered[1:]
    streak, prev = 0, None
    for b in ordered:
        day = date.fromisoformat(b.day)
        if prev is not None and (prev - day).days != 1:
            break
        if b.verdict != "accurate" or (b.missed_items or 0) > 0:
            break
        streak, prev = streak + 1, day
    return streak


def briefing_stats(window: list[Briefing], all_daily: list[Briefing]) -> dict[str, Any]:
    daily = [b for b in window if b.daily_key]
    verified = [b for b in daily if b.verdict]
    accurate = [b for b in verified if b.verdict == "accurate" and not (b.missed_items or 0)]
    return {
        "daily": len(daily),
        "on_time": sum(b.trigger == "scheduled" for b in daily),
        "catch_up": sum(b.trigger == "catch_up" for b in daily),
        "verified": len(verified),
        "accurate": len(accurate),
        "missed_items": sum(b.missed_items or 0 for b in verified),
        "accuracy": _rate(len(accurate), len(verified)),
        "stale_days": sum(bool(b.stale_sources) for b in daily),
        "streak": accurate_streak(all_daily),
        "gate_days": GATE_STREAK_DAYS,
    }


def compute(engine: Engine, days: int = 7, now: datetime | None = None) -> dict[str, Any]:
    now = now or utcnow()
    since = now - timedelta(days=days)
    baselines = load_time_baselines()
    with session_scope(engine) as db:
        rows = list(db.exec(select(Interaction).where(col(Interaction.created_at) >= since, Interaction.context != "eval")).all())
        feedback = list(db.exec(select(Feedback).where(col(Feedback.created_at) >= since)).all())
        alerts = list(db.exec(select(Alert).where(col(Alert.created_at) >= since)).all())
        nps = list(db.exec(select(NpsSurvey).order_by(col(NpsSurvey.created_at).desc()).limit(8)).all())
        approvals = list(db.exec(select(Approval)).all())
        briefings = list(db.exec(select(Briefing).where(col(Briefing.created_at) >= since)).all())
        all_daily = list(db.exec(select(Briefing).where(col(Briefing.daily_key).is_not(None))).all())
    brief = briefing_stats(briefings, all_daily)
    # A daily briefing replaces the manual morning check, unless the user found it wrong
    briefing_minutes = float(baselines.get("tasks", {}).get("morning_check", 0.0)) * sum(
        1 for b in briefings if b.daily_key and b.verdict != "inaccurate")

    final = [r for r in rows if r.status in FINAL_STATUSES]
    statuses = Counter(r.status for r in rows)
    # Answer latency for read questions: chat runs that produced an answer (failed runs aren't answers) with no approval wait.
    reads = [
        r for r in final
        if r.context == "chat" and r.status != "failed"
        and not any(a.get("decision") == "approved" for a in (r.tool_args or []))
    ]

    ups = sum(1 for f in feedback if f.thumbs > 0)
    downs = sum(1 for f in feedback if f.thumbs < 0)
    csat_scores = [f.csat for f in feedback if f.csat]
    misunderstood = sum(1 for f in feedback if f.reason == "misunderstood")

    sent_alerts = [a for a in alerts if a.notified_at]
    rated_alerts = [a for a in sent_alerts if a.feedback or a.acted]
    useful_alerts = sum(1 for a in sent_alerts if a.feedback == "useful" or a.acted)

    per_tool: dict[str, Counter] = {}
    for a in approvals:
        per_tool.setdefault(a.tool, Counter())[a.status] += 1

    return {
        "window_days": days,
        "interactions": len(rows),
        "status_counts": dict(statuses),
        # 1. Intent & slot accuracy: offline from the golden set (atlas bench / evals); online proxy below
        "intent_online": {"misunderstood_feedback": misunderstood, "rated": ups + downs},
        # 2. Task completion
        "completion_rate": _rate(statuses["completed"] + statuses["completed_with_approval"], len(final)),
        # 3. Autonomy
        # L3 = proactive: daily briefings delivered without being asked
        "autonomy_distribution": dict(sorted((Counter(f"L{r.autonomy_level}" for r in final if r.autonomy_level is not None)
                                              + Counter({"L3": brief["daily"]})).items())),
        "graduation": {
            tool: {
                "approved": c["approved"],
                "rejected": c["rejected"],
                "eligible": c["approved"] >= GRADUATION_MIN_APPROVALS and c["rejected"] == 0,
            }
            for tool, c in per_tool.items()
        },
        # 4. Latency (split)
        "latency_ms": _latency(final),
        "read_latency_ms": _latency(reads),
        # Kept visible on purpose: a system shouldn't look fast just because it fails fast.
        "failed_latency_ms": _latency([r for r in final if r.status == "failed"]),
        # 5. Fallback & escalation
        "fallback_rate": _rate(sum(statuses[s] for s in FALLBACK_STATUSES), len(final)),
        # 6. Useful proactivity
        "alerts": {
            "sent": len(sent_alerts),
            "toasts": sum(1 for a in sent_alerts if a.channel == "toast"),
            "rated": len(rated_alerts),
            "precision": _rate(useful_alerts, len(rated_alerts)),
        },
        "briefings": brief,
        # 7. Time saved
        "time_saved_hours": round((time_saved_minutes(final, useful_alerts, downs, baselines) + briefing_minutes) / 60.0, 2),
        # 8. CSAT / NPS
        "csat": {
            "thumbs_up_rate": _rate(ups, ups + downs),
            "avg_score": round(sum(csat_scores) / len(csat_scores), 2) if csat_scores else None,
            "responses": ups + downs,
        },
        "self_nps_trend": [n.score for n in reversed(nps)],
        "targets": TARGETS,
    }
