# Atlas: a free, local, metrics-driven personal assistant

Atlas is a personal assistant for a university student. It reads Canvas, calendars, routine and (later) Gmail, GitHub, Outlook and LinkedIn emails. It answers questions about them, warns about deadlines and clashes, and every action it takes is approval-gated. It's built on [Strands Agents](https://strandsagents.com) (Apache-2.0) with a **local Ollama model**, so it costs nothing and private data never leaves your laptop.

**Guiding principle: reliable before autonomous.** See [docs/00-process.md](docs/00-process.md).

| | |
|---|---|
| Process & phases | [docs/00-process.md](docs/00-process.md) |
| Requirements | [docs/01-requirements.md](docs/01-requirements.md) |
| Architecture & runtime pipeline | [docs/02-architecture.md](docs/02-architecture.md) |
| Security design | [docs/03-security.md](docs/03-security.md) |
| Metrics (7 KPIs) | [docs/04-metrics.md](docs/04-metrics.md) |
| Decisions | [docs/adr/](docs/adr/) |

## Status: Phase 2 (reliable daily briefing): built, gate pending

The code for Phase 2 is done. The gate is 7 days in a row of briefings that match your real Canvas and calendar, with nothing missed, and it needs your accounts connected (see below).

Daily briefing and alerts ([ADR-0007](docs/adr/0007-daily-briefing-and-alerts.md)):
- **The 7 AM briefing** is built by code from your synced data, never written by the model. It covers:
  - today's classes and events;
  - work due within 48 hours and later in the week, skipping anything already submitted;
  - missing submissions;
  - clashes over the next 14 days;
  - new announcements;
  - a data-health section that names stale sources.
- **Never missed, never doubled.** If the laptop was asleep or Atlas wasn't running at 7:00, the briefing arrives when Atlas next runs, marked as caught up.
- **Windows notifications only for urgent items:** due within 24 h, a missing submission, a clash today, or an expired sign-in. They're capped at 5 a day. Text from other people's invites never appears in a notification.
- The briefing sits at the top of the Chat page. You mark each day "accurate" or "something's wrong" and rate each alert. The Metrics page tracks the 7-day gate streak and alert precision.

Sync ([ADR-0006](docs/adr/0006-sync-layer-design.md)):
- Connectors for your timetable (`routine.yaml`), Canvas (API token or calendar feed) and Google Calendar (read-only sign-in or secret iCal address).
- **Canvas is verified on real data** (2026-11-17): due times and submission statuses match the raw API exactly. Course codes like `CS5100.17342.202710` are stored as `CS 5100`. "Redo" requests count as open work. The Canvas client can only send GET requests.
- An unedited copy of the example timetable is ignored, so fictional classes never appear in a briefing.
- Sync that never retries in a loop:
  - an expired sign-in parks the source until you sign in again;
  - other failures back off;
  - a failed sync never deletes your data.
- Invites written by other people are marked untrusted and taint the run.
- `atlas auth` (credentials go to Windows Credential Manager) and `atlas sync [--dry-run]`.
- The benchmark now pins its reference date and checks answers on all seven weekdays.
- 169 tests. They never read your own `config/settings.toml` or `config/routine.yaml`.

Phase 1 (done):
- A single Strands agent with 5 read tools over a SQLite store.
- Security hooks: allowlist, taint tracking, approval gate, output sanitizer.
- Split-latency metrics.
- Local web chat and a metrics page.
- The `atlas bench` model benchmark: golden set plus 8 end-to-end scenarios, including adversarial ones.
- Default model **qwen3.5:4b**, chosen by benchmark: 8/8 scenarios, 100% intent, 98% slots, 2.6 s p50, fully on a 6 GB GPU ([ADR-0005](docs/adr/0005-default-model.md)).
- Adversarial tests (see docs/03-security.md).

`atlas seed-demo` loads fictional data for trying things out. Remove it with `atlas clear-demo` before connecting your real sources.

## Setup (Windows)

1. **Install Ollama** (free) from https://ollama.com/download, then pull the default model (3.3 GB):
   ```bash
   ollama pull qwen3.5:4b
   ```
   To re-run the model benchmark you also need the other candidates, `qwen3:8b` and `qwen3:4b`. See [ADR-0005](docs/adr/0005-default-model.md) for why `qwen3.5:4b` is the default.
2. **Create the environment** (from this folder):
   ```bash
   python -m venv .venv
   ```
   ```bash
   .venv/Scripts/python -m pip install -e ".[dev,evals]"
   ```
3. **Optional config:** copy `config/settings.example.toml` to `config/settings.toml` (gitignored).
4. **Check everything:**
   ```bash
   .venv/Scripts/atlas doctor
   ```

## Use

```bash
.venv/Scripts/atlas seed-demo
```
```bash
.venv/Scripts/atlas ask "what's due this week?"
```
```bash
.venv/Scripts/atlas serve
```
Then open http://127.0.0.1:8765. **Chat** shows split timings and 👍/👎 on every answer; **Metrics** shows the KPI baseline.

## Connect your data sources

Credentials never go in files. You type them into `atlas auth`, which checks each one and stores it in Windows Credential Manager. `atlas auth status` shows only whether each credential is present or absent.

1. **Settings:** copy `config/settings.example.toml` to `config/settings.toml`. Then edit the `[sources.*]` sections: `enabled = true`, the `mode`, and your Canvas `base_url`.
2. **Timetable:** copy `config/routine.example.yaml` to `config/routine.yaml` and enter your real lectures, labs and office hours. Both files are gitignored.
3. **Canvas, API mode** (recommended: it includes submission status):
   - In Canvas, go to Account → Settings → **+ New Access Token** and set an expiry date.
   - Then run:
   ```bash
   .venv/Scripts/atlas auth canvas
   ```
   - If your school doesn't offer access tokens, use the calendar feed instead. In Canvas, go to Calendar → **Calendar Feed**, copy the link, set `mode = "ics"`, and run:
   ```bash
   .venv/Scripts/atlas auth canvas --ics
   ```
4. **Google Calendar, API mode:**
   - Create a free Google Cloud project at https://console.cloud.google.com/.
   - Enable the **Google Calendar API**.
   - On the OAuth consent screen, choose External, add yourself as a test user, then **Publish app → In production**. A "Testing" app's sign-in expires every 7 days.
   - Create an OAuth client of type **Desktop app** and download its JSON.
   - Run:
   ```bash
   .venv/Scripts/atlas auth gcal --client-file path/to/client_secret.json
   ```
   - Your browser opens and you sign in. Google will warn that the app is unverified, because it's your own personal project. Choose "Continue".
   - Simpler alternative: Google Calendar → Settings → your calendar → **Secret address in iCal format**. Set `mode = "ics"`, then run:
   ```bash
   .venv/Scripts/atlas auth gcal --ics
   ```
5. **Check and sync:**
   ```bash
   .venv/Scripts/atlas sync --dry-run
   ```
   ```bash
   .venv/Scripts/atlas sync
   ```
   ```bash
   .venv/Scripts/atlas auth status
   ```

## Daily briefing

While `atlas serve` is running, Atlas syncs every 30 minutes and makes the briefing at 7:00. If 7:00 was missed, it makes it as soon as Atlas runs. To see one now:
```bash
.venv/Scripts/atlas brief
```

**Optional: a briefing even when `atlas serve` isn't running.** Create a Windows scheduled task yourself in PowerShell. Replace the path with your project folder. "Start when available" makes Windows run it at wake if 7:00 was missed.
```powershell
$dir = "C:\path\to\Personal-Assistant"
$action = New-ScheduledTaskAction -Execute "$dir\.venv\Scripts\atlas.exe" -Argument "brief --if-due --notify" -WorkingDirectory $dir
Register-ScheduledTask -TaskName "Atlas daily briefing" -Action $action -Trigger (New-ScheduledTaskTrigger -Daily -At 7:00am) -Settings (New-ScheduledTaskSettingsSet -StartWhenAvailable)
```
`--if-due` makes at most one daily briefing, so this never doubles up with `atlas serve`. Remove the task with `Unregister-ScheduledTask -TaskName "Atlas daily briefing"`.

**The Phase 2 gate:** each morning, compare the briefing with Canvas and your calendar. Then click **Yes, accurate** or **Something's wrong** on the Chat page, or record it from the terminal:
```bash
.venv/Scripts/atlas brief --verdict accurate
```
The Metrics page shows the streak. Phase 2 passes at 7 accurate days in a row with nothing missed.

**Try it on demo data first**, without touching your real data. In PowerShell, point Atlas at a separate, gitignored data folder:
```powershell
$env:ATLAS_DATA_DIR = "data/preview-demo"; .venv/Scripts/atlas seed-demo; .venv/Scripts/atlas brief --if-due --no-sync; .venv/Scripts/atlas serve --no-scheduler
```

## Phase 1 gate: benchmark the models

```bash
.venv/Scripts/atlas bench
```

For each model, the benchmark measures:
- VRAM use and how much of the model stays on the GPU at 4k, 8k and 16k context, plus peak GPU memory during the run
- tokens per second
- golden-set intent and slot accuracy
- 8 end-to-end scenarios: task success, incorrect tool calls, hallucinations, refusals, multi-turn memory, injection resistance
- split latency
- a ~3k-token email with an embedded prompt injection
- a 3-step request
- a weekday sweep: day questions asked as if it were each day of the week, checking that each answer names everything on that day

Runs use a fixed reference date by default (`--now 2026-11-13T09:00:00`), so results from different days are comparable. The model's answers can depend on the weekday in its prompt, which is what the sweep is for. Pass `--now live` to use the real clock.

The suggested default is the most **correct** model that runs well on the GPU. Speed only breaks near-ties. Reports go to `evals/reports/`. Record the chosen default in a new ADR using `docs/adr/0000-template.md`. Use `--quick` for a faster, smaller run.

## Tests

```bash
.venv/Scripts/python -m pytest
```

The tests use a scripted fake model, so Ollama isn't needed. They also pin the Strands SDK behaviour Atlas relies on: hooks, `cancel_tool`, interrupts and resume.

## Layout

```
config/    settings, policy.yaml (tool tiers + allowlists), time baselines, routine example
docs/      process, requirements, architecture, security, metrics, ADRs
evals/     golden.jsonl (intent/slot set), reports/ (bench output, gitignored)
src/atlas/ agent/ hooks/ security/ store/ metrics/ evaluation/ web/ cli.py
tests/     unit + pipeline tests (scripted model)
```
