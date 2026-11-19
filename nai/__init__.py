"""nai (N-AI): local-first spec-to-code system."""
from importlib.metadata import version as _version

try:
    __version__ = _version("nai")
except Exception:
    __version__ = "2.0.0.dev0"

__all__ = ["__version__"]
