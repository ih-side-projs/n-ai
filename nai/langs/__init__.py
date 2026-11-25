"""Language adapters.

Importing this package registers the built-in adapters.  The registry is the
only source of truth for which languages are supported; never hard-code a list
elsewhere.
"""

from . import knowledge, probe, registry, support
from .base import (
    Command,
    LanguageAdapter,
    Outcome,
    ParseResult,
    StubFile,
    StubResult,
    TestCase,
    ToolCheck,
)
from .python import PythonAdapter
from .swift import SwiftAdapter

registry.register(PythonAdapter())
registry.register(SwiftAdapter())

__all__ = [
    "Command",
    "LanguageAdapter",
    "Outcome",
    "ParseResult",
    "StubFile",
    "StubResult",
    "TestCase",
    "ToolCheck",
    "knowledge",
    "probe",
    "registry",
    "support",
]
