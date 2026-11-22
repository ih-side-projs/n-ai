"""Runner configuration (X7): defaults, then the `[runner]` table of the TOML file, then command-line options."""
from __future__ import annotations

from dataclasses import dataclass, field, fields
from datetime import datetime
from pathlib import Path


@dataclass
class SchedConfig:
    poll_seconds: float = 1.0                  # how long the loop sleeps when nothing happened
    plan_interval_seconds: float = 2.0         # how often ready-promotion and planners run
    heartbeat_seconds: float = 2.0             # SCH-9
    concurrency: int = 1                       # SCH-3 (same-model slots, D4)
    max_consecutive_jobs: int = 25             # SCH-2 cap on consecutive jobs for one model
    max_batch_seconds: float = 1800.0          # SCH-2 cap on elapsed time for one model
    windows: list[str] = field(default_factory=list)   # SCH-8, local time, e.g. ["22:00-06:00"]
    until: datetime | None = None              # SCH-8 one-off stop time (local, naive)
    budgets: dict = field(default_factory=lambda: {"max_attempts": 8, "max_wall_seconds": 7200.0, "max_tokens": 2_000_000})
    attempt_job_kinds: list[str] = field(default_factory=lambda: ["implement"])   # starting one of these counts an attempt
    transient_retries: int = 5                 # server-unreachable requeues before a job is failed
    backoff_start_seconds: float = 2.0
    backoff_max_seconds: float = 60.0
    require_parent_contract_approval: bool = True   # SCH-4
    record_call_artifacts: bool = True         # X6: full prompt and response of every call
    record_log_events: bool = True             # WEB-5: log lines of a job become events
    max_log_events_per_job: int = 2000
    plugins: list[str] = field(default_factory=lambda: ["nai.spec", "nai.build"])
    threaded: bool = True                      # False runs jobs inline (tests, debugging); needs concurrency 1

    @classmethod
    def from_table(cls, table: dict | None) -> SchedConfig:
        cfg = cls()
        known = {f.name for f in fields(cls)}
        for k, v in (table or {}).items():
            if k not in known:
                raise ValueError(f"unknown [runner] key: {k}")
            if k == "budgets":
                v = {**cfg.budgets, **v}
            setattr(cfg, k, v)
        return cfg

    @classmethod
    def from_toml(cls, path: str | Path | None) -> SchedConfig:
        if not path:
            return cls()
        from ..config import _toml_load
        return cls.from_table(_toml_load(Path(path)).get("runner"))
