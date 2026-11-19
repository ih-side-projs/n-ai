"""Typed errors raised by the N-AI persistence layer."""


class StoreError(Exception):
    """Base class for store errors."""


class InvalidTransition(StoreError):
    """Raised when a node status transition is not permitted by STO-4."""


class DependencyError(StoreError):
    """Raised when a node dependency is not a sibling in the same project."""


class DependencyCycleError(DependencyError):
    """Raised when a dependency cycle is found while hashing a project tree."""


class ExportImportError(StoreError):
    """Raised for malformed or unsafe project export bundles."""
