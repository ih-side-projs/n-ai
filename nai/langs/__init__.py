"""Language adapters.

Importing this package registers the built-in adapters.  The registry is the
only source of truth for which languages are supported; never hard-code a list
elsewhere.
"""

from . import knowledge, probe, registry, support
from ._c_family import CAdapter, CppAdapter
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
from .golang import GoAdapter
from .python import PythonAdapter
from .rust import RustAdapter
from .swift import SwiftAdapter
from .typescript import TypeScriptAdapter

registry.register(PythonAdapter())
registry.register(SwiftAdapter())
registry.register(TypeScriptAdapter())
registry.register(GoAdapter())
registry.register(RustAdapter())
registry.register(CAdapter())
registry.register(CppAdapter())

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
