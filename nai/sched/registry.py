"""Handler registry: how SS-SPEC and SS-BUILD plug job kinds into the runner (see README, "Handler API")."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


class SchedError(Exception):
    pass


class JobCancelled(SchedError):
    """Raised by RunContext.check() when the owner cancelled the job or the runner is shutting down."""


class BudgetExceeded(SchedError):
    """Raised by RunContext.check() when the job's node ran out of attempts, tokens or wall-clock time (X5)."""


@dataclass
class JobResult:
    ok: bool = True
    result: dict = field(default_factory=dict)   # stored as the job's result_ref
    error: str | None = None
    requeue: bool = False                        # put the job back in the queue instead of finishing it

    @classmethod
    def success(cls, **result) -> JobResult:
        return cls(True, result)

    @classmethod
    def failure(cls, error: str, **result) -> JobResult:
        return cls(False, result, error)

    def __getitem__(self, key: str) -> Any:
        """Dict-like access for callers/tests written before JobResult existed."""
        if key == "ok":
            return self.ok
        if key == "error":
            return self.error
        if key in self.result:
            return self.result[key]
        raise KeyError(key)

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default


@dataclass(frozen=True)
class Entry:
    kind: str
    handler: Callable
    uses_model: bool = True


class HandlerRegistry:
    def __init__(self):
        self._entries: dict[str, Entry] = {}

    def register(self, kind: str, handler: Callable, *, uses_model: bool = True) -> None:
        """`uses_model=False` for jobs that never call a model (verify, integrate): they run in any batch, cause no swap."""
        self._entries[kind] = Entry(kind, handler, uses_model)

    def job_handler(self, kind: str, *, uses_model: bool = True):
        def deco(fn):
            self.register(kind, fn, uses_model=uses_model)
            return fn
        return deco

    def get(self, kind: str) -> Entry | None:
        return self._entries.get(kind)

    def kinds(self) -> list[str]:
        return sorted(self._entries)
