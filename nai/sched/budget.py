"""X5 / SCH-5: per-node budgets. Usage is summed from the node's finished jobs; the runner checks it before a job
starts, and between model calls of a running job."""
from __future__ import annotations

from dataclasses import dataclass, replace

from .util import parse_ts


@dataclass(frozen=True)
class Budget:
    max_attempts: int = 8
    max_wall_seconds: float = 7200.0
    max_tokens: int = 2_000_000

    def scaled(self, factor: float) -> Budget:
        """e.g. the escalation pass of BLD-9 gets a larger token and time budget (job input_ref budget_multiplier)."""
        return replace(self, max_wall_seconds=self.max_wall_seconds * factor, max_tokens=int(self.max_tokens * factor))


def budget_for(defaults: dict, project_settings: dict | None, multiplier: float = 1.0) -> Budget:
    """Built-in < runner defaults < project settings["budgets"]. Keys: max_attempts, max_wall_seconds, max_tokens."""
    merged = {**{f: getattr(Budget(), f) for f in ("max_attempts", "max_wall_seconds", "max_tokens")}, **defaults,
              **((project_settings or {}).get("budgets") or {})}
    b = Budget(int(merged["max_attempts"]), float(merged["max_wall_seconds"]), int(merged["max_tokens"]))
    return b.scaled(multiplier) if multiplier and multiplier != 1.0 else b


@dataclass
class Usage:
    attempts: int = 0
    tokens: int = 0
    seconds: float = 0.0


class UsageTracker:
    """Per-node usage since the node's last owner retry (a retry grants a fresh budget)."""

    def __init__(self):
        self._tokens: dict[str, int] = {}
        self._seconds: dict[str, float] = {}
        self._base: dict[str, Usage] = {}

    @staticmethod
    def job_seconds(job: dict) -> float:
        a, b = parse_ts(job.get("started_at")), parse_ts(job.get("ended_at"))
        return max(0.0, (b - a).total_seconds()) if a and b else 0.0

    def load(self, jobs: list[dict], bases: dict[str, Usage] | None = None) -> None:
        for j in jobs:
            if j.get("node_id") and j.get("status") in ("succeeded", "failed", "cancelled"):
                self.add(j["node_id"], int(j.get("tokens_in") or 0) + int(j.get("tokens_out") or 0), self.job_seconds(j))
        self._base.update(bases or {})

    def add(self, node_id: str, tokens: int, seconds: float) -> None:
        self._tokens[node_id] = self._tokens.get(node_id, 0) + tokens
        self._seconds[node_id] = self._seconds.get(node_id, 0.0) + seconds

    def reset(self, node_id: str, attempts: int) -> Usage:
        """Owner retry: remember today's totals as the new zero. Returns the base (persisted by the runner as an event)."""
        base = Usage(attempts, self._tokens.get(node_id, 0), self._seconds.get(node_id, 0.0))
        self._base[node_id] = base
        return base

    def usage(self, node_id: str, attempts: int = 0, extra_tokens: int = 0, extra_seconds: float = 0.0) -> Usage:
        b = self._base.get(node_id, Usage())
        return Usage(max(0, attempts - b.attempts),
                     self._tokens.get(node_id, 0) - b.tokens + extra_tokens,
                     self._seconds.get(node_id, 0.0) - b.seconds + extra_seconds)

    @staticmethod
    def exceeded(u: Usage, b: Budget, *, attempts_inclusive: bool = False) -> str | None:
        """Why the budget is used up, or None. Before a job starts an attempt counts when attempts >= max_attempts."""
        if u.attempts >= b.max_attempts:
            return f"attempts {u.attempts} reached the limit of {b.max_attempts}"
        if u.tokens >= b.max_tokens:
            return f"tokens {u.tokens} reached the limit of {b.max_tokens}"
        if u.seconds >= b.max_wall_seconds:
            return f"wall-clock {u.seconds:.0f}s reached the limit of {b.max_wall_seconds:.0f}s"
        return None
