# ADR-0004: Every write needs approval until the user graduates it

- **Status:** accepted
- **Date:** 2026-11-13

## Context
Even "low-risk" writes such as archiving an email or marking coursework done can be harmful if a prompt injection triggers them, and they erode trust if they go wrong. The goal is *reliable before autonomous*.

## Decision
- T1 (reversible) and T2 (outward-facing) writes both require approval through a Strands interrupt and the Approvals queue.
- A write tool graduates to automatic only when the user adds it to `policy.yaml → graduated`, after ≥20 approvals and 0 rejections.
- Graduation never applies in a **tainted** run, meaning one where untrusted text was read.
- T3 and unknown tools are always cancelled.

## Consequences
Autonomy grows on evidence, and the Metrics page shows the approval and rejection history per tool. Early on there will be more clicks, which is accepted.
