"""Per-language knowledge packs.

Each registered language ships a small, hard-capped pack::

    nai/langs/knowledge/<language>/{spec,stub,test,impl,pitfalls,compile_errors}.md

The cap exists because the coder context is small.  ``MAX_SECTION_CHARS`` is
``~600 tokens`` at the project-wide ``chars / 4`` estimate used by
:mod:`nai.build.tenv`; ``tests/langs/test_knowledge.py`` enforces it.

Content rules for a pack: only facts the author is certain of, with tool
versions stated.  No marketing prose, no speculative APIs.
"""

from __future__ import annotations

from pathlib import Path

KNOWLEDGE_DIR = Path(__file__).resolve().parent

SECTIONS = ("spec", "stub", "test", "impl", "pitfalls", "compile_errors")

#: ``~600 tokens`` at the repo's ``chars / 4`` estimate.
MAX_SECTION_TOKENS = 600
MAX_SECTION_CHARS = MAX_SECTION_TOKENS * 4

#: Which pack sections a job kind receives.  Unknown job kinds get no pack.
JOB_SECTIONS: dict[str, tuple[str, ...]] = {
    "intake": ("spec",),
    "clarify": ("spec",),
    "decompose": ("spec",),
    "interfaces": ("spec",),
    "component_spec": ("spec",),
    "contract_stub": ("stub", "compile_errors"),
    "author_tests": ("test", "pitfalls"),
    "implement": ("impl", "stub", "pitfalls"),
    "integrate": ("impl", "pitfalls"),
    "verify": ("compile_errors",),
    "diagnose": ("compile_errors", "pitfalls"),
    "review": ("pitfalls",),
}

_SECTION_TITLES = {
    "spec": "Writing the spec",
    "stub": "Contract stubs",
    "test": "Tests",
    "impl": "Implementation",
    "pitfalls": "Pitfalls",
    "compile_errors": "Compile / import errors",
}


def knowledge_dir(language_id: str) -> Path:
    return KNOWLEDGE_DIR / language_id


def section_path(language_id: str, section: str) -> Path:
    return knowledge_dir(language_id) / f"{section}.md"


def has_pack(language_id: str) -> bool:
    """True when every section file exists and is non-empty."""
    return not missing_sections(language_id)


def missing_sections(language_id: str) -> tuple[str, ...]:
    """Sections that are absent, empty or over the cap."""
    missing: list[str] = []
    for section in SECTIONS:
        path = section_path(language_id, section)
        if not path.is_file():
            missing.append(section)
            continue
        text = path.read_text(encoding="utf-8").strip()
        if not text or len(text) > MAX_SECTION_CHARS:
            missing.append(section)
    return tuple(missing)


def load_section(language_id: str, section: str) -> str:
    """Return one section's text, or ``""`` when it is not shipped."""
    if section not in SECTIONS:
        raise KeyError(f"unknown knowledge section: {section}")
    path = section_path(language_id, section)
    return path.read_text(encoding="utf-8").strip() if path.is_file() else ""


def sections_for_job(job_kind: str | None) -> tuple[str, ...]:
    """Pack sections to inject for *job_kind* (empty for unknown kinds)."""
    if not job_kind:
        return ()
    return JOB_SECTIONS.get(str(job_kind), ())


def estimate_tokens(text: str) -> int:
    """The project-wide estimate: ``chars / 4`` rounded up."""
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def block(language_id: str, job_kind: str | None, *, sections: tuple[str, ...] | None = None) -> str:
    """Render the pack sections for *job_kind* as one prompt block."""
    chosen = sections if sections is not None else sections_for_job(job_kind)
    parts: list[str] = []
    for section in chosen:
        text = load_section(language_id, section)
        if text:
            parts.append(f"### {_SECTION_TITLES.get(section, section)}\n{text}")
    if not parts:
        return ""
    return f"## Language Knowledge: {language_id}\n\n" + "\n\n".join(parts)
