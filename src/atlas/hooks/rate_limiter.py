"""Throttle model calls (needed for free cloud tiers; a no-op for local Ollama). Retries on throttling
errors are handled separately by Strands' ModelRetryStrategy."""

from __future__ import annotations

from typing import Any

from strands.hooks import BeforeModelCallEvent, HookProvider, HookRegistry

from atlas.ratelimit import TokenBucket


class RateLimitHook(HookProvider):
    def __init__(self, bucket: TokenBucket) -> None:
        self.bucket = bucket
        self.total_wait_s = 0.0

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeModelCallEvent, self.before_model, order=-10)

    def before_model(self, event: BeforeModelCallEvent) -> None:
        self.total_wait_s += self.bucket.acquire()
