"""Google Calendar connector (OAuth, read-only scope) and the desktop sign-in flow.

Verified 2026-11-14 against Google docs:
- Desktop apps: loopback redirect http://127.0.0.1:<port>, PKCE S256 and `state`. Code exchange and refresh
  go to https://oauth2.googleapis.com/token. A revoked or expired refresh token returns `invalid_grant`.
- events.list: timeMin bounds an event's END, timeMax its START (both exclusive). singleEvents=true expands
  recurring events. syncToken can't be combined with timeMin/timeMax, so we refresh a fixed window (ADR-0006).
- Narrowest read scope: calendar.events.readonly.
OAuth client IDs, secrets and refresh tokens live in the OS keyring. The user signs in in their own browser.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import httpx

from atlas import timeutil
from atlas.config import SyncSettings
from atlas.connectors.base import (AuthError, Coverage, FetchResult, NotConfigured, RateLimited, SourceError,
                                   make_client, window)
from atlas.security.sanitize import clean_title, to_plain_text
from atlas.store.models import Item
from atlas.vault import SecretStore

SOURCE = "gcal"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/calendar/v3"
SCOPE = "https://www.googleapis.com/auth/calendar.events.readonly"
MAX_PAGES = 20
REAUTH = "Run `atlas auth gcal` to sign in again"


# ---------------------------------------------------------------------------- sign-in (run by the user)

def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)  # 86 chars, within RFC 7636's 43-128
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()
    return verifier, challenge


def build_auth_url(client_id: str, redirect_uri: str, state: str, challenge: str) -> str:
    return AUTH_URL + "?" + urlencode({
        "client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code", "scope": SCOPE,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
        "access_type": "offline", "prompt": "consent",
    })


def run_consent_flow(client_id: str, client_secret: str, open_browser: Callable[[str], Any],
                     timeout: float = 300, client_factory: Callable[..., httpx.Client] = make_client) -> str:
    """Open Google's consent page, catch the redirect on 127.0.0.1, exchange the code. Returns a refresh token."""
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(24)
    outcome: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (http.server API)
            query = parse_qs(urlsplit(self.path).query)
            if "state" not in query:  # e.g. /favicon.ico: not the redirect
                self.send_response(404)
                self.end_headers()
                return
            if query["state"][0] != state:
                outcome["error"] = "state mismatch (possible cross-site request); try again"
            elif "error" in query:
                outcome["error"] = query["error"][0]
            elif "code" in query:
                outcome["code"] = query["code"][0]
            ok = "code" in outcome
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(("Atlas: Google Calendar connected. You can close this tab." if ok else
                              "Atlas: sign-in failed. Return to the terminal for details.").encode())

        def log_message(self, *args: Any) -> None:  # keep the authorization code out of the console
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)  # loopback only; random free port
    server.timeout = 1
    redirect_uri = f"http://127.0.0.1:{server.server_port}"
    try:
        open_browser(build_auth_url(client_id, redirect_uri, state, challenge))
        deadline = time.monotonic() + timeout
        while not outcome and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if "code" not in outcome:
        raise AuthError(f"Google sign-in did not complete: {outcome.get('error', 'timed out')}")

    with client_factory() as client:
        r = client.post(TOKEN_URL, data={
            "code": outcome["code"], "client_id": client_id, "client_secret": client_secret,
            "code_verifier": verifier, "redirect_uri": redirect_uri, "grant_type": "authorization_code"})
    body = _json(r)
    if r.status_code != 200:
        raise AuthError(f"Google token exchange failed: {body.get('error', r.status_code)}")
    if SCOPE not in str(body.get("scope", "")).split():
        raise AuthError("calendar read permission was not granted on the consent screen; try again and tick it")
    refresh = body.get("refresh_token")
    if not refresh:
        raise AuthError("Google returned no refresh token. Remove Atlas at https://myaccount.google.com/permissions "
                        "and run `atlas auth gcal` again")
    return refresh


def _json(r: httpx.Response) -> dict:
    try:
        data = r.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


RATE_REASONS = {"rateLimitExceeded", "userRateLimitExceeded"}


def _reason(r: httpx.Response) -> str:
    err = _json(r).get("error")
    errors = err.get("errors") if isinstance(err, dict) else None
    first = errors[0] if isinstance(errors, list) and errors and isinstance(errors[0], dict) else {}
    return str(first.get("reason", ""))


def refresh_access_token(client: httpx.Client, client_id: str, client_secret: str, refresh_token: str) -> str:
    """One token request. invalid_grant (expired/revoked sign-in) -> AuthError, which parks the source."""
    r = client.post(TOKEN_URL, data={"client_id": client_id, "client_secret": client_secret,
                                     "refresh_token": refresh_token, "grant_type": "refresh_token"})
    if r.status_code == 200 and _json(r).get("access_token"):
        return _json(r)["access_token"]
    error = _json(r).get("error", "")
    if r.status_code == 429:
        raise RateLimited("Google token endpoint is rate limiting (HTTP 429)")
    if r.status_code in (400, 401) or error:
        raise AuthError(f"Google sign-in expired or was revoked ({error or r.status_code}). {REAUTH}")
    raise SourceError(f"Google token endpoint returned HTTP {r.status_code}")


# ---------------------------------------------------------------------------- sync

def _when(value: dict, zone) -> tuple[datetime | None, bool]:
    if value.get("dateTime"):
        dt = datetime.fromisoformat(str(value["dateTime"]).replace("Z", "+00:00"))
        return (dt if dt.tzinfo else dt.replace(tzinfo=zone)), False
    if value.get("date"):
        return timeutil.start_of_day(datetime.fromisoformat(str(value["date"])).date(), zone), True
    return None, False


def event_item(ev: dict, calendar: str, zone) -> Item | None:
    if ev.get("status") == "cancelled" or ev.get("eventType") == "workingLocation" or not ev.get("id"):
        return None
    start, all_day = _when(ev.get("start") or {}, zone)
    end, _ = _when(ev.get("end") or {}, zone)
    if start is None:
        return None
    me = next((a for a in ev.get("attendees") or [] if a.get("self")), None)
    if me and me.get("responseStatus") == "declined":
        status = "declined"
    elif ev.get("transparency") == "transparent":
        status = "optional"  # shown as "free": never a hard conflict
    else:
        status = None
    # Invites from other people carry text they wrote: mark untrusted (taints the run, see docs/03-security.md)
    mine = (ev.get("organizer") or {}).get("self") or ((ev.get("creator") or {}).get("self") and not ev.get("organizer"))
    source_id = f"{calendar}:{ev['id']}"
    return Item(id=f"{SOURCE}:{source_id}", source=SOURCE, source_id=source_id, kind="event",
                title=clean_title(ev.get("summary")), start=start, end=end, all_day=all_day,
                location=clean_title(ev["location"]) if ev.get("location") else None,
                url=ev.get("htmlLink") if str(ev.get("htmlLink", "")).startswith("https://") else None,
                body=to_plain_text(ev.get("description")), status=status,
                trust="trusted" if mine else "untrusted",
                updated_at=datetime.fromisoformat(ev["updated"].replace("Z", "+00:00")) if ev.get("updated") else None)


class GoogleCalendarConnector:
    source = SOURCE

    def __init__(self, store: SecretStore, sync: SyncSettings, calendars: list[str],
                 client_factory: Callable[..., httpx.Client] = make_client) -> None:
        self.store = store
        self.sync = sync
        self.calendars = calendars or ["primary"]
        self.client_factory = client_factory

    def fetch(self, now: datetime) -> FetchResult:
        cid, csecret, refresh = (self.store.get(k) for k in ("gcal.client_id", "gcal.client_secret",
                                                              "gcal.refresh_token"))
        if not (cid and csecret and refresh):
            raise NotConfigured("Google Calendar is not signed in: run `atlas auth gcal`")
        start, end = window(now, self.sync)
        zone = timeutil.tz()
        items: list[Item] = []
        with self.client_factory() as client:
            token = refresh_access_token(client, cid, csecret, refresh)
            refreshed_on_401 = False
            for cal in self.calendars:
                page_token: str | None = None
                for _ in range(MAX_PAGES):
                    params = {"timeMin": start.isoformat(), "timeMax": end.isoformat(), "singleEvents": "true",
                              "orderBy": "startTime", "maxResults": 250}
                    if page_token:
                        params["pageToken"] = page_token
                    r = client.get(f"{API}/calendars/{quote(cal, safe='')}/events", params=params,
                                   headers={"Authorization": f"Bearer {token}"})
                    if r.status_code == 401 and not refreshed_on_401:  # access token expired mid-sync: once
                        token = refresh_access_token(client, cid, csecret, refresh)
                        refreshed_on_401 = True
                        continue
                    if r.status_code == 401:
                        raise AuthError(f"Google rejected the access token twice. {REAUTH}")
                    if r.status_code == 429 or (r.status_code == 403 and _reason(r) in RATE_REASONS):
                        raise RateLimited("Google Calendar is rate limiting; will try again later")
                    if r.status_code == 404:
                        raise SourceError(f"calendar {cal!r} not found (check [sources.gcal] calendars)")
                    if r.status_code >= 400:
                        raise SourceError(f"Google Calendar returned HTTP {r.status_code}")
                    data = _json(r)
                    items += [i for ev in data.get("items") or [] if (i := event_item(ev, cal, zone))]
                    page_token = data.get("nextPageToken")
                    if not page_token:
                        break
                else:
                    raise SourceError(f"calendar {cal!r} returned more than {MAX_PAGES} pages")
        return FetchResult(items, [Coverage("start", start, end)])
