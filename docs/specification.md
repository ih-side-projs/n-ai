# Overall specification

The master design document lives in the repository root:
[`nai (N-AI) Overall Specification.md`](<../nai (N-AI) Overall Specification.md>).

It is the authoritative source for goal IDs (G1–G7), subsystem requirements
(CORE, STO, SCH, SPEC, BLD, TLS, WEB, EVAL), cross-cutting rules (X1–X10) and
open decisions (D1–D9). Requirement IDs are stable and referenced in code
comments and test names.

## Quick map

| Section | Topic |
|---------|-------|
| 0 | How to use the document |
| 1–2 | Purpose, goals, non-goals, environment and constraints |
| 3–5 | Data model, subsystem list, cross-cutting rules |
| 6 | SS-CORE |
| 7 | SS-STORE |
| 8 | SS-SCHED |
| 9 | SS-SPEC |
| 10 | SS-BUILD |
| 11 | SS-TOOLS |
| 12 | SS-WEB |
| 13 | SS-EVAL |
| 14 | Work packages |
| 15 | Open decisions (defaults in brackets) |
| 16 | Future, out of scope |

## Cross-cutting rules (summary)

- **X1** Structured outputs with schema validation and a bounded repair loop.
- **X2** Deterministic Python for control flow, checks and context assembly.
- **X3** Tool results are strings; failures start with `ERROR:`.
- **X4** Resumability: crash recovery requeues running jobs.
- **X5** Budgets: attempts, wall time, tokens; no infinite loops.
- **X6** Logging: full model calls stored; model swaps detected.
- **X7** Config: CLI > env > TOML > defaults; no secrets in the store.
- **X8** Testing: every subsystem has offline tests using a stub server.
- **X9** Size limits are config, not constants (see `[limits]` in TOML).
- **X10** Prompts are versioned text files.

## Open decisions and defaults

| Decision | Default in this implementation |
|----------|--------------------------------|
| D1 Web stack | FastAPI + server-rendered Jinja templates + local JS/SSE |
| D2 Candidate count | 3 at root, 1 elsewhere (distinct from X9 `max_components_per_level`) |
| D3 Approval granularity | Per specification level |
| D4 Parallel slots | Off, concurrency 1 |
| D5 Target languages | Python first |
| D6 Consult side conversations | Inside integration/diagnose jobs, transcripts stored |
| D7 Execution environment | Docker if available, local otherwise |
| D8 Sampling | Same reasoner model id, per-job sampling overrides |
| D9 Structured output | JSON schema → grammar → validate-and-repair |

## Proposed changes

The integrated implementation keeps the shared definitions (sections 4–5)
unchanged. Any future change to shared definitions should be proposed with a
numbered rationale, as the specification instructs.
