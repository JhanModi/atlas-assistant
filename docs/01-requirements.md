# 01 · Requirements

## Problem
A university student's obligations are spread across Canvas, two email accounts (personal Gmail, university Outlook), calendars, GitHub, LinkedIn and a weekly lecture routine. Checking all of them by hand every day is slow, and it is easy to miss a deadline or a clash.

## Vision
A **proactive, persistent, local** assistant that knows the user's schedule and obligations, answers natural-language questions about them, warns about problems ahead of time, and, later, carries out routine work behind a clear approval step.

## Hard constraints
- **C1 Free:** no paid APIs, subscriptions or hosting. Local models via Ollama; free official APIs only.
- **C2 Private:** private data (email, grades, coursework) is processed only by a local model. A cloud model, if ever enabled, sees only non-private text.
- **C3 Allowed access only:** official APIs or exports. No scraping (e.g. LinkedIn User Agreement §8.2).
- **C4 Runs on the user's laptop:** Windows 11, RTX 3060 6 GB, 16 GB RAM. Works offline for reads of already-synced data.
- **C5 Safe by default:** every write action needs approval until the user explicitly graduates it.

## User stories by phase

### Phase 1 – Local foundation
- **US-1.1** As the user, I can open a local web page, ask a question and get an answer from a local model.
- **US-1.2** As the developer, I can benchmark candidate local models (VRAM, speed, tool-call validity) and choose the default from data.
- **US-1.3** Every interaction is recorded with split latency (model / tool / end-to-end) and can be rated 👍/👎.

### Phase 2 – Daily briefing (Canvas, Google Calendar, routine)
- **US-2.1** Each morning (or at first wake after 7:00), I see today's lectures, events and deadlines in one summary.
- **US-2.2** I'm alerted when an assignment is due within 48 h and not yet submitted.
- **US-2.3** I'm alerted to overlapping commitments up to 14 days ahead.
- **US-2.4** I can mark each alert useful / not useful.

### Phase 3 – Conversational search
- **US-3.1** I can ask "what's due next week?", "am I free Thursday afternoon?", "when is my next CS 5800 lecture?" and get answers that cite the underlying items.
- **US-3.2** When Atlas can't answer, it says so plainly (counted as a fallback) instead of guessing.

### Phase 4 – More sources and approved actions
- **US-4.1** Gmail, GitHub, Outlook calendar (ICS or Graph) and LinkedIn notification emails appear in the briefing.
- **US-4.2** Atlas can draft an email reply, create a reminder, archive mail, or mark coursework done, and only after I approve each one.

### Phase 5 – Proactivity and memory
- **US-5.1** Atlas remembers my preferences (e.g. "skip optional office hours") without re-explaining.
- **US-5.2** Before a lecture or meeting, Atlas prepares a short prep note.
- **US-5.3** Weekly review every Sunday.

## Non-functional requirements
| ID | Requirement |
|---|---|
| NFR-1 | Read questions answered from the local store: p50 < 3 s (aspirational until Phase 4). |
| NFR-2 | Proactive alert shown within one sync interval of the source changing. |
| NFR-3 | Dashboard bound to 127.0.0.1 only. |
| NFR-4 | Secrets in Windows Credential Manager (`keyring`), never in the repo. |
| NFR-5 | All model output sanitized before rendering. No remote images loaded. |

## Out of scope (for now)
Voice, mobile app, multi-user, hosting in the cloud, sending anything without approval, LinkedIn automation of any kind.
