"""CORE-3: per-job path policy and job context.

A PathPolicy lists writable and read-only globs, matched against the workspace-relative
POSIX path. `*` matches within one path segment, `**` any number of segments (`**/` may
match zero directories), `?` one character, `[..]` a class, and a trailing `/` means
"everything below". A write is allowed iff it matches a writable glob and no read-only glob.
An empty `writable` tuple means nothing is writable. Reads are never restricted.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache


@lru_cache(maxsize=512)
def glob_to_regex(pat: str) -> re.Pattern:
    if pat.startswith("./"):
        pat = pat[2:]
    if pat.endswith("/"):
        pat += "**"
    out: list[str] = []
    i = 0
    while i < len(pat):
        c = pat[i]
        if c == "*":
            if pat[i:i + 2] == "**":
                i += 2
                if pat[i:i + 1] == "/":
                    out.append("(?:.*/)?")
                    i += 1
                else:
                    out.append(".*")
            else:
                out.append("[^/]*")
                i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[" and "]" in pat[i + 2:]:
            j = pat.index("]", i + 2)
            body = pat[i + 1:j]
            if body.startswith("!"):
                body = "^" + body[1:]
            out.append("[" + body.replace("\\", "\\\\") + "]")
            i = j + 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("".join(out) + r"\Z")


def glob_match(pat: str, rel: str) -> bool:
    return glob_to_regex(pat).match(rel) is not None


@dataclass(frozen=True)
class PathPolicy:
    writable: tuple[str, ...] = ("**",)
    readonly: tuple[str, ...] = ()

    @classmethod
    def allow_all(cls) -> PathPolicy:
        return cls()

    def allows_write(self, rel: str) -> bool:
        return (any(glob_match(g, rel) for g in self.writable)
                and not any(glob_match(g, rel) for g in self.readonly))

    def is_protected(self, rel: str) -> bool:
        return any(glob_match(g, rel) for g in self.readonly)


@dataclass
class JobContext:
    """What the workspace needs to know about the job currently using it (CORE-3/4/5)."""
    node_id: str | None = None
    job_kind: str | None = None
    attempt: int = 1
    policy: PathPolicy | None = None
    test_command: str | list[str] | None = None

    def test_commands(self) -> list[str]:
        if not self.test_command:
            return []
        return [self.test_command] if isinstance(self.test_command, str) else list(self.test_command)
