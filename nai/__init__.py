"""nai (N-AI): local-first spec-to-code system."""
from importlib.metadata import version as _version

try:
    __version__ = _version("nai")
except Exception:
    __version__ = "2.0.0.dev0"

# Import submodules to register CLI subcommands (nai run, service, etc.).
# Failures are recorded rather than silently swallowed: `nai doctor` reports them.
_SUBCOMMAND_IMPORT_ERRORS: dict[str, str] = {}
try:
    from .sched import cli as _sched_cli  # noqa: F401
except Exception as _exc:
    _SUBCOMMAND_IMPORT_ERRORS["run"] = f"{type(_exc).__name__}: {_exc}"
try:
    from .cli import register_subcommand
    from .eval.cli import bench_main as _bench_main
    from .service import cli as _service_cli  # noqa: F401
    from .tools.cli import mcp_main as _mcp_main

    def _doctor_cli(argv: list[str]) -> int:
        from .doctor import main
        return main(argv)

    register_subcommand("service", _service_cli.service_main)
    register_subcommand("mcp", _mcp_main)
    register_subcommand("bench", _bench_main)
    register_subcommand("doctor", _doctor_cli)
except Exception as _exc:
    _SUBCOMMAND_IMPORT_ERRORS["service/mcp/bench/doctor"] = f"{type(_exc).__name__}: {_exc}"

__all__ = ["__version__", "_SUBCOMMAND_IMPORT_ERRORS"]
