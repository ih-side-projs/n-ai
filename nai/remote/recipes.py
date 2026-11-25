"""Named recipes and their parameter allowlists.

Both sides import this module.  The orchestrator validates the parameters it
is about to queue; the worker validates them again before rendering argv, so a
compromised or buggy peer cannot widen the surface.  Recipes never involve a
shell: ``render_argv`` returns a list of arguments.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from .errors import RecipeError

#: A single argv-safe token.  Deliberately excludes quotes, spaces, newlines,
#: globs, ``$``, backticks and ``;`` so nothing can be re-interpreted later.
TOKEN_RE = re.compile(r"^[A-Za-z0-9._:/@+=,-]{1,200}$")
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
MAX_LIST = 64
MAX_RELPATH = 512

#: Kinds whose value is a list spliced into argv as separate arguments.
LIST_KINDS = frozenset({"relpaths", "tokens"})
KINDS = frozenset({"token", "tokens", "relpath", "relpaths", "int", "choice"})
#: Recipes never run a shell; a template that starts with one is a config error.
SHELLS = frozenset({"sh", "bash", "dash", "zsh", "ksh", "fish", "csh", "tcsh", "cmd", "cmd.exe",
                    "powershell", "powershell.exe", "pwsh"})


@dataclass(frozen=True)
class ParamSpec:
    """One allowed parameter and the values it may take."""

    kind: str
    choices: tuple[str, ...] = ()
    required: bool = True
    description: str = ""

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise RecipeError(f"unknown parameter kind: {self.kind}")
        if self.kind == "choice" and not self.choices:
            raise RecipeError("a choice parameter needs choices")


@dataclass(frozen=True)
class RecipeSpec:
    """A recipe the orchestrator may ask a worker to run."""

    id: str
    params: Mapping[str, ParamSpec] = field(default_factory=dict)
    capabilities: tuple[str, ...] = ()
    timeout: float = 900.0
    description: str = ""


def _clean_relpath(name: str, value: Any) -> str:
    if not isinstance(value, str):
        raise RecipeError(f"parameter {name!r} must be a string path")
    text = value.strip().replace("\\", "/")
    if not text or len(text) > MAX_RELPATH:
        raise RecipeError(f"parameter {name!r} is not a usable relative path")
    if _CONTROL.search(text):
        raise RecipeError(f"parameter {name!r} contains control characters")
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        raise RecipeError(f"parameter {name!r} must be relative to the project root")
    parts = [p for p in text.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise RecipeError(f"parameter {name!r} may not escape the project root")
    if not parts:
        return "."
    return PurePosixPath(*parts).as_posix()


def _clean_token(name: str, value: Any) -> str:
    if not isinstance(value, str) or not TOKEN_RE.match(value):
        raise RecipeError(f"parameter {name!r} must match {TOKEN_RE.pattern}")
    return value


def _check_value(name: str, spec: ParamSpec, value: Any) -> Any:
    if spec.kind == "token":
        return _clean_token(name, value)
    if spec.kind == "relpath":
        return _clean_relpath(name, value)
    if spec.kind in LIST_KINDS:
        if isinstance(value, str) or not isinstance(value, Iterable):
            raise RecipeError(f"parameter {name!r} must be a list")
        items = list(value)
        if len(items) > MAX_LIST:
            raise RecipeError(f"parameter {name!r} accepts at most {MAX_LIST} entries")
        cleaner = _clean_token if spec.kind == "tokens" else _clean_relpath
        return [cleaner(name, item) for item in items]
    if spec.kind == "int":
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise RecipeError(f"parameter {name!r} must be an integer") from None
        if not -1_000_000 <= number <= 1_000_000:
            raise RecipeError(f"parameter {name!r} is out of range")
        return number
    if spec.kind == "choice":
        if value not in spec.choices:
            raise RecipeError(f"parameter {name!r} must be one of {', '.join(spec.choices)}")
        return value
    raise RecipeError(f"parameter {name!r} has an unsupported kind")  # pragma: no cover


def validate_params(spec: RecipeSpec, params: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return the normalised parameters or raise :class:`RecipeError`.

    Unknown names are rejected rather than ignored: a typo must not silently
    drop a flag, and a hostile peer must not smuggle extra arguments.
    """
    if params is None:
        params = {}
    if not isinstance(params, Mapping):
        raise RecipeError("params must be an object")
    unknown = sorted(set(params) - set(spec.params))
    if unknown:
        raise RecipeError(f"recipe {spec.id!r} does not accept: {', '.join(unknown)}")
    cleaned: dict[str, Any] = {}
    for name, param in spec.params.items():
        if name not in params or params[name] is None:
            if param.required:
                raise RecipeError(f"recipe {spec.id!r} requires parameter {name!r}")
            continue
        cleaned[name] = _check_value(name, param, params[name])
    return cleaned


def spec_for(recipe: str) -> RecipeSpec:
    """Look up a recipe, rejecting unknown ids."""
    try:
        return RECIPES[str(recipe)]
    except KeyError:
        raise RecipeError(f"unknown recipe: {recipe!r}") from None


def check_template(spec: RecipeSpec, template: Sequence[str], variables: Iterable[str]) -> None:
    """Validate a worker's argv template against the recipe spec.

    Every placeholder must be a declared parameter or one of the worker's own
    variables (``python``, ``workdir``); a list parameter must occupy a whole
    argument so it can be spliced safely.
    """
    allowed_vars = set(variables)
    if not template:
        raise RecipeError(f"recipe {spec.id!r} has an empty argv template")
    program = str(template[0]).strip().rsplit("/", 1)[-1].lower()
    if program in SHELLS:
        raise RecipeError(f"recipe {spec.id!r} may not invoke a shell ({template[0]!r})")
    for entry in template:
        if not isinstance(entry, str) or _CONTROL.search(entry):
            raise RecipeError(f"recipe {spec.id!r} has an unsafe argv entry")
        for name in _PLACEHOLDER.findall(entry):
            if name in allowed_vars:
                continue
            param = spec.params.get(name)
            if param is None:
                raise RecipeError(f"recipe {spec.id!r} template uses unknown placeholder {name!r}")
            if param.kind in LIST_KINDS and entry.strip() != "{" + name + "}":
                raise RecipeError(
                    f"recipe {spec.id!r}: list parameter {name!r} must be a whole argument"
                )


def render_argv(
    spec: RecipeSpec,
    template: Sequence[str],
    params: Mapping[str, Any] | None,
    variables: Mapping[str, Any] | None = None,
) -> list[str]:
    """Render a validated argv template into concrete arguments."""
    cleaned = validate_params(spec, params)
    values = {str(k): str(v) for k, v in (variables or {}).items()}
    argv: list[str] = []
    for entry in template:
        match = _PLACEHOLDER.fullmatch(entry.strip())
        if match:
            name = match.group(1)
            if name in values:
                argv.append(values[name])
                continue
            param = spec.params.get(name)
            if param is None:
                raise RecipeError(f"unknown placeholder {name!r}")
            if param.kind in LIST_KINDS:
                argv.extend(str(item) for item in cleaned.get(name, []))
            elif name in cleaned:
                argv.append(str(cleaned[name]))
            continue
        rendered = entry
        for name in _PLACEHOLDER.findall(entry):
            if name in values:
                rendered = rendered.replace("{" + name + "}", values[name])
                continue
            param = spec.params.get(name)
            if param is None:
                raise RecipeError(f"unknown placeholder {name!r}")
            if param.kind in LIST_KINDS:
                raise RecipeError(f"list parameter {name!r} must be a whole argument")
            if name in cleaned:
                rendered = rendered.replace("{" + name + "}", str(cleaned[name]))
        argv.append(rendered)
    if not argv:
        raise RecipeError(f"recipe {spec.id!r} rendered an empty command")
    return argv


#: Recipes the orchestrator may queue.  A worker's ``worker.toml`` may define
#: a subset of these (never an id that is missing here).
RECIPES: dict[str, RecipeSpec] = {
    "echo": RecipeSpec(
        id="echo",
        params={"message": ParamSpec("token")},
        description="Echo one token; used by the end-to-end worker test.",
    ),
    "toolchain": RecipeSpec(
        id="toolchain",
        capabilities=("python",),
        timeout=120.0,
        description="Report the worker's python version.",
    ),
    "build": RecipeSpec(
        id="build",
        params={"targets": ParamSpec("relpaths", required=False)},
        capabilities=("python",),
        description="Compile/type-check the project or selected paths.",
    ),
    "lint": RecipeSpec(
        id="lint",
        params={"targets": ParamSpec("relpaths", required=False)},
        capabilities=("python",),
        description="Run the adapter's lint gate on the worker.",
    ),
    "verify": RecipeSpec(
        id="verify",
        params={
            "targets": ParamSpec("relpaths", required=False),
            "args": ParamSpec("tokens", required=False),
        },
        capabilities=("python",),
        timeout=1800.0,
        description="Run the frozen tests on the worker.",
    ),
    "integrate": RecipeSpec(
        id="integrate",
        params={"targets": ParamSpec("relpaths", required=False)},
        capabilities=("python",),
        timeout=1800.0,
        description="Run integration tests on the worker.",
    ),
}

