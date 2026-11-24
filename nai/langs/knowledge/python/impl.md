Implement only what the spec and contract require; keep the stub's signatures.

Python 3.12 idioms
- `match`/`case` for three-way-plus branching; `str | None` unions; builtin generics (`list[str]`).
- `@dataclass(frozen=True)` for value objects; `field(default_factory=list)` for mutable defaults.
- `pathlib.Path` for paths, `tomllib` for TOML, `json` for JSON.
- `datetime.now(timezone.utc)` for timestamps; never naive `utcnow()`.
- Explicit errors: `raise ValueError(f"value: {value!r}")`; never bare `except:`.

Structure
- Small pure helpers; pass state in and return new values.
- Module constants in `UPPER_SNAKE_CASE`; no mutable globals.
- `__all__` only when it clarifies the public surface.
- Docstrings: one line, imperative, ending with a period.

Verification
- `ruff check --select UP,B` must pass; UP flags deprecated stdlib usage.
- Type hints must be honest; the lint gate runs `mypy` or `pyright` when the project configures one.
- Import from the project package, never via `sys.path` edits.
