"""The 7 KPIs (docs/04-metrics.md), computed from SQLite. Phases 1–3 record baselines; targets are aspirational."""

from __future__ import annotations

import math
from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.engine import Engine
from sqlmodel import col, select

from atlas.config import load_time_baselines
from atlas.store import Alert, Approval, Feedback, Interaction, NpsSurvey, session_scope, utcnow

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
        "autonomy_distribution": dict(sorted(Counter(f"L{r.autonomy_level}" for r in final if r.autonomy_level is not None).items())),
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
            "rated": len(rated_alerts),
            "precision": _rate(useful_alerts, len(rated_alerts)),
        },
        # 7. Time saved
        "time_saved_hours": round(time_saved_minutes(final, useful_alerts, downs, baselines) / 60.0, 2),
        # 8. CSAT / NPS
        "csat": {
            "thumbs_up_rate": _rate(ups, ups + downs),
            "avg_score": round(sum(csat_scores) / len(csat_scores), 2) if csat_scores else None,
            "responses": ups + downs,
        },
        "self_nps_trend": [n.score for n in reversed(nps)],
        "targets": TARGETS,
    }
