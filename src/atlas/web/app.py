"""Local dashboard (bound to 127.0.0.1 by `atlas serve`): Chat (with today's briefing on top) + Metrics.
When `atlas serve` passes a scheduler, it runs for the lifetime of the app (sync + daily briefing)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy.engine import Engine
from sqlmodel import col, select

from atlas import timeutil
from atlas.agent import Assistant
from atlas.config import Settings, get_settings
from atlas.metrics import compute
from atlas.proactive import briefing
from atlas.store import Alert, Approval, Feedback, Interaction, NpsSurvey, get_engine, session_scope

HERE = Path(__file__).parent


class ChatIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    session_id: str | None = Field(default=None, max_length=64)


class DecisionIn(BaseModel):
    approve: bool


class FeedbackIn(BaseModel):
    interaction_id: str
    thumbs: int = Field(ge=-1, le=1)
    csat: int | None = Field(default=None, ge=1, le=5)
    reason: str | None = Field(default=None, max_length=40)


class NpsIn(BaseModel):
    score: int = Field(ge=0, le=10)


class VerdictIn(BaseModel):
    verdict: Literal["accurate", "inaccurate"]
    missed: int | None = Field(default=None, ge=0, le=100)
    note: str | None = Field(default=None, max_length=500)


class AlertFeedbackIn(BaseModel):
    useful: bool


def briefing_json(engine: Engine, b) -> dict[str, Any] | None:
    if b is None:
        return None
    ids = [a["id"] for a in b.content.get("alerts", [])]
    with session_scope(engine) as db:
        rated = {a.id: a.feedback for a in db.exec(select(Alert).where(col(Alert.id).in_(ids))).all()} if ids else {}
    return {"id": b.id, "day": b.day, "trigger": b.trigger, "daily": b.daily_key is not None,
            "created_at": b.created_at, "content": b.content, "verdict": b.verdict, "missed_items": b.missed_items,
            "verdict_note": b.verdict_note, "alert_feedback": rated}


def ollama_status(settings: Settings) -> dict[str, Any]:
    try:
        r = httpx.get(f"{settings.model.host}/api/tags", timeout=2.0)
        r.raise_for_status()
        names = [m.get("name") for m in r.json().get("models", [])]
        return {"reachable": True, "models": names, "model_ready": settings.model.model_id in names}
    except Exception as exc:  # noqa: BLE001
        return {"reachable": False, "models": [], "model_ready": False, "error": type(exc).__name__}


def create_app(engine: Engine | None = None, assistant: Assistant | None = None, settings: Settings | None = None,
               scheduler: Any = None) -> FastAPI:
    settings = settings or get_settings()
    engine = engine or get_engine()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if scheduler is not None:
            scheduler.start()
        try:
            yield
        finally:
            if scheduler is not None:
                scheduler.shutdown()

    app = FastAPI(title="Atlas", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    state: dict[str, Assistant | None] = {"assistant": assistant}

    def get_assistant() -> Assistant:
        if state["assistant"] is None:
            state["assistant"] = Assistant(engine, settings)
        return state["assistant"]

    @app.get("/", response_class=HTMLResponse)
    def chat_page(request: Request):
        return templates.TemplateResponse(request, "chat.html", {"model_id": settings.model.model_id, "page": "chat"})

    @app.get("/metrics", response_class=HTMLResponse)
    def metrics_page(request: Request, days: int = 7):
        kpis = compute(engine, days=max(1, min(days, 90)))
        return templates.TemplateResponse(request, "metrics.html", {"k": kpis, "page": "metrics", "model_id": settings.model.model_id})

    @app.get("/api/health")
    def health():
        jobs = {j.id: timeutil.fmt_local(j.next_run_time) for j in scheduler.jobs()} if scheduler else {}
        return {"model_id": settings.model.model_id, "ollama": ollama_status(settings),
                "scheduler": {"running": bool(jobs), "next_runs": jobs,
                              "status": scheduler.status if scheduler else {}}}

    @app.get("/api/briefing")
    def get_briefing():
        return {"briefing": briefing_json(engine, briefing.latest(engine, timeutil.to_local(timeutil.now()).date().isoformat())
                                          or briefing.latest(engine))}

    @app.post("/api/briefing")
    def make_briefing():  # on demand, from stored data (the scheduler keeps it synced)
        made = briefing.generate(engine, settings, timeutil.now(), trigger="manual", daily=False)
        return {"briefing": briefing_json(engine, made)}

    @app.post("/api/briefing/{briefing_id}/verdict")
    def verdict(briefing_id: str, body: VerdictIn):
        b = briefing.set_verdict(engine, briefing_id, body.verdict, body.missed, body.note, timeutil.now())
        if b is None:
            raise HTTPException(404, "unknown briefing")
        return {"ok": True, "verdict": b.verdict}

    @app.post("/api/alerts/{alert_id}/feedback")
    def alert_feedback(alert_id: str, body: AlertFeedbackIn):
        with session_scope(engine) as db:
            alert = db.get(Alert, alert_id)
            if alert is None:
                raise HTTPException(404, "unknown alert")
            alert.feedback = "useful" if body.useful else "not_useful"
            db.add(alert)
            db.commit()
        return {"ok": True}

    @app.post("/api/chat")
    def chat(body: ChatIn):  # sync def -> runs in FastAPI's threadpool; the agent call is blocking
        result = get_assistant().handle(body.text.strip(), context="chat", session_id=body.session_id)
        return asdict(result)

    @app.post("/api/session/{session_id}/reset")
    def reset(session_id: str):
        get_assistant().reset_session(session_id)
        return {"ok": True}

    @app.get("/api/approvals")
    def approvals():
        return [
            {"id": a.id, "tool": a.tool, "args": a.args, "why": (a.reason or {}).get("why"), "created_at": a.created_at}
            for a in get_assistant().pending_approvals()
        ]

    @app.post("/api/approvals/{approval_id}")
    def decide(approval_id: str, body: DecisionIn):
        with session_scope(engine) as db:
            appr = db.get(Approval, approval_id)
            if appr is None:
                raise HTTPException(404, "unknown approval")
            if appr.status != "pending":  # replayed or double-clicked decisions are refused
                raise HTTPException(409, f"approval already {appr.status}")
        result = get_assistant().decide(approval_id, body.approve)
        if result is not None:
            return asdict(result)
        with session_scope(engine) as db:
            return {"status": db.get(Approval, approval_id).status}

    @app.post("/api/feedback")
    def feedback(body: FeedbackIn):
        with session_scope(engine) as db:
            if db.get(Interaction, body.interaction_id) is None:
                raise HTTPException(404, "unknown interaction")
            db.add(Feedback(**body.model_dump()))
            db.commit()
        return {"ok": True}

    @app.post("/api/nps")
    def nps(body: NpsIn):
        with session_scope(engine) as db:
            db.add(NpsSurvey(score=body.score))
            db.commit()
        return {"ok": True}

    @app.get("/api/kpis")
    def kpis(days: int = 7):
        return compute(engine, days=max(1, min(days, 90)))

    return app
