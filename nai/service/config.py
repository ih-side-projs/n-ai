"""Configuration for the nai service supervisor."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ServiceConfig:
    """Settings for the supervisor. Defaults keep everything inside .nai/."""

    state_dir: Path = Path(".nai")
    start_web: bool = True
    start_runner: bool = True
    web_host: str = "127.0.0.1"
    web_port: int = 8080
    db_path: Path | None = None
    runner_args: list[str] = field(default_factory=list)
    python: str | None = None
    auto_restart: bool = True
    restart_limit: int = 3
    restart_backoff: float = 2.0
    stop_timeout: float = 15.0
    poll_interval: float = 0.5
    socket_name: str = "service.sock"
    pid_name: str = "service.pid"
    log_dir_name: str = "logs"

    @property
    def socket_path(self) -> Path:
        return self.state_dir / self.socket_name

    @property
    def pid_path(self) -> Path:
        return self.state_dir / self.pid_name

    @property
    def log_dir(self) -> Path:
        return self.state_dir / self.log_dir_name

    @property
    def effective_db_path(self) -> Path:
        return self.db_path or self.state_dir / "nai.sqlite3"

    @classmethod
    def from_env(cls, state_dir: str | Path | None = None, **overrides) -> ServiceConfig:
        import sys

        root = Path(state_dir or os.environ.get("NAI_STATE_DIR", ".nai"))
        cfg = cls(
            state_dir=root,
            db_path=Path(os.environ["NAI_DB_PATH"]) if os.environ.get("NAI_DB_PATH") else None,
            web_host=os.environ.get("NAI_WEB_HOST", "127.0.0.1"),
            web_port=int(os.environ.get("NAI_WEB_PORT", "8080")),
            python=os.environ.get("NAI_PYTHON") or sys.executable,
            auto_restart=os.environ.get("NAI_SERVICE_NO_RESTART", "").lower() not in ("1", "true", "yes"),
        )
        for key, value in overrides.items():
            if hasattr(cfg, key) and value is not None:
                setattr(cfg, key, value)
        return cfg

    def child_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["NAI_DB_PATH"] = str(self.effective_db_path)
        env["NAI_WEB_HOST"] = self.web_host
        env["NAI_WEB_PORT"] = str(self.web_port)
        # SCH-9 writes runner.json into the runner's state dir (same as state_dir here)
        env.setdefault("NAI_RUNNER_STATUS_FILE", str(self.state_dir / "runner.json"))
        return env
