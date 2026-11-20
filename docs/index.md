# nai documentation

`nai` is a local-first system that turns a rough project brief into a layered
specification and then into a tested codebase using locally served LLMs.

## Start here

- [Installation](installation.md) — Python, dependencies, local model server
- [Quick start](quickstart.md) — first run in five minutes
- [Architecture](architecture.md) — subsystems and control flow
- [Configuration](configuration.md) — TOML, environment variables, size limits

## Reference

- [CLI reference](cli-reference.md) — every console command and its flags
- [Service / daemon](service.md) — run web + runner as a managed service
- [API reference](api-reference.md) — public Python interfaces
- [Subsystems](subsystems/index.md) — one page per subsystem
- [Overall specification](specification.md) — requirement IDs and design rules

## Contributing and operations

- [Testing](testing.md) — running and extending the test suites
- [Troubleshooting](troubleshooting.md) — common problems and fixes
- [FAQ](faq.md) — frequently asked questions

## Design principles

| ID | Principle |
|----|-----------|
| G2 | No data leaves the machine at runtime |
| G4 | Deterministic Python for control flow; models only for narrow jobs |
| G5 | One tool registry, two consumers (orchestration + MCP) |
| X1 | Structured outputs with schema validation and repair |
| X4 | Resumable: crash recovery requeues running jobs |
| X8 | Every subsystem has tests using a stub model server |
| X9 | Size limits are configuration, not constants |

See the [full specification](specification.md) for all requirement IDs.
