# ADR-0006: Phase 2 sync layer design

- **Status:** accepted
- **Date:** 2026-11-14

## Context
Phase 2's goal is a reliable daily briefing. Its gate requires 7 days of briefs that match a manual check, with 0 missed deadlines. That makes sync correctness and failure behaviour more important than sync speed. The sources are Canvas, Google Calendar and the routine file.

**Constraints:**
- Free only.
- Single user, on a laptop that sleeps.
- Canvas student tokens may be disabled at some schools.
- Google OAuth apps left in "Testing" mode lose their refresh token after 7 days.
- Every credential is created and entered by the user.

## Options considered
1. **Google Calendar `syncToken` incremental sync** (the original plan). Efficient, but it can't be combined with `timeMin`/`timeMax`. A full first sync pulls all history, and 410 GONE needs a resync path. It also needs extra state and code paths.
2. **Windowed refresh.** Every sync fetches `[today − 1 day, today + 30 days)` with `singleEvents=true` and replaces what that window covers. At one sync every 30 minutes that's about 50 requests a day, far below the free quota. Deletions and cancellations are handled by construction.
3. **`google-auth-oauthlib`** for sign-in, versus **our own loopback + PKCE flow** (~80 lines over stdlib `http.server` plus `httpx`).

## Decision
- **Windowed refresh for every source.** Each connector returns its items plus the *coverage* it is authoritative for: a field, a window and a set of kinds. Covered items that disappear are removed. Items outside the coverage are never touched. A failed fetch changes nothing.
- **Failure policy:**
  - There are no retry loops.
  - `AuthError` (401, `invalid_grant`, or a rejected feed URL) parks the source as `needs_reauth`. It stays parked until `atlas auth …` succeeds; scheduled ticks skip it without making requests.
  - Network errors and rate limiting back off exponentially, starting at 30 minutes and capped at 6 hours.
  - The one exception: an access token expiring partway through a Google sync is refreshed **once**.
- **Two modes per source:**
  - Canvas: `api` (personal token; includes submission status and announcements) or `ics` (calendar feed; no submission status).
  - Google Calendar: `api` (OAuth, `calendar.events.readonly`) or `ics` (the "secret address in iCal format").
- **Own OAuth flow** (loopback 127.0.0.1, PKCE S256, `state`). This avoids adding Google's libraries, keeps every request mockable with `respx`, and makes the `invalid_grant` handling explicit and tested.
- **Trust at ingest:**
  - Titles from invites organized by other people, ICS meetings with attendees, and Canvas announcements are `untrusted`.
  - Bodies are converted to bounded plain text.
  - Titles are normalized: no markup, control characters or bidi overrides.
  - The taint hook reacts to `"untrusted": true` in any tool result.
- **Canvas hygiene:**
  - https base URL only.
  - Sequential requests (Canvas recommends this).
  - The sync stops early when `X-Rate-Limit-Remaining` drops below 20.
  - Pagination links to another host are refused, so the token never leaves the configured host.
- **Schema evolution:** a forward-only `add_missing_columns()` runs at startup, because `create_all()` never alters existing tables.

## Evidence
- The API details were checked against the official docs on 2026-11-14:
  - Canvas planner, announcements and throttling (429, `X-Rate-Limit-Remaining`).
  - Google `events.list` (`timeMin` is compared to an event's end and `timeMax` to its start; `syncToken` excludes both).
  - Google's native-app OAuth (loopback, PKCE, and `invalid_grant` meaning expired or revoked).
- 36 connector and OAuth tests run on recorded-style fixtures. They cover pagination, submission states, recurrence expansion, cancellations, invites, expiry partway through a sync, a forged OAuth `state`, cross-host pagination and feed-URL redaction.
- A mutation check broke four safety behaviours one at a time; the matching test failed for each.

## Consequences
- Re-fetching a 31-day window wastes some bandwidth. That's acceptable for one user and keeps the sync logic simple.
- **ICS mode can't show submission status.** In ICS mode the briefing must say that "due" items may already be submitted.
- **Google OAuth needs the user to publish the app to "In production"** (single user, unverified). Otherwise the refresh token expires after 7 days, and the source parks as `needs_reauth`, which is visible rather than silent.
- The routine file is local and never stale. Network sources become "stale" after 3 missed intervals or any failure, and the briefing will show this.
- Next checkpoint: the scheduler (sync every 30 minutes, plus a 7 AM briefing with catch-up after sleep), deterministic detectors and the briefing itself.
