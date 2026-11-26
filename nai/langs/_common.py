"""Shared helpers for the compiled-language adapters.

TypeScript, Go, Rust, C and C++ all need the same three things: stable
identifier casing, a way to read a contract's components and ``provides``
entries, and a cheap structural syntax check.  None of it is language-specific,
so it lives here instead of being copied five times.

Everything in this module is pure description: no filesystem I/O and no
subprocesses.
"""

from __future__ import annotations

import re
from typing import Any

_WORDS = re.compile(r"[A-Za-z0-9]+")
#: Split ``WordCounter`` into ``Word Counter`` so casing conversions round-trip.
_CASE_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _words(value: str) -> list[str]:
    return _WORDS.findall(_CASE_BOUNDARY.sub(" ", str(value or "")))

#: ``(open, close)`` pairs the structural check balances.
_PAIRS = {"(": ")", "[": "]", "{": "}"}
_CLOSERS = {close: open_ for open_, close in _PAIRS.items()}


# -- naming ----------------------------------------------------------------


def pascal(value: str, *, fallback: str = "Component") -> str:
    """``"word-counter"`` -> ``"WordCounter"``; never empty, never leading digit."""
    words = _words(str(value or ""))
    ident = "".join(word[:1].upper() + word[1:] for word in words) or fallback
    if ident[0].isdigit():
        ident = f"T{ident}"
    return ident


def camel(value: str, *, fallback: str = "component") -> str:
    """``"word-counter"`` -> ``"wordCounter"``."""
    ident = pascal(value, fallback=fallback)
    return ident[:1].lower() + ident[1:]


def snake(value: str, *, fallback: str = "component") -> str:
    """``"word-counter"`` -> ``"word_counter"``."""
    words = [word.lower() for word in _words(str(value or ""))]
    ident = "_".join(words) or fallback
    if ident[0].isdigit():
        ident = f"t_{ident}"
    return ident


def kebab(value: str, *, fallback: str = "component") -> str:
    """``"WordCounter"`` -> ``"word-counter"``."""
    words = [word.lower() for word in _words(str(value or ""))]
    return "-".join(words) or fallback


# -- contract traversal ----------------------------------------------------


def components(project: dict[str, Any], contract: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Components from the project's frozen map, else from the passed contract."""
    settings = (project or {}).get("settings") or {}
    block = settings.get("component_map") if isinstance(settings, dict) else None
    raw = (block or {}).get("components") if isinstance(block, dict) else None
    if not raw:
        raw = (contract or {}).get("components")
    return [c for c in (raw or []) if isinstance(c, dict) and str(c.get("name") or "").strip()]


def component_entry(contract: dict[str, Any] | None, node: dict[str, Any]) -> dict[str, Any]:
    """The frozen contract entry for *node* (empty when absent)."""
    name = str(node.get("name") or "").strip()
    for component in (contract or {}).get("components") or []:
        if isinstance(component, dict) and str(component.get("name") or "").strip() == name:
            return component
    return {}


def entities(contract: dict[str, Any] | None) -> list[dict[str, Any]]:
    return [e for e in (contract or {}).get("entities") or [] if isinstance(e, dict)]


def _split_params(text: str) -> list[str]:
    """Split a parameter list on top-level commas."""
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for char in text:
        if char in "([{<":
            depth += 1
        elif char in ")]}>":
            depth = max(0, depth - 1)
        if char == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    if current:
        parts.append("".join(current).strip())
    return [part for part in parts if part]


def _parse_param(text: str) -> tuple[str, str]:
    """``"name: type"`` or ``"const char *name"`` -> ``(name, type)``."""
    cleaned = str(text or "").strip()
    if not cleaned:
        return "", ""
    if ":" in cleaned:
        head, _, tail = cleaned.partition(":")
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", head.strip()):
            return head.strip(), tail.strip()
    words = cleaned.split()
    if len(words) >= 2:
        name = words[-1].lstrip("*&")
        prefix = " ".join(words[:-1])
        stars = "*" * (len(words[-1]) - len(words[-1].lstrip("*")))
        return name, f"{prefix}{stars}".strip()
    return cleaned.lstrip("*&"), ""


def parse_provide(provide: dict[str, Any]) -> tuple[str, list[tuple[str, str]], str]:
    """Return ``(name, [(param, type)], return_type)`` for one ``provides`` entry.

    ``params``/``returns`` win; a ``signature`` is parsed as a fallback so
    hand-written contracts still work.  Both ``name: type`` and C-style
    ``type name`` parameters are understood.
    """
    name = str(provide.get("name") or "").strip()
    params: list[Any] = list(provide.get("params") or [])
    returns = str(provide.get("returns") or "").strip()
    signature = str(provide.get("signature") or "").strip()
    if signature:
        head, _, tail = signature.partition("->")
        head = head.strip()
        if tail.strip():
            returns = returns or tail.strip()
        match = re.match(
            r"^(?:(?:func|fn|function|def)\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*\((.*)\)\s*$", head
        )
        if match:
            name = name or match.group(1)
            params = _split_params(match.group(2))
        else:
            match = re.match(r"^(.*?)\b([A-Za-z_][A-Za-z0-9_]*)\s*\((.*)\)\s*$", head)
            if match:
                name = name or match.group(2)
                returns = returns or match.group(1).strip()
                params = _split_params(match.group(3))
    rendered: list[tuple[str, str]] = []
    for index, param in enumerate(params):
        if isinstance(param, dict):
            rendered.append((str(param.get("name") or f"arg{index}"), str(param.get("type") or "")))
        else:
            param_name, param_type = _parse_param(str(param))
            rendered.append((param_name or f"arg{index}", param_type))
    return name or "operation", rendered, returns


# -- structural syntax check -----------------------------------------------


def structural_error(
    code: str,
    filename: str,
    *,
    preprocessor: bool = False,
    char_literals: bool = True,
) -> str | None:
    """Balance ``()``/``[]``/``{}`` while skipping comments and literals.

    This is a cheap, always-available pre-check: it catches the truncation and
    mismatched-brace mistakes a model makes most often.  It is *not* a parser;
    the toolchain remains the authority.  ``preprocessor`` skips ``#`` directive
    lines (C/C++ ``#define`` bodies often hold unbalanced braces).
    ``char_literals`` is disabled for Rust, where ``'a`` is a lifetime.
    """
    text = str(code or "")
    stack: list[tuple[str, int]] = []
    index = 0
    line = 1
    length = len(text)
    while index < length:
        char = text[index]
        nxt = text[index + 1] if index + 1 < length else ""
        if char == "\n":
            line += 1
            index += 1
            continue
        if char == "#" and preprocessor and not stack:
            while index < length and text[index] != "\n":
                if text[index] == "\\" and index + 1 < length and text[index + 1] == "\n":
                    index += 2
                    line += 1
                    continue
                index += 1
            continue
        if char == "/" and nxt == "/":
            while index < length and text[index] != "\n":
                index += 1
            continue
        if char == "/" and nxt == "*":
            index += 2
            while index < length and not (
                text[index] == "*" and index + 1 < length and text[index + 1] == "/"
            ):
                if text[index] == "\n":
                    line += 1
                index += 1
            index += 2
            continue
        if char == '"':
            index += 1
            while index < length and text[index] != '"':
                if text[index] == "\\":
                    index += 1
                elif text[index] == "\n":
                    line += 1
                index += 1
            index += 1
            continue
        if char == "`":
            index += 1
            while index < length and text[index] != "`":
                if text[index] == "\n":
                    line += 1
                index += 1
            index += 1
            continue
        if char_literals and char == "'":
            # Rust lifetimes ('a) are not literals; only treat 'x' as one.
            if index + 2 < length and text[index + 2] == "'":
                index += 3
                continue
            if nxt == "\\" and index + 3 < length and text[index + 3] == "'":
                index += 4
                continue
        if char in _PAIRS:
            stack.append((char, line))
        elif char in _CLOSERS:
            if not stack or stack[-1][0] != _CLOSERS[char]:
                return f"{filename}:{line}: unmatched {char!r}"
            stack.pop()
        index += 1
    if stack:
        opener, opened = stack[-1]
        return f"{filename}:{opened}: unclosed {opener!r}"
    return None


# -- integration pre-stub detection ---------------------------------------

#: Diagnostics a build emits when a module/header/symbol does not exist yet.
#: Compared case-insensitively against the build output.
_MISSING_MARKERS = (
    "no such file",
    "cannot find module",
    "cannot find name",
    "unresolved import",
    "unresolved module",
    "use of undeclared crate",
    "failed to resolve",
    "undefined:",
    "undefined reference",
    "symbol(s) not found",
    "no required module provides",
    "did not match any packages",
    "no matching package",
    "matched no packages",
)


def integration_build_failure(output: str, module: str) -> bool:
    """True when a build failed *only* because ``module`` does not exist yet.

    Integration nodes have no contract stub, so the expected pre-implementation
    state is a build failure that names the integration module itself.  Requiring
    the module name in the diagnostics keeps a genuine typo in the frozen test
    from being mistaken for the expected state.
    """
    text = str(output or "")
    name = str(module or "").strip()
    if not text or not name or name not in text:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in _MISSING_MARKERS)


__all__ = [
    "camel",
    "component_entry",
    "components",
    "entities",
    "integration_build_failure",
    "kebab",
    "parse_provide",
    "pascal",
    "snake",
    "structural_error",
]
