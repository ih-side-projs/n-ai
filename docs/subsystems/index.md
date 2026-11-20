# Subsystems

Each subsystem corresponds to a work package in the overall specification.

| Subsystem | Package | Detail |
|-----------|---------|--------|
| SS-CORE | `nai/*.py` | [core.md](core.md) |
| SS-STORE | `nai/store/` | [store.md](store.md) |
| SS-SCHED | `nai/sched/` | [scheduler.md](scheduler.md) |
| SS-SPEC | `nai/spec/` | [spec-pipeline.md](spec-pipeline.md) |
| SS-BUILD | `nai/build/` | [build-pipeline.md](build-pipeline.md) |
| SS-TOOLS | `nai/tools/` | [tools.md](tools.md) |
| SS-WEB | `nai/web/` | [web.md](web.md) |
| SS-EVAL | `nai/eval/` | [evaluation.md](evaluation.md) |

## Dependency direction

```
core → store → sched → spec → build
                    ↘ tools ↗
                       web (read-only store + command queue)
                       eval (stub server + harness)
```

`core` has no dependency on the other subsystems. `store` uses only the standard
library. `sched` depends on `core` and `store`. `spec` and `build` register job
handlers as scheduler plugins. `tools` is used by jobs through allowlists. `web`
and `eval` sit on top.

## Shared rules

All subsystems follow the cross-cutting rules X1–X10 described in
[Architecture](../architecture.md): structured outputs, deterministic Python,
string tool results with `ERROR:`, resumability, budgets, call logging, layered
configuration, offline tests, configurable size limits and versioned prompts.
