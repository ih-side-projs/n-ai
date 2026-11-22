"""SCH-2 / SCH-3: which queued job runs next. Pure logic, no store or model access.

Batching is keyed by *model id* (the role's model in the project's profile), so two roles that share one model never
count as a swap, and two projects with different profiles do. A job whose handler never calls a model has key None and
runs in any batch.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Cand:
    job: dict
    key: str | None            # model id, or None for jobs that need no model
    depth: int                 # tree depth of the job's node; project-level jobs are -1
    order: int                 # first-seen counter: older jobs have smaller values
    project_id: str = ""

    def sort_key(self):
        """SCH-3: higher-level nodes first, then older first (job id breaks ties deterministically)."""
        return (self.depth, self.order, str(self.job.get("id")))


@dataclass
class Batcher:
    max_consecutive: int = 25
    max_seconds: float = 1800.0
    loaded_key: str | None = None      # model most recently seen loaded (from model calls or a /running probe)
    batch_key: str | None = None       # model the current batch is draining
    consecutive: int = 0
    started: float | None = None       # when the current batch began (None until its first job starts)
    switches: int = 0

    def note_loaded(self, key: str | None) -> None:
        self.loaded_key = key
        if self.batch_key is None and key is not None:
            self.batch_key = key

    def capped(self, now: float) -> bool:
        if self.consecutive >= self.max_consecutive:
            return True
        return self.started is not None and (now - self.started) >= self.max_seconds

    def choose(self, cands: list[Cand], running_keys: set, now: float) -> Cand | None:
        """The next job to start, or None to wait.

        1. Jobs needing no model are always eligible.
        2. Jobs for the batch's model are eligible; the batch ends early only when it hit a cap AND another model has work.
        3. Switching to another model needs every running model job to have finished (never swap under a running job).
        """
        ordered = sorted(cands, key=Cand.sort_key)
        if not ordered:
            return None
        busy = [k for k in running_keys if k is not None]
        current = busy[0] if busy else (self.batch_key or self.loaded_key)
        if current is None:                                  # nothing known yet: the highest priority job decides
            return ordered[0]
        same = [c for c in ordered if c.key is None or c.key == current]
        other = [c for c in ordered if c.key is not None and c.key != current]
        if busy:
            return same[0] if same else None
        draining = any(c.key == current for c in same)
        if same and not (other and draining and self.capped(now)):
            return same[0]
        return other[0] if other else None

    def note_started(self, c: Cand, now: float) -> None:
        if c.key is None:
            return
        if c.key != self.batch_key:
            if self.batch_key is not None:
                self.switches += 1
            self.batch_key, self.consecutive, self.started = c.key, 0, now
        elif self.started is None:
            self.started = now
        self.consecutive += 1
