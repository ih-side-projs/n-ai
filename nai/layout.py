"""Deterministic project layout (pure functions, no filesystem I/O).

Every writer uses these helpers instead of hand-building ``src/...``,
``tests/...`` or ``SPEC-...`` paths.  The layout is::

    <output_root>/<project-slug>/
        .nai-project                 marker written at creation
        .nai-manifest.json           relpath -> {sha256, node_id, stage}
        SPEC.md                      root specification
        contract.json                frozen root interface contract
        specs/<node-path>/SPEC.md    child specifications
        src/<node-path>/             implementation files
        tests/<node-path>/           frozen tests

``<node-path>`` is the slug chain from the first non-root node (for example
``billing/invoice``).  The root node has an empty path and uses the project
root files.  Python currently uses a flat package layout (``src/<package>/``
and ``tests/``), which the Python adapter helpers below express through the
same functions.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from pathlib import PurePosixPath

MARKER_NAME = ".nai-project"
MANIFEST_NAME = ".nai-manifest.json"


def slugify(value: str) -> str:
    """Return a stable lowercase-hyphenated slug (total, never empty)."""
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text or "project"


def unique_slug(base: str, existing: Iterable[str]) -> str:
    """Return ``base`` or ``base-2``, ``base-3`` ... not present in *existing*.

    Slugs are assigned once at creation; callers must persist the result and
    never recompute it on rename.
    """
    base = slugify(base)
    taken = {str(item) for item in existing if item is not None}
    if base not in taken:
        return base
    index = 2
    while f"{base}-{index}" in taken:
        index += 1
    return f"{base}-{index}"


def project_root(output_root, project_slug: str):
    """Absolute project directory: ``<output_root>/<project-slug>``."""
    from pathlib import Path

    return Path(output_root).expanduser() / str(project_slug)


def node_path(slugs: Sequence[str]) -> str:
    """Join ancestor node slugs into a POSIX relative path (root = ``""``)."""
    return "/".join(str(part).strip("/") for part in slugs if str(part).strip("/"))


def node_dir(project_root, path: str = ""):
    """Directory holding a node's specification; the root node uses the project root."""
    from pathlib import Path

    root = Path(project_root)
    path = str(path or "").strip("/")
    return root if not path else root / "specs" / path


def spec_path(project_root, path: str = ""):
    """``SPEC.md`` for a node (root -> project root, child -> ``specs/<path>``)."""
    return node_dir(project_root, path) / "SPEC.md"


def contract_path(project_root):
    """The frozen root interface contract."""
    from pathlib import Path

    return Path(project_root) / "contract.json"


def src_dir(project_root, path: str = ""):
    """Source directory for a node (``src/`` for the root/flat layout)."""
    from pathlib import Path

    root = Path(project_root)
    path = str(path or "").strip("/")
    return root / "src" if not path else root / "src" / path


def test_path(project_root, path: str = "", filename: str | None = None):
    """Test directory or one test file for a node."""
    from pathlib import Path

    root = Path(project_root)
    path = str(path or "").strip("/")
    base = root / "tests" if not path else root / "tests" / path
    return base if filename is None else base / str(filename)


def manifest_path(project_root):
    """The project manifest (``.nai-manifest.json``)."""
    from pathlib import Path

    return Path(project_root) / MANIFEST_NAME


def marker_path(project_root):
    """The project marker (``.nai-project``)."""
    from pathlib import Path

    return Path(project_root) / MARKER_NAME


# ---------------------------------------------------------------------------
# Relative-path safety (pure; symlink resolution lives in nai.paths)
# ---------------------------------------------------------------------------

def relative_parts(value: str) -> tuple[str, ...]:
    """Split a relative POSIX/Windows path, rejecting absolute/traversal forms."""
    raw = str(value or "")
    if not raw or "\x00" in raw:
        raise ValueError("path must be a non-empty relative path")
    normalized = raw.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise ValueError("absolute paths are not allowed")
    path = PurePosixPath(normalized)
    if path.is_absolute():
        raise ValueError("absolute paths are not allowed")
    parts = tuple(part for part in path.parts if part not in ("", "."))
    if not parts or any(part == ".." for part in parts):
        raise ValueError("parent traversal is not allowed")
    return parts


def is_safe_relative(value: str) -> bool:
    """True when *value* is a safe relative path (no absolute, ``..`` or NUL)."""
    try:
        relative_parts(value)
    except ValueError:
        return False
    return True


def safe_join(project_root, value: str):
    """Lexically join a safe relative path below *project_root*.

    This does not touch the filesystem; use :func:`nai.paths.resolve_within`
    when symlink containment must be verified.
    """
    from pathlib import Path

    return Path(project_root).joinpath(*relative_parts(value))


def relative_to_root(project_root, path):
    """Return *path* relative to *project_root* as POSIX text, or raise."""
    from pathlib import Path

    root = Path(project_root)
    return Path(path).relative_to(root).as_posix()


# ---------------------------------------------------------------------------
# Python's flat package layout, expressed through the generic helpers
# ---------------------------------------------------------------------------

def module_slug(value: str) -> str:
    """A valid lowercase Python module/package identifier."""
    import keyword

    text = slugify(value).replace("-", "_")
    if not text:
        text = "component"
    if text[0].isdigit() or keyword.iskeyword(text):
        text = f"mod_{text}"
    return text


def project_package(project: dict) -> str:
    settings = (project or {}).get("settings") or {}
    if settings.get("package_name"):
        return module_slug(str(settings["package_name"]))
    return module_slug(str((project or {}).get("name") or "app"))


def python_src_init_rel() -> str:
    return (src_dir(".", "") / "__init__.py").as_posix()


def python_package_init_rel(project: dict) -> str:
    return (src_dir(".", "") / project_package(project) / "__init__.py").as_posix()


def python_source_rel(project: dict, node: dict) -> str:
    package = project_package(project)
    return (src_dir(".", "") / package / f"{module_slug(str(node['name']))}.py").as_posix()


def python_test_rel(node: dict) -> str:
    return test_path(".", "", f"test_{module_slug(str(node['name']))}.py").as_posix()


def python_entities_rel(project: dict) -> str:
    package = project_package(project)
    return (src_dir(".", "") / package / "entities.py").as_posix()


def python_source_path(project_root, project: dict, node: dict):
    return safe_join(project_root, python_source_rel(project, node))


def python_test_path(project_root, node: dict):
    return safe_join(project_root, python_test_rel(node))


def python_entities_path(project_root, project: dict):
    return safe_join(project_root, python_entities_rel(project))
