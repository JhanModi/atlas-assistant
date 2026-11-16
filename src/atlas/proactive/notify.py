"""Desktop notifications. A failed toast must never break a sync or a briefing, so send() never raises."""

from __future__ import annotations

import sys
from typing import Protocol

from atlas.security.sanitize import clean_title


class Notifier(Protocol):
    def send(self, title: str, body: str, url: str | None = None) -> bool: ...


class ToastNotifier:
    """Windows toast via win11toast (non-blocking `notify`). Clicking it opens the local dashboard."""

    def send(self, title: str, body: str, url: str | None = None) -> bool:
        if sys.platform != "win32":
            return False
        try:
            from win11toast import notify

            notify(title=clean_title(title, 80), body=clean_title(body, 240), on_click=url or "")
            return True
        except Exception:  # noqa: BLE001 - notification problems are not worth crashing the scheduler for
            return False


class NullNotifier:
    def send(self, title: str, body: str, url: str | None = None) -> bool:
        return False


class RecordingNotifier:
    """For tests: remembers what would have been shown."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str | None]] = []

    def send(self, title: str, body: str, url: str | None = None) -> bool:
        self.sent.append((title, body, url))
        return True
