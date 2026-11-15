"""Source connectors (Phase 2): routine.yaml, Canvas (API or ICS feed) and Google Calendar (API or ICS feed)."""

from __future__ import annotations

from atlas.config import Settings
from atlas.connectors.base import (AuthError, Connector, Coverage, FetchResult, NotConfigured, RateLimited,
                                   SourceError, SyncReport, apply_result, is_stale, make_client, sync_source, window)
from atlas.vault import SecretStore, get_store

SOURCES = ("routine", "canvas", "gcal")


def build_connectors(settings: Settings, store: SecretStore | None = None, only: str | None = None,
                     client_factory=make_client) -> list[Connector]:
    """The enabled connectors, in a stable order. `only` picks one source even if it is disabled."""
    from atlas.connectors.canvas import CanvasConnector
    from atlas.connectors.gcal import GoogleCalendarConnector
    from atlas.connectors.ics_feed import IcsFeedConnector
    from atlas.connectors.routine import RoutineConnector

    store = store or get_store()
    src, sync = settings.sources, settings.sync
    out: list[Connector] = []
    if (only or "routine") == "routine" and (src.routine.enabled or only == "routine"):
        out.append(RoutineConnector(src.routine.path, sync))
    if (only or "canvas") == "canvas" and (src.canvas.enabled or only == "canvas"):
        if src.canvas.mode == "ics":
            out.append(IcsFeedConnector("canvas", store.get("canvas.ics_url"), sync, "canvas",
                                        "atlas auth canvas --ics", client_factory))
        else:
            out.append(CanvasConnector(src.canvas.base_url, store, sync, client_factory))
    if (only or "gcal") == "gcal" and (src.gcal.enabled or only == "gcal"):
        if src.gcal.mode == "ics":
            out.append(IcsFeedConnector("gcal", store.get("gcal.ics_url"), sync, "generic",
                                        "atlas auth gcal --ics", client_factory))
        else:
            out.append(GoogleCalendarConnector(store, sync, src.gcal.calendars, client_factory))
    return out


__all__ = ["SOURCES", "AuthError", "Connector", "Coverage", "FetchResult", "NotConfigured", "RateLimited",
           "SourceError", "SyncReport", "apply_result", "build_connectors", "is_stale", "make_client", "sync_source",
           "window"]
