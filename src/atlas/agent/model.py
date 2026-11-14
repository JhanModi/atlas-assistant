"""Model factory. Only local providers are constructed: private data never leaves the laptop (docs/03-security.md)."""

from __future__ import annotations

from functools import lru_cache

import httpx
from strands.models.model import Model

from atlas.config import ModelSettings


@lru_cache(maxsize=16)
def thinking_mode(host: str, model_id: str) -> bool | None:
    """The `think` value to send for this model, from Ollama's /api/show.

    - None:  the model has no thinking capability, so don't send the flag at all.
    - False: thinking can be switched off (fastest; e.g. qwen3.5:4b, qwen3:8b).
    - True:  the model *only* thinks (e.g. qwen3:4b reports thinking values [true]). Sending False makes it
             reason anyway, inside the visible answer; True keeps the reasoning in a separate field Atlas ignores.
    """
    try:
        r = httpx.post(f"{host}/api/show", json={"model": model_id}, timeout=10.0)
        r.raise_for_status()
        info = r.json()
    except Exception:  # noqa: BLE001  (Ollama down: the request will fail later anyway)
        return False
    if "thinking" not in (info.get("capabilities") or []):
        return None
    values = (info.get("thinking") or {}).get("values")
    if values is None:  # older Ollama without the field: assume the toggle works
        return False
    return False if False in values else True


def resolve_think(ms: ModelSettings, model_id: str) -> bool | None:
    if ms.think == "auto":
        return thinking_mode(ms.host, model_id)
    return ms.think


def build_model(ms: ModelSettings, model_id: str | None = None) -> Model:
    if ms.provider != "ollama":
        raise ValueError(f"unsupported provider {ms.provider!r}: only local 'ollama' may see private data")
    from strands.models.ollama import OllamaModel

    model_id = model_id or ms.model_id
    think = resolve_think(ms, model_id)
    return OllamaModel(
        host=ms.host,
        model_id=model_id,
        temperature=ms.temperature,
        keep_alive=ms.keep_alive,
        options={"num_ctx": ms.num_ctx},
        additional_args={"think": think} if think is not None else None,
    )
