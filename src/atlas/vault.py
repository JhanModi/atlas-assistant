"""Secrets live in the OS credential store (Windows Credential Manager) via keyring, never in files or the DB.

The user enters every token themselves (`atlas auth ...`). Code only reads them back. Names in use:
  canvas.token          Canvas personal access token (API mode)
  canvas.ics_url        Canvas calendar feed URL (ICS mode; the URL itself is the secret)
  gcal.client_id / gcal.client_secret / gcal.refresh_token   Google OAuth (API mode)
  gcal.ics_url          Google Calendar "secret address in iCal format" (ICS mode)
"""

from __future__ import annotations

from typing import Callable, Protocol

SERVICE = "atlas-assistant"
KNOWN = {
    "canvas": ["canvas.token", "canvas.ics_url"],
    "gcal": ["gcal.client_id", "gcal.client_secret", "gcal.refresh_token", "gcal.ics_url"],
}


class SecretStore(Protocol):
    def get(self, name: str) -> str | None: ...
    def set(self, name: str, value: str) -> None: ...
    def delete(self, name: str) -> None: ...


class KeyringStore:
    def get(self, name: str) -> str | None:
        import keyring

        return keyring.get_password(SERVICE, name)

    def set(self, name: str, value: str) -> None:
        import keyring

        if not value or not value.strip():
            raise ValueError(f"refusing to store an empty secret for {name}")
        keyring.set_password(SERVICE, name, value.strip())

    def delete(self, name: str) -> None:
        import keyring
        from keyring.errors import PasswordDeleteError

        try:
            keyring.delete_password(SERVICE, name)
        except PasswordDeleteError:
            pass


class MemoryStore:
    """For tests."""

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = dict(values or {})

    def get(self, name: str) -> str | None:
        return self.values.get(name)

    def set(self, name: str, value: str) -> None:
        self.values[name] = value

    def delete(self, name: str) -> None:
        self.values.pop(name, None)


_store_factory: Callable[[], SecretStore] = KeyringStore


def get_store() -> SecretStore:
    return _store_factory()
