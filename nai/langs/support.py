"""Derived language support: adapter + knowledge pack + toolchain probe + e2e fixture.

"Supported" is never a hand-maintained list.  A language is supported when the
registry has an adapter for it, the adapter ships a complete knowledge pack and
declares a toolchain probe.  The fourth criterion -- an end-to-end fixture
project that passes in CI -- is a repository artifact, so it is reported as
``None`` when the fixtures directory is not installed (wheel installs) and as
``True``/``False`` inside a checkout.  ``tests/langs/test_support.py`` enforces
all four for every registered adapter.

Every entry point (Store, CLI, web form, contract schema, spec prompts) reads
its list from :func:`supported_languages`; nothing else may hard-code one.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from . import knowledge, registry

#: Aliases used to notice a brief asking for a language N-AI cannot build.
#: Ambiguous single letters (``c``, ``r``) are deliberately absent.
LANGUAGE_ALIASES: dict[str, str] = {
    "python": "Python",
    "py": "Python",
    "javascript": "JavaScript",
    "js": "JavaScript",
    "node": "JavaScript",
    "nodejs": "JavaScript",
    "node.js": "JavaScript",
    "typescript": "TypeScript",
    "ts": "TypeScript",
    "rust": "Rust",
    "golang": "Go",
    "java": "Java",
    "kotlin": "Kotlin",
    "scala": "Scala",
    "clojure": "Clojure",
    "csharp": "C#",
    "c#": "C#",
    "cpp": "C++",
    "c++": "C++",
    "ruby": "Ruby",
    "php": "PHP",
    "elixir": "Elixir",
    "erlang": "Erlang",
    "haskell": "Haskell",
    "ocaml": "OCaml",
    "swift": "Swift",
    "dart": "Dart",
    "lua": "Lua",
    "perl": "Perl",
    "julia": "Julia",
    "zig": "Zig",
    "nim": "Nim",
    "fortran": "Fortran",
    "cobol": "COBOL",
    "solidity": "Solidity",
}

_ALIAS_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(rf"(?<![\w+#.]){re.escape(alias)}(?![\w+#])", re.IGNORECASE), display)
    for alias, display in sorted(LANGUAGE_ALIASES.items(), key=lambda item: -len(item[0]))
)


@dataclass(frozen=True)
class SupportStatus:
    """Every criterion a language must satisfy to be usable."""

    language: str
    adapter: bool
    knowledge: bool
    probe_defined: bool
    e2e_fixture: bool | None
    missing_knowledge: tuple[str, ...] = ()

    @property
    def supported(self) -> bool:
        """Adapter, knowledge pack and probe -- the runtime criteria."""
        return self.adapter and self.knowledge and self.probe_defined

    @property
    def complete(self) -> bool:
        """Supported *and* not known to lack its CI e2e fixture."""
        return self.supported and self.e2e_fixture is not False

    def to_dict(self) -> dict[str, object]:
        return {
            "language": self.language,
            "adapter": self.adapter,
            "knowledge": self.knowledge,
            "probe_defined": self.probe_defined,
            "e2e_fixture": self.e2e_fixture,
            "missing_knowledge": list(self.missing_knowledge),
            "supported": self.supported,
            "complete": self.complete,
        }


def e2e_fixture_root() -> Path | None:
    """Directory holding per-language e2e fixtures, or ``None`` outside a checkout."""
    override = os.environ.get("NAI_LANG_FIXTURES")
    if override:
        path = Path(override).expanduser()
        return path if path.is_dir() else None
    candidate = Path(__file__).resolve().parents[2] / "tests" / "e2e" / "fixtures" / "langs"
    return candidate if candidate.is_dir() else None


def fixture_path(language_id: str, *, root: Path | None = None) -> Path | None:
    base = root if root is not None else e2e_fixture_root()
    return (base / language_id / "project.json") if base is not None else None


def support_status(language_id: str, *, fixture_root: Path | None = None) -> SupportStatus:
    """Compute the support status of *language_id*."""
    adapter = registry.all_adapters().get(language_id)
    missing = knowledge.missing_sections(language_id) if adapter else tuple(knowledge.SECTIONS)
    fixture = fixture_path(language_id, root=fixture_root)
    return SupportStatus(
        language=language_id,
        adapter=adapter is not None,
        knowledge=adapter is not None and not missing,
        probe_defined=bool(getattr(adapter, "toolchain", ())) if adapter else False,
        e2e_fixture=(fixture is not None and fixture.is_file()) if fixture is not None else None,
        missing_knowledge=missing,
    )


def supported_languages(*, fixture_root: Path | None = None) -> list[str]:
    """Sorted ids of every registered language that meets the runtime criteria."""
    return [
        language_id
        for language_id in registry.supported()
        if support_status(language_id, fixture_root=fixture_root).supported
    ]


def is_supported(language_id: str | None) -> bool:
    """True when *language_id* may be used for a project."""
    if not language_id:
        return False
    return str(language_id).strip().lower() in supported_languages()


def require_supported(language_id: str | None) -> str:
    """Return the normalised id, or raise :class:`UnknownLanguageError`."""
    normalised = str(language_id or "").strip().lower()
    if not normalised:
        raise registry.UnknownLanguageError("a language is required")
    if not is_supported(normalised):
        supported = ", ".join(supported_languages()) or "(none)"
        raise registry.UnknownLanguageError(
            f"unsupported language {normalised!r}; supported: {supported}"
        )
    return normalised


def unsupported_languages_in(text: str) -> list[str]:
    """Display names of languages mentioned in *text* that N-AI cannot build.

    Matching is word-bounded and case-insensitive.  Supported languages (and
    their aliases) never appear in the result.
    """
    if not text:
        return []
    supported_displays = {
        str(getattr(registry.all_adapters().get(language_id), "display_name", language_id))
        for language_id in supported_languages()
    }
    found: set[str] = set()
    for pattern, display in _ALIAS_PATTERNS:
        if display in supported_displays:
            continue
        if pattern.search(text):
            found.add(display)
    return sorted(found)


def supported_languages_block() -> str:
    """Prompt block listing the languages the pipeline may choose."""
    lines = ["## Supported Languages (v1: one language per project)"]
    for language_id in supported_languages():
        adapter = registry.get(language_id)
        lines.append(
            f"- `{language_id}` -- {adapter.display_name}, tests: {adapter.test_framework}, "
            f"target stack: {adapter.stack_hint}"
        )
    lines.append(
        "Rule: pick exactly one id from this list. Never substitute a different language, "
        "and never invent one. If the brief asks for a language that is not listed, ask the "
        "owner which supported language to use instead."
    )
    return "\n".join(lines)
