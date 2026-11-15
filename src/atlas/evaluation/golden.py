"""Golden-set evaluation: intent accuracy (tool chosen) and slot accuracy (tool arguments).

Each case in evals/datasets/golden.jsonl:
  {"id", "label", "utterance", "tool": <expected tool or null for out-of-scope>, "args": {...},
   "accept": [alternative acceptable first tools], "expect_all": [tools that must all be called]}
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.engine import Engine

from atlas.agent import Assistant
from atlas.agent.slots import slot_score
from atlas.config import project_root
from atlas.metrics import percentile
from atlas.store import Interaction, session_scope

DEFAULT_PATH = Path("evals") / "datasets" / "golden.jsonl"


def load_cases(path: Path | None = None) -> list[dict]:
    path = path or project_root() / DEFAULT_PATH
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


@dataclass
class CaseResult:
    case: dict
    tools_called: list[str]
    first_args: dict
    status: str
    reply: str
    intent_ok: bool
    slots_matched: int
    slots_expected: int
    metrics: dict[str, Any] = field(default_factory=dict)


def judge_intent(case: dict, tools_called: list[str]) -> bool:
    expected = case.get("tool")
    first = tools_called[0] if tools_called else None
    if expected is None:
        return first is None
    called = set(tools_called)
    if case.get("expect_all"):
        return set(case["expect_all"]) <= called or (bool(case.get("accept")) and set(case["accept"]) <= called)
    return first == expected or first in (case.get("accept") or [])


def run_case(assistant: Assistant, engine: Engine, case: dict) -> CaseResult:
    result = assistant.handle(case["utterance"], context="eval", session_id=f"eval-{uuid.uuid4().hex[:8]}")
    with session_scope(engine) as db:
        inter = db.get(Interaction, result.interaction_id)
        calls = list(inter.tool_args or [])
    tools_called = [c["tool"] for c in calls]
    first_args = calls[0]["args"] if calls else {}
    intent_ok = judge_intent(case, tools_called)
    matched = expected = 0
    if case.get("tool") and not case.get("expect_all"):
        if tools_called and tools_called[0] == case["tool"]:
            matched, expected = slot_score(case["tool"], case.get("args") or {}, first_args)
        elif not (tools_called and tools_called[0] in (case.get("accept") or [])):
            _, expected = slot_score(case["tool"], case.get("args") or {}, case.get("args") or {})
    return CaseResult(case, tools_called, first_args, result.status, result.reply, intent_ok, matched, expected, result.metrics)


def summarize(results: list[CaseResult]) -> dict[str, Any]:
    n = len(results)
    by_label: dict[str, list[CaseResult]] = {}
    for r in results:
        by_label.setdefault(r.case.get("label", "?"), []).append(r)
    slots_m = sum(r.slots_matched for r in results)
    slots_e = sum(r.slots_expected for r in results)
    e2e = [r.metrics.get("e2e_ms") for r in results if r.metrics.get("e2e_ms") is not None]
    ttfb = [r.metrics.get("ttfb_ms") for r in results if r.metrics.get("ttfb_ms") is not None]
    model = [r.metrics.get("model_ms") for r in results if r.metrics.get("model_ms") is not None]
    tool = [r.metrics.get("tool_ms") for r in results if r.metrics.get("tool_ms") is not None]
    return {
        "cases": n,
        "intent_accuracy": round(sum(r.intent_ok for r in results) / n, 3) if n else None,
        "slot_accuracy": round(slots_m / slots_e, 3) if slots_e else None,
        "by_label": {k: round(sum(r.intent_ok for r in v) / len(v), 3) for k, v in sorted(by_label.items())},
        "failed_runs": sum(r.status == "failed" for r in results),
        "e2e_ms": {"p50": percentile(e2e, 0.5), "p95": percentile(e2e, 0.95)},
        "ttfb_ms": {"p50": percentile(ttfb, 0.5), "p95": percentile(ttfb, 0.95)},
        "model_ms": {"p50": percentile(model, 0.5), "p95": percentile(model, 0.95)},
        "tool_ms": {"p50": percentile(tool, 0.5), "p95": percentile(tool, 0.95)},
        "misses": [
            {"id": r.case["id"], "utterance": r.case["utterance"], "expected": r.case.get("tool"),
             "got": r.tools_called, "args": r.first_args, "status": r.status}
            for r in results if not r.intent_ok
        ],
        # Right tool, wrong arguments (wrong-tool cases already appear under misses)
        "slot_misses": [
            {"id": r.case["id"], "utterance": r.case["utterance"], "expected_args": r.case.get("args"),
             "got_args": r.first_args, "matched": f"{r.slots_matched}/{r.slots_expected}"}
            for r in results if r.intent_ok and r.slots_matched < r.slots_expected
        ],
    }
