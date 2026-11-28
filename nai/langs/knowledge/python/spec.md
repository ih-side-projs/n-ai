Target: CPython 3.12, standard library first; third-party packages only when the project pins them in `target_env.packages`.

Signatures
- Type every parameter and return value: `def normalize(text: str) -> str:`.
- PEP 604 unions (`str | None`) and builtin generics (`list[str]`, `dict[str, int]`) are available (3.10+).
- Prefer `dataclasses.dataclass` for entity records; do not restate field lists in prose.

Requirements
- Each requirement names one observable behaviour and one exact example.
- State errors explicitly: which exception type and which message prefix.
- Name the owning module: `src/<package>/<module>.py`.

Constraints to write into the spec
- No network, no clock and no filesystem unless the requirement names them.
- Deterministic: same input, same output; sort output whose order is undefined.
- `ValueError` for bad values, `TypeError` for wrong types.
- Keep functions pure where possible; pass dependencies as arguments.

Platform
- Default `platform: any`. Use `linux` or `macos` only for a leaf component that must touch OS-specific paths or APIs.
- Portable components must not branch on `os.name` at import time.

Don't
- Don't promise performance numbers you cannot test.
- Don't require libraries absent from `target_env.packages`.
