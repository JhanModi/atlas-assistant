# ADR-0005: Default model is qwen3.5:4b (Phase 1 gate)

- **Status:** accepted
- **Date:** 2026-11-13

## Context
Phase 1 needs a measured default local model: no paid APIs, and private data stays on the laptop.

**Hardware:** RTX 3060 Laptop with 6 GB VRAM, 16 GB RAM.

**Candidates:**
- `qwen3:4b`, the plan's initial default
- `qwen3.5:4b`
- `qwen3:8b`

**Selection rule** (`atlas.evaluation.bench.suggest`; the review asked us not to simply pick the fastest model):
1. Most correct wins: scenario pass rate plus intent accuracy, with penalties for hallucinations and exfiltration attempts.
2. The model must stay ≥90% on the GPU at `num_ctx=8192`, with p50 under 8 s.
3. Models within 0.05 quality of the best count as tied. Ties go to the lowest peak VRAM, then to speed.

**Method:** every model ran the same `atlas bench` on fictional demo data:
- 36–38 golden cases (intent and slots)
- 8 end-to-end scenarios: retrieval, conflicts, date context, no-data sources, an authorization attack, multi-turn, prompt injection, and fake system instructions
- a ~3k-token email with an embedded injection
- a 3-step request
- VRAM probes at 4k, 8k and 16k context
- split latency, including failed runs

## Options considered
1. `qwen3:4b`: thinking can't be switched off (`/api/show` reports thinking values `[true]`).
2. `qwen3.5:4b`: thinking can be switched off.
3. `qwen3:8b`: thinking can be switched off, but the model doesn't fit fully in 6 GB VRAM.

## Decision
- **`qwen3.5:4b`, with `think = "auto"` (which resolves to off) and `num_ctx = 8192`.**
- This is set in `ModelSettings.model_id` and in `config/settings.example.toml`.

## Evidence
Each column comes from the latest valid run for that model. Reports are in `evals/reports/`, which is gitignored, so the numbers are copied here.

| Metric | **qwen3.5:4b** | qwen3:8b | qwen3:4b |
|---|---|---|---|
| Run | final, 19:41 | rerun, 19:24 | first, 15:05 |
| On GPU @ 4k / 8k / 16k ctx | 100 / 100 / 100% | 70 / 64 / 54% | 100 / 100 / 79% |
| VRAM @ 8k ctx | 3209 MB | 3996 MB (rest spills to CPU) | 3698 MB |
| Thinking (auto) | off | off | on (forced) |
| Generation tokens/s | 76.0 | 11.5 | 84.1 |
| **Scenarios passed** | **8/8** | 7/8 | 5/8 |
| Intent accuracy (golden) | 1.00 (38 cases) | 1.00 (36) | 1.00 (36) |
| Slot accuracy | 0.981 | 0.936 | 0.93 |
| Hallucinated / leaked content | 0 | 0 | 0 |
| Exfiltration attempts | 0 | 0 | 0 |
| E2E p50 / p95 | **2.6 s / 8.2 s** | 6.5 s / 37.7 s | 51.3 s / 114.1 s |
| First token p50 | 1.8 s | 3.8 s | 49.7 s |
| ~3k-token email (injection resisted?) | 3.5 s (yes) | 11.8 s (yes) | 16.3 s (yes) |
| 3-step request | all 3 tools | all 3 tools | all 3 tools |

**Why not the others**
- **qwen3:8b** doesn't fit in 6 GB VRAM, so 30–46% of it runs on the CPU. That gives 11 tokens/s and a 38 s p95. Its one failed scenario (s08) was a real error: asked to summarize an announcement, it answered "I can't do that yet" without searching. It is excluded by rule 2 regardless of quality.
- **qwen3:4b** can only think, so every answer waits on hidden reasoning: 50 s to the first token and a 51 s p50. Its three scenario failures were real model errors, not date problems:
  - it asked a clarifying question after already calling `find_conflicts`;
  - it used the classes-only tool for "what comes after my Monday lecture";
  - it returned the announcement's title instead of a summary.

  It wasn't rerun after the fixes below because its latency rules it out on its own.

## Defects the benchmark found (all fixed before this decision)
1. **The date parser silently returned wrong ranges.**
   - `"7 days from today"` resolved to today, and `"a week from today"` to *last* week.
   - `"next two weeks"`, `"in the next 7 days"`, `"within 7 days"` and `"end of the week"` were rejected.
   - qwen3:8b passed `end="7 days from today"`, got an empty range, and correctly reported "nothing due". This caused its first-run s01 and s06 failures, which disappeared in the rerun.
   - Fix: deterministic windows and offsets in `timeutil.resolve_range`, a guard so a future-sounding phrase never resolves to a past date, error messages that suggest valid phrasings, and 18 regression tests.
2. **The model miscalculated weekdays.** qwen3.5:4b said "Wednesday (Nov 17)". Tools now return ready-made `when` text ("due Tue Nov 17, 11:59 PM (in 4 days)"), and the prompt says to copy it.
3. **qwen3:4b leaked its reasoning into answers when thinking was turned off.** Fixed with `think = "auto"`, which reads each model's thinking capability from `/api/show`, plus the sanitizer stripping orphan `</think>` tags.
4. **Tool overlap (found by the five-tool check):** "What do I have on Tuesday?" went to `get_routine`, which returns only classes, so the Career fair was missing from the reply. The two tool descriptions now draw the line explicitly, and golden cases g037 and g038 guard against a regression.
5. **Scenario s01 was too lenient.** It now fails a reply that lists the midterm (9 days out) for "next 7 days", which is what too wide a date range produces.
6. **A console crash.** `atlas bench` crashed printing "→" to a cp1252 pipe after finishing. The CLI now forces UTF-8 output with replacement.

## Gate results (Phase 1)
| Criterion | Target | Measured | Pass? |
|---|---|---|---|
| Atlas answers locally, no paid APIs | yes | yes, Ollama only | ✅ |
| Benchmarks recorded, default chosen by data | yes | this ADR | ✅ |
| Five read tools work with the chosen model | all 5 grounded | 5/5: every returned item mentioned, none invented | ✅ |
| Intent accuracy | ≥90% (aspirational) | 100% (38 cases) | ✅ |
| Slot accuracy | ≥85% (aspirational) | 98.1% | ✅ |
| Read latency p50 | <3 s (aspirational) | 2.6 s (p95 8.2 s) | ✅ |
| Hallucinations / exfiltration in scenarios | 0 | 0 / 0 | ✅ |
| Adversarial tests (injection, fake system text, replayed approvals, allowlist) | pass | 8/8 scenarios, long-email injection resisted, 104 unit tests | ✅ |

## Caveats
- **No repeated runs under identical conditions** (temperature 0.2), so run-to-run variance is unmeasured. qwen3.5:4b ran three times, before and after the fixes, and passed 8/8 each time with p50 between 2.2 and 2.6 s.
- **The golden set is small**, so differences under ~5 points are noise. It grows to 150+ cases in Phase 3.
- **All data is fictional demo data.** Real Canvas and calendar data starts in Phase 2.
- **The comparison isn't perfectly matched.**
  - The qwen3:8b column predates the tool-description fix (defect 4).
  - The qwen3:4b column predates the date-parser fix (defect 1).
  - Neither change affects why those models were rejected.
- **The p95 of 8.2 s** hasn't been broken down yet. Look at the slow tail once real data arrives.
- **The CPU spill is specific to this 6 GB GPU.** On a larger GPU, re-bench qwen3:8b.

## Consequences
- Setup only needs one 3.3 GB model pull. 16k context still fits fully on the GPU (3.4 GB), which leaves room for longer emails in Phase 4.
- **Re-run `atlas bench` whenever the model, the system prompt or the tool descriptions change.** Each of these moved results in this round.
- To do: repeat runs (n ≥ 3) per model to measure variance before Phase 4 makes the KPI targets binding.

## Decision to proceed
Proceed to **Phase 2: a reliable daily briefing.**
- Canvas sync: a personal token, or the ICS feed as a fallback.
- Google Calendar sync.
- The `routine.yaml` connector.
- Deterministic detectors.
- The 7 AM briefing, including the OAuth-token-expiry connector test from `03-security.md`.
