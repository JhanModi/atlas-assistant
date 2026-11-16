# ADR-0007: Daily briefing, proactive alerts and scheduling

- **Status:** accepted
- **Date:** 2026-11-16

## Context
The Phase 2 milestone, in the review's words, is that Atlas "automatically produces an accurate summary of your actual classes, assignments, and meetings, without you having to ask for it". The gate is 7 days of briefs that match a manual check, with 0 missed deadlines.

Constraints and risks:
- The laptop sleeps, and Atlas may not be running at 7 AM.
- A small local model can misstate dates (ADR-0005).
- Calendar invites can carry text written by strangers.
- Too many notifications teach the user to ignore them, which hurts the "useful proactive alerts" KPI.

## Options considered
1. **Who writes the briefing:**
   - *the LLM writes it* from tool calls: fluent, but adds model latency and a chance of invented or misdated items;
   - *code composes it* from the store: exact, explainable, testable.
2. **When it runs:**
   - *a cron-style 7:00 job*: missed if the process wasn't running or the laptop was asleep;
   - *an idempotent "ensure today's briefing" check* run often: never missed, never doubled.
3. **Which alerts get pushed:**
   - *everything*;
   - *only urgent items*, with the rest collected in the briefing.

## Decision
- **The briefing is composed by code, not the model.**
  - It's built from the same detectors that raise alerts: today's schedule; work due within 48 h and later this week (excluding submitted, graded, excused or done work); missing submissions; clashes in the next 14 days; new announcements.
  - A data-health section lists stale sources and their last successful sync.
  - Caveats appear for feed mode and for demo data.
  - A model-written summary may be layered on later, but never replaces these facts.
- **Scheduling:** `ensure_daily()` makes today's briefing only if it's past the briefing time (default 07:00) and none exists yet. Uniqueness is enforced by the database (`Briefing.daily_key`), and the check is safe to call from anywhere:
  - an APScheduler job inside `atlas serve`, every 5 minutes, with coalescing and no misfire limit, plus a first tick 30 s after start-up;
  - `atlas brief --if-due`, for Windows Task Scheduler with "run as soon as possible after a missed start".
- **Fresh data first:** sources are synced before the briefing is built, skipping any that synced in the last 10 minutes. A briefing made within 15 minutes of the scheduled time is `scheduled`; a later one is `catch_up`, so on-time delivery is measurable.
- **Alert policy:**
  - Every finding has a stable dedupe key that the database keeps unique, so the same alert is never raised twice.
  - Only *urgent* findings become Windows toasts: due within 24 h, a missing submission, a clash within 24 h, an expired sign-in.
  - Toasts are capped at 5 a day; anything beyond the cap is folded into one summary toast.
  - Everything else waits for the briefing.
  - "Missing" is claimed only when Canvas's own submission data says so. Feed mode never claims it.
- **Toast safety:** toast text uses counts and *trusted* titles only. An invite written by someone else appears as "an event from someone else's invite", so a phishing-style title can't arrive as a notification that seems to come from Atlas.
- **Gate measurement:**
  - Each daily briefing can be marked "accurate" or "inaccurate", with a count of missed items and a note, from the dashboard or with `atlas brief --verdict`.
  - The Metrics page shows the consecutive-day streak toward 7, plus on-time vs catch-up delivery.
  - Briefings count as L3 autonomy. Each daily briefing not marked inaccurate earns the 15-minute `morning_check` time-saved baseline.
  - Alerts rated in the briefing feed alert precision.

## Evidence
- **20 tests** (`tests/test_briefing.py`) cover: the detectors on demo data; finished work never reported as due; "missing" only from Canvas status; one toast per alert; invite titles kept out of toasts; the daily toast cap; the toast pointing at the next item; timing (none before 07:00, `scheduled` at 07:02, `catch_up` at 09:40 after a sync); no duplicate daily briefing; job configuration (coalesce, no misfire limit, one instance); job errors recorded instead of raised; the streak rules; the KPIs; and the dashboard flow.
- **Mutation check:** 7 behaviours were broken on purpose. The first pass missed one ("submitted work reported as due"), because the demo quiz falls outside the 48 h window. After I added a dedicated test, all 7 were caught.
- **The real APScheduler** was run for 45 s on a throwaway database at 16:47 local time:
  - the sync job ran at +5 s and recorded 2 alerts, neither urgent, so no toasts;
  - the briefing job ran at +30 s and produced exactly one `catch_up` briefing and its summary toast.
- **Browser check** (demo data in `data/preview-demo`): the briefing card, rating an alert, recording a verdict, and the Metrics page (streak, delivery counts, alert precision) all worked, with no console errors.
- **One defect found by the real run:** the toast led with a class that had already ended ("First: … 2:50 PM", sent at 4:47 PM). It now points at the next upcoming trusted item, and a regression test covers it.

## Consequences
- The briefing is exactly as accurate as the sync. Wrong or missing source data shows up as a gate failure, which is the point of the gate.
- If `atlas serve` isn't running and no scheduled task is set up, the briefing appears on the next start (catch-up). It can't arrive while nothing is running.
- The Phase 2 gate needs the user's real accounts connected and 7 days of checks. Code alone can't pass it.
