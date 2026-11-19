"""X10: prompts live in versioned text files (nai/prompts/<name>.txt), never inline.

File format: header lines `key: value`, a line `---`, then the prompt body. The only
required header is `version`. `Prompt.id` ("orch_system@1") is recorded on model calls.
"""
from __future__ import annotations

from dataclasses import dataclass
from importlib import resources


@dataclass(frozen=True)
class Prompt:
    name: str
    version: str
    text: str

    @property
    def id(self) -> str:
        return f"{self.name}@{self.version}"


_CACHE: dict[str, Prompt] = {}


def get(name: str) -> Prompt:
    if name not in _CACHE:
        raw = resources.files("nai.prompts").joinpath(f"{name}.txt").read_text(encoding="utf-8")
        head, sep, body = raw.partition("\n---\n")
        if not sep:
            raise ValueError(f"prompt {name}: missing '---' header separator")
        meta = dict(line.split(":", 1) for line in head.splitlines() if ":" in line)
        version = meta.get("version", "").strip()
        if not version:
            raise ValueError(f"prompt {name}: missing version header")
        _CACHE[name] = Prompt(name, version, body.rstrip("\n"))
    return _CACHE[name]
