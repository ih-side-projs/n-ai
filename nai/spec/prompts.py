"""Prompt loader for SS-SPEC (X10).

Each pipeline stage has a versioned prompt file in prompts/.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


@dataclass(frozen=True)
class SpecPrompt:
    name: str
    version: int
    text: str

    @property
    def id(self) -> str:
        return f"{self.name}@{self.version}"


def _parse_prompt_file(name: str, raw: str) -> SpecPrompt:
    version = 1
    lines = raw.splitlines()
    body_start = 0
    if lines and lines[0].startswith("# version:"):
        try:
            version = int(lines[0].split(":", 1)[1].strip())
            body_start = 1
        except ValueError:
            version = 1
    text = "\n".join(lines[body_start:]).lstrip("\n")
    return SpecPrompt(name=name, version=version, text=text)


def get_prompt(name: str) -> SpecPrompt:
    """Resolve a spec prompt via WP1 promptstore if registered, else local file (X10)."""
    try:
        from nai import promptstore  # type: ignore

        p = promptstore.get(f"spec_{name}")
        if p is not None:
            return SpecPrompt(name=p.name, version=int(p.version), text=p.text)
    except Exception:
        pass

    path = _PROMPTS_DIR / f"{name}.txt"
    if not path.is_file():
        raise FileNotFoundError(f"Spec prompt not found: {path}")
    return _parse_prompt_file(name, path.read_text(encoding="utf-8"))


# Prompt name constants for each stage
DECOMPOSE = "decompose"
SELECT_MAP = "select_map"
INTERFACES = "interfaces"
CHECK_INTERFACES = "check_interfaces"
COMPONENT_SPEC = "component_spec"
RECONCILE = "reconcile"
LINT = "lint"  # code-only, no model
CLARIFY = "clarify"

ALL_PROMPTS = [
    DECOMPOSE,
    SELECT_MAP,
    INTERFACES,
    CHECK_INTERFACES,
    COMPONENT_SPEC,
    RECONCILE,
    LINT,
    CLARIFY,
]