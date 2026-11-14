"""Bind an approval to the exact action the user saw."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def args_digest(args: dict[str, Any] | None) -> str:
    canonical = json.dumps(args or {}, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
