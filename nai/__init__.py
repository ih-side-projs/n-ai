"""nai (N-AI): local-first spec-to-code system."""
from importlib.metadata import version as _version

try:
    __version__ = _version("nai")
except Exception:
    __version__ = "2.0.0.dev0"

# Import submodules to register CLI subcommands (nai run, service, etc.)
try:
    from .sched import cli as _sched_cli  # noqa: F401
except Exception:
    pass
try:
    from .service import cli as _service_cli  # noqa: F401
    from .cli import register_subcommand

    register_subcommand("service", _service_cli.service_main)
except Exception:
    pass

__all__ = ["__version__"]
