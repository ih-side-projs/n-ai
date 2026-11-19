"""nai.spec — SS-SPEC: Local Spec Pipeline (WP4).

This package implements the spec pipeline stages (Section 9) as WP3 scheduler plugins:
- decompose: component map generation (reasoner)
- select_map: candidate selection & review (reasoner) - optional
- interfaces: contract generation (reasoner)
- check_interfaces: scenario walk-through (reasoner)
- component_spec: per-child spec generation (reasoner)
- reconcile: sibling reconciliation (reasoner)
- lint: mechanical spec validation (code, no model)
- clarify: brief clarification (reasoner) - optional

Each stage has its own prompt (X10), JSON schema (X1), and retry budget.
"""

from .plugin import register

__all__ = ["register"]