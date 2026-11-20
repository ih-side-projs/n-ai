"""nai.service: thin supervisor for the web UI, runner, and on-demand nai jobs."""

from .config import ServiceConfig
from .daemon import ServiceDaemon, ServiceAlreadyRunning, daemon_main
from .cli import service_main

__all__ = [
    "ServiceConfig",
    "ServiceDaemon",
    "ServiceAlreadyRunning",
    "service_main",
    "daemon_main",
]
