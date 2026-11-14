"""A scripted stand-in for the LLM so the pipeline can be tested without Ollama."""

from __future__ import annotations

import json
from typing import Any

from strands.models.model import Model


class ScriptedModel(Model):
    """Replays turns in order. A turn is {"text": str}, {"tool": name, "input": {...}} or
    {"tools": [(name, input), ...]} for parallel tool calls. Raises if it runs out of turns."""

    def __init__(self, turns: list[dict], model_id: str = "scripted") -> None:
        self.turns = list(turns)
        self.config: dict[str, Any] = {"model_id": model_id}
        self.calls = 0
        self.seen_tool_results: list[dict] = []

    def update_config(self, **kw: Any) -> None:
        self.config.update(kw)

    def get_config(self) -> dict:
        return self.config

    async def structured_output(self, *args: Any, **kwargs: Any):  # pragma: no cover
        raise NotImplementedError

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        self.calls += 1
        for msg in messages[-1:]:
            for block in msg.get("content", []):
                if "toolResult" in block:
                    self.seen_tool_results.append(block["toolResult"])
        if not self.turns:
            raise RuntimeError("ScriptedModel ran out of turns")
        turn = self.turns.pop(0)
        yield {"messageStart": {"role": "assistant"}}
        calls = turn.get("tools") or ([(turn["tool"], turn.get("input", {}))] if "tool" in turn else [])
        if calls:
            for n, (name, inp) in enumerate(calls):
                yield {"contentBlockStart": {"start": {"toolUse": {"name": name, "toolUseId": f"tu{self.calls}_{n}"}}}}
                yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(inp)}}}}
                yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}
        else:
            yield {"contentBlockStart": {"start": {}}}
            yield {"contentBlockDelta": {"delta": {"text": turn["text"]}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "end_turn"}}
        yield {"metadata": {"usage": {"inputTokens": 100, "outputTokens": 20, "totalTokens": 120}, "metrics": {"latencyMs": 5}}}
