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

## Status: Phase 1 (local foundation)

Done:
- A single Strands agent with 5 read tools over a SQLite store.
- Security hooks: allowlist, taint tracking, approval gate, output sanitizer.
- Split-latency metrics.
- Local web chat and a metrics page.
- The `atlas bench` model benchmark: golden set plus 8 end-to-end scenarios, including adversarial ones.
- Default model **qwen3.5:4b**, chosen by benchmark: 8/8 scenarios, 100% intent, 98% slots, 2.6 s p50, fully on a 6 GB GPU ([ADR-0005](docs/adr/0005-default-model.md)).
- 104 tests, including adversarial cases (see docs/03-security.md).

Real data sources start in Phase 2. Until then, `atlas seed-demo` loads fictional data.

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
