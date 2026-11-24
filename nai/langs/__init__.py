"""Language adapters.

Importing this package registers the built-in adapters.  The registry is the
only source of truth for which languages are supported; never hard-code a list
elsewhere.
"""

from . import registry
from .base import (
    Command,
    LanguageAdapter,
    Outcome,
    ParseResult,
    StubFile,
    StubResult,
    TestCase,
)
from .python import PythonAdapter

registry.register(PythonAdapter())

__all__ = [
    "Command",
    "LanguageAdapter",
    "Outcome",
    "ParseResult",
    "StubFile",
    "StubResult",
    "TestCase",
    "registry",
]
