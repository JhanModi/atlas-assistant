"""Append-only JSONL audit log of every gate decision (data/audit.log)."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

_lock = threading.Lock()


class AuditLog:
    def __init__(self, path: Path | None) -> None:
        self.path = path
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, **event) -> None:
        if self.path is None:
            return
        event = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **event}
        line = json.dumps(event, default=str, ensure_ascii=False)
        with _lock, open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
